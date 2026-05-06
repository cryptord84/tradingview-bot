"""Global risk circuit breaker for all Kalshi bots.

Monitors aggregate P&L across all bots and triggers a global halt
when the daily loss exceeds the configured threshold. All bots are
killed immediately and a Telegram alert is sent.

Also provides:
- Category-level exposure limits (sports, esports, crypto, politics, etc.)
- Liquidity-based position sizing (thin books → fewer contracts)

Reset manually via API or Telegram after investigating the loss.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.risk")

# ─── Category Detection ──────────────────────────────────────────────────────

# Ticker prefix → category mapping. Longer prefixes win (see detect_category).
TICKER_CATEGORY_MAP = {
    "KXMLB": "sports", "KXNBA": "sports", "KXNFL": "sports", "KXNHL": "sports",
    "KXWNBA": "sports", "KXUFC": "sports", "KXNCAA": "sports", "KXATP": "sports",
    "KXWTA": "sports", "KXSOCCER": "sports", "KXARG": "sports",
    "KXCS2": "esports", "KXDOTA": "esports", "KXLOL": "esports",
    "KXVALORANT": "esports", "KXOVERWATCH": "esports", "KXMVE": "esports",
    "KXBTCD": "crypto_strikes", "KXETHD": "crypto_strikes", "KXSOLD": "crypto_strikes",
    "KXBTC": "crypto", "KXETH": "crypto", "KXSOL": "crypto",
    "KXFED": "finance", "KXCPI": "finance", "KXGDP": "finance",
    "KXINX": "finance", "KXNDX": "finance", "KXSPY": "finance", "KXDJI": "finance",
    "KXRUS": "finance", "KXVIX": "finance",
}

# Sorted longest-first so KXBTCD matches before KXBTC.
_TICKER_PREFIXES_SORTED = sorted(TICKER_CATEGORY_MAP.items(), key=lambda kv: -len(kv[0]))

# Title keyword fallback
TITLE_CATEGORY_KEYWORDS = {
    "sports": ["mlb", "nba", "nfl", "nhl", "soccer", "ufc", "wnba", "tennis",
               "game", "match", "playoff", "championship", "super bowl", "world series"],
    "esports": ["cs2", "dota", "lol", "valorant", "overwatch", "esport"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"],
    "finance": ["fed ", "interest rate", "gdp", "cpi", "inflation", "treasury", "earnings",
                "s&p", "nasdaq", "dow jones", "index", "russell"],
    "politics": ["election", "trump", "biden", "president", "senate", "congress", "vote"],
    "weather": ["hurricane", "temperature", "weather", "tornado", "storm"],
}


def detect_category(ticker: str, title: str = "") -> str:
    """Detect market category from ticker prefix or title keywords."""
    ticker_upper = ticker.upper()
    for prefix, cat in _TICKER_PREFIXES_SORTED:
        if ticker_upper.startswith(prefix):
            return cat

    title_lower = title.lower()
    for cat, keywords in TITLE_CATEGORY_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return cat

    return "other"


# ─── Liquidity Sizing ────────────────────────────────────────────────────────

# Max order size as a fraction of total book depth (avoid moving the market)
MAX_BOOK_FRACTION = 0.10  # Never take more than 10% of visible depth

# Max order cost as a fraction of account balance
MAX_BALANCE_FRACTION = 0.15  # Never risk more than 15% of balance on one trade

# Depth thresholds: (min_total_depth_contracts, max_order_size)
# Used as a fallback floor — proportional sizing may go lower
LIQUIDITY_TIERS = [
    (500, None),   # Deep book: use configured max
    (200, 50),     # Moderate
    (50, 20),      # Thin
    (10, 10),      # Very thin
    (0, 5),        # Barely any liquidity
]


class KalshiRiskManager:
    """Cross-bot daily loss circuit breaker with category limits and liquidity sizing."""

    def __init__(self):
        cfg = get("kalshi") or {}
        risk_cfg = cfg.get("risk_manager", {})

        self.enabled = risk_cfg.get("enabled", True)
        self.max_daily_loss_cents = risk_cfg.get("max_daily_loss_cents", 1000)  # $10
        self.check_interval = risk_cfg.get("check_interval_seconds", 30)
        self.telegram_alerts = risk_cfg.get("telegram_alerts", True)

        # Category exposure limits (cents) — default $20 each ($60 balance)
        cat_limits = risk_cfg.get("category_limits", {})
        self.category_limits = {
            "sports": cat_limits.get("sports_cents", 2000),
            "esports": cat_limits.get("esports_cents", 1500),
            "crypto": cat_limits.get("crypto_cents", 2000),
            "crypto_strikes": cat_limits.get("crypto_strikes_cents", 1500),
            "finance": cat_limits.get("finance_cents", 1500),
            "politics": cat_limits.get("politics_cents", 1500),
            "weather": cat_limits.get("weather_cents", 1000),
            "other": cat_limits.get("other_cents", 1000),
        }

        # Category exposure tracking (reset daily)
        self._category_exposure: dict[str, int] = defaultdict(int)

        # Active-exposure cap (real cash currently committed across all open positions).
        # Reconciled from Kalshi's portfolio every check_interval — resting/cancelled orders
        # don't count, fills do. This is the meaningful "money at risk on Kalshi" gate.
        self.max_active_exposure_cents = risk_cfg.get("max_active_exposure_cents", 8000)  # $80

        # Daily buy-notional cap (anti-churn only — catches runaway placement loops,
        # not exposure). Higher default than active-exposure since unfilled placements
        # are cheap; we just don't want thousands per day.
        self.max_daily_volume_cents = risk_cfg.get("max_daily_volume_cents", 50000)  # $500
        self._daily_volume_cents: int = 0

        # Ticker-prefix allow-list — if non-empty, orders against other prefixes are blocked
        allowed = risk_cfg.get("allowed_ticker_prefixes", [
            "KXNBA", "KXNHL", "KXMLB", "KXUCL", "KXUFC", "KXATP", "KXWTA",
            "KXLOL", "KXNBAGAME", "KXNBATOTAL", "KXMLBGAME", "KXMLBTOTAL",
            "KXNHLGAME", "KXUCLGAME", "KXUFCFIGHT", "KXATPMATCH", "KXWTAMATCH",
            "KXLOLGAME",
            "KXBTC", "KXBTCD", "KXETH", "KXETHD",
        ])
        self.allowed_ticker_prefixes = tuple(allowed) if allowed else ()

        # State
        self._tripped = False
        self._tripped_at: Optional[str] = None
        self._trip_reason: Optional[str] = None
        self._trip_pnl_cents: int = 0
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_count = 0
        self._last_checked: Optional[str] = None
        self._day_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Audit counters
        self._audits_approved = 0
        self._audits_rejected = 0
        self._audits_adjusted = 0

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Risk manager started: max daily loss=${self.max_daily_loss_cents/100:.2f}, "
            f"check every {self.check_interval}s"
        )
        return self._task

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run_loop(self):
        while self._running:
            try:
                self._check_count += 1
                await self._check()
            except Exception as e:
                logger.error(f"Risk manager check error: {e}")
            await asyncio.sleep(self.check_interval)

    async def _check(self):
        """Check aggregate P&L and trip if threshold exceeded."""
        self._last_checked = datetime.now(timezone.utc).isoformat()

        # Reset on new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start:
            self._day_start = today
            self._category_exposure.clear()
            self._daily_volume_cents = 0
            if self._tripped:
                logger.info("New day — auto-resetting circuit breaker")
                self._tripped = False
                self._tripped_at = None
                self._trip_reason = None
                self._trip_pnl_cents = 0

        # Reconcile category exposure from live Kalshi positions every cycle.
        # Without this, _category_exposure drifts: record_order increments on placement
        # but never decrements on cancel/close/fill, so the counter saturates over a
        # session of spread-bot quoting and starts blocking otherwise-valid orders.
        await self._reconcile_category_exposure()

        if self._tripped:
            return  # Already halted, waiting for reset

        total_pnl = self._get_aggregate_pnl()

        if total_pnl <= -self.max_daily_loss_cents:
            await self._trip(total_pnl)

    async def _reconcile_category_exposure(self):
        """Replace `_category_exposure` with the cost-basis sum of live Kalshi positions.

        Source of truth is Kalshi's portfolio summary (open positions only). Resting
        orders that haven't filled aren't counted as exposure — they reserve cash on
        Kalshi's side but represent no at-risk principal here.
        """
        try:
            from app.services.kalshi_client import get_kalshi_client, AsyncKalshiClient
            kcli = get_kalshi_client()
            if not kcli or not getattr(kcli, "enabled", False):
                return
            akc = AsyncKalshiClient(kcli)
            summary = await akc.get_portfolio_summary()
        except Exception as e:
            logger.debug(f"Risk manager reconcile: portfolio fetch failed ({e})")
            return

        positions = (summary or {}).get("positions", []) or []
        new_exposure: dict[str, int] = defaultdict(int)
        for p in positions:
            count = int(p.get("position", 0) or 0)
            if count == 0:
                continue
            cost_cents = abs(int(p.get("total_traded_cost_cents", 0) or 0))
            if cost_cents == 0:
                continue
            cat = detect_category(p.get("ticker", "") or "", p.get("title", "") or "")
            new_exposure[cat] += cost_cents

        # Atomic replace, then log only meaningful drift (>$1) so this is auditable.
        old = dict(self._category_exposure)
        self._category_exposure = new_exposure
        for cat in set(old.keys()) | set(new_exposure.keys()):
            old_val = old.get(cat, 0)
            new_val = new_exposure.get(cat, 0)
            if abs(old_val - new_val) >= 100:
                logger.info(
                    f"Category '{cat}' exposure reconciled: "
                    f"${old_val/100:.2f} → ${new_val/100:.2f} (live positions)"
                )

    def _get_aggregate_pnl(self) -> int:
        """Sum P&L cents across all trading bots + DB-recorded positions."""
        total = 0

        # Market Maker (in-memory P&L)
        try:
            from app.services.kalshi_market_maker import get_market_maker
            total += get_market_maker()._total_pnl_cents
        except Exception:
            pass

        # Spread Bot (in-memory P&L)
        try:
            from app.services.kalshi_spread_bot import get_spread_bot
            total += get_spread_bot()._total_pnl_cents
        except Exception:
            pass

        # Technical Bot (in-memory P&L)
        try:
            from app.services.kalshi_technical_bot import get_technical_bot
            total += get_technical_bot()._total_pnl_cents
        except Exception:
            pass

        # DB-recorded Kalshi positions (covers AI agent + any manual trades)
        try:
            from app.database import get_kalshi_stats
            db_stats = get_kalshi_stats()
            db_pnl = db_stats.get("total_pnl_cents", 0) or 0
            total += db_pnl
        except Exception:
            pass

        return total

    async def _trip(self, pnl_cents: int):
        """Trip the circuit breaker — kill all bots."""
        self._tripped = True
        self._tripped_at = datetime.now(timezone.utc).isoformat()
        self._trip_pnl_cents = pnl_cents
        self._trip_reason = (
            f"Daily loss ${abs(pnl_cents)/100:.2f} exceeded "
            f"limit ${self.max_daily_loss_cents/100:.2f}"
        )
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {self._trip_reason}")

        # Kill all bots
        killed = []

        try:
            from app.services.kalshi_market_maker import get_market_maker
            mm = get_market_maker()
            if mm._running:
                mm.kill()
                killed.append("Market Maker")
        except Exception:
            pass

        try:
            from app.services.kalshi_spread_bot import get_spread_bot
            sb = get_spread_bot()
            if sb._running:
                sb.kill()
                killed.append("Spread Bot")
        except Exception:
            pass

        try:
            from app.services.kalshi_technical_bot import get_technical_bot
            tb = get_technical_bot()
            if tb._running:
                tb.stop()
                killed.append("Technical Bot")
        except Exception:
            pass

        try:
            from app.services.kalshi_ai_agent import get_ai_agent_bot
            ai = get_ai_agent_bot()
            if ai._running:
                ai.stop()
                killed.append("AI Agent")
        except Exception:
            pass

        try:
            from app.services.kalshi_arbitrage import get_arbitrage_scanner
            arb = get_arbitrage_scanner()
            if arb._running:
                arb.stop()
                killed.append("Arb Scanner")
        except Exception:
            pass

        try:
            from app.services.kalshi_sports_scanner import get_sports_scanner
            sports = get_sports_scanner()
            if sports._running:
                sports.stop()
                killed.append("Sports")
        except Exception:
            pass

        try:
            from app.services.kalshi_esports_scanner import get_esports_scanner
            esports = get_esports_scanner()
            if esports._running:
                esports.stop()
                killed.append("Esports")
        except Exception:
            pass

        logger.critical(f"Killed bots: {', '.join(killed) or 'none running'}")

        # Emergency flatten — cancel all orders and sell all positions
        flatten_result = await self._emergency_flatten()

        # Telegram alert
        flatten_summary = flatten_result.get("summary", "none")
        await self._alert(
            f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n\n"
            f"<b>Reason:</b> {self._trip_reason}\n"
            f"<b>Aggregate P&L:</b> ${pnl_cents/100:+.2f}\n"
            f"<b>Bots killed:</b> {', '.join(killed) or 'none'}\n"
            f"<b>Emergency flatten:</b> {flatten_summary}\n"
            f"<b>Time:</b> {self._tripped_at}\n\n"
            f"All Kalshi trading is HALTED.\n"
            f"Use /kalshi_reset or the dashboard to resume."
        )

    async def _emergency_flatten(self) -> dict:
        """Cancel all open orders and sell all positions to stop bleeding."""
        orders_cancelled = 0
        positions_closed = 0
        errors = []

        try:
            from app.services.kalshi_client import get_async_kalshi_client
            client = get_async_kalshi_client()

            # 1. Cancel all open/resting orders
            try:
                open_orders = await client.get_open_orders()
                for order in open_orders:
                    order_id = order.get("order_id")
                    if order_id:
                        try:
                            await client.cancel_order(order_id)
                            orders_cancelled += 1
                        except Exception as e:
                            errors.append(f"cancel {order_id}: {e}")
                logger.info(f"Emergency flatten: cancelled {orders_cancelled} orders")
            except Exception as e:
                errors.append(f"list orders: {e}")
                logger.error(f"Emergency flatten: failed to list orders: {e}")

            # 2. Sell all open positions at market (aggressive pricing)
            try:
                positions = await client.get_positions()
                for pos in positions:
                    ticker = pos.get("ticker", "")
                    count = pos.get("position", 0)
                    if count == 0 or not ticker:
                        continue

                    try:
                        if count > 0:
                            # Long YES position — sell YES at 1c (aggressive exit)
                            await client.place_order(
                                ticker=ticker, side="yes", action="sell",
                                yes_price=1, count=count, order_type="limit",
                            )
                        else:
                            # Long NO position — sell NO at 1c (aggressive exit)
                            await client.place_order(
                                ticker=ticker, side="no", action="sell",
                                no_price=1, count=abs(count), order_type="limit",
                            )
                        positions_closed += 1
                    except Exception as e:
                        errors.append(f"close {ticker}: {e}")
                logger.info(f"Emergency flatten: closed {positions_closed} positions")
            except Exception as e:
                errors.append(f"list positions: {e}")
                logger.error(f"Emergency flatten: failed to list positions: {e}")

        except Exception as e:
            errors.append(f"client init: {e}")
            logger.error(f"Emergency flatten: client error: {e}")

        summary = f"{orders_cancelled} orders cancelled, {positions_closed} positions closed"
        if errors:
            summary += f" ({len(errors)} errors)"
            logger.error(f"Emergency flatten errors: {errors}")

        return {"orders_cancelled": orders_cancelled, "positions_closed": positions_closed,
                "errors": errors, "summary": summary}

    def check_order(self, order_cost_cents: int, bot_name: str = "unknown",
                    ticker: str = "", title: str = "") -> dict:
        """Pre-trade gate: returns {"allowed": True} or {"allowed": False, "reason": ...}.

        Checks:
        1. Circuit breaker not tripped
        2. Current P&L + order cost wouldn't breach daily loss limit
        3. Category exposure limit not exceeded
        """
        if self._tripped:
            return {
                "allowed": False,
                "reason": f"Circuit breaker tripped: {self._trip_reason}",
            }

        # Ticker allow-list gate (strategy scope)
        if ticker and self.allowed_ticker_prefixes:
            if not ticker.upper().startswith(self.allowed_ticker_prefixes):
                reason = f"Ticker '{ticker}' not in allow-list"
                logger.warning(f"[{bot_name}] {reason}")
                return {"allowed": False, "reason": reason}

        # Active-exposure cap (real cash committed in open Kalshi positions; reconciled).
        active_exposure = sum(self._category_exposure.values())
        if active_exposure + order_cost_cents > self.max_active_exposure_cents:
            reason = (
                f"Active exposure cap: ${(active_exposure + order_cost_cents)/100:.2f} "
                f"would exceed ${self.max_active_exposure_cents/100:.2f} "
                f"(committed: ${active_exposure/100:.2f}, order: ${order_cost_cents/100:.2f})"
            )
            logger.warning(f"[{bot_name}] {reason}")
            return {"allowed": False, "reason": reason}

        # Daily volume cap (anti-churn — catches runaway placement loops)
        if self._daily_volume_cents + order_cost_cents > self.max_daily_volume_cents:
            reason = (
                f"Daily volume cap: ${(self._daily_volume_cents + order_cost_cents)/100:.2f} "
                f"would exceed ${self.max_daily_volume_cents/100:.2f} "
                f"(placed today: ${self._daily_volume_cents/100:.2f}, order: ${order_cost_cents/100:.2f})"
            )
            logger.warning(f"[{bot_name}] {reason}")
            return {"allowed": False, "reason": reason}

        # Global daily loss check
        current_pnl = self._get_aggregate_pnl()
        projected_loss = current_pnl - order_cost_cents

        if projected_loss <= -self.max_daily_loss_cents:
            reason = (
                f"Order blocked: worst-case loss ${abs(projected_loss)/100:.2f} "
                f"would breach daily limit ${self.max_daily_loss_cents/100:.2f} "
                f"(current P&L: ${current_pnl/100:+.2f}, order cost: ${order_cost_cents/100:.2f})"
            )
            logger.warning(f"[{bot_name}] {reason}")
            return {"allowed": False, "reason": reason}

        # Category exposure check
        if ticker:
            category = detect_category(ticker, title)
            cat_limit = self.category_limits.get(category, 1000)
            cat_current = self._category_exposure.get(category, 0)

            if cat_current + order_cost_cents > cat_limit:
                reason = (
                    f"Category '{category}' limit: exposure ${(cat_current + order_cost_cents)/100:.2f} "
                    f"would exceed limit ${cat_limit/100:.2f} "
                    f"(current: ${cat_current/100:.2f}, order: ${order_cost_cents/100:.2f})"
                )
                logger.warning(f"[{bot_name}] {reason}")
                return {"allowed": False, "reason": reason, "category": category}

        return {"allowed": True}

    def audit_trade(self, ticker: str, side: str, price_cents: int,
                    count: int, confidence: float = 0.5,
                    bot_name: str = "unknown", title: str = "") -> dict:
        """Rule-based risk auditor for any bot's proposed trade.

        Returns {"approved": True/False, "reason": ..., "flags": [...], "adjustments": {...}}
        Runs fast (no LLM call) — checks liquidity, spread, concentration, and signal quality.
        """
        flags = []
        adjustments = {}

        def _reject(reason, reject_flags):
            self._audits_rejected += 1
            return {"approved": False, "reason": reason, "flags": reject_flags, "adjustments": {}}

        # 1. Basic check_order gate (circuit breaker + category limits)
        cost = price_cents * count
        gate = self.check_order(cost, bot_name=bot_name, ticker=ticker, title=title)
        if not gate["allowed"]:
            return _reject(gate["reason"], ["gate_blocked"])

        # 2. Dead zone check — prices near 50¢ have minimal edge
        if 42 <= price_cents <= 58:
            flags.append("near_dead_zone")
            if 45 <= price_cents <= 55:
                return _reject(f"Price {price_cents}¢ in dead zone (45-55)", flags)

        # 3. Low confidence filter
        if confidence < 0.3:
            return _reject(f"Confidence {confidence:.0%} too low", ["low_confidence"])

        # 4. Liquidity check — cap size to book depth + balance
        max_size = self.get_max_size(ticker, price_cents=price_cents, side=side)
        if max_size is not None and count > max_size:
            adjustments["count"] = max_size
            flags.append(f"size_capped_{count}_to_{max_size}")

        # 5. Category concentration — warn if >60% of daily budget in one category
        category = detect_category(ticker, title)
        cat_limit = self.category_limits.get(category, 1000)
        cat_used = self._category_exposure.get(category, 0)
        if cat_limit > 0 and (cat_used + cost) / cat_limit > 0.6:
            flags.append("high_category_concentration")

        # 6. Aggregate P&L check — reduce size if already losing today
        current_pnl = self._get_aggregate_pnl()
        if current_pnl < -self.max_daily_loss_cents * 0.5:
            # Already lost >50% of daily limit — halve position size
            reduced = max(1, (adjustments.get("count", count)) // 2)
            adjustments["count"] = reduced
            flags.append("drawdown_reduction")

        adjusted_count = adjustments.get("count", count)
        if adjusted_count != count:
            logger.info(
                f"[{bot_name}] Risk audit adjusted {ticker}: "
                f"{count}→{adjusted_count} contracts, flags={flags}"
            )

        self._audits_approved += 1
        if adjustments:
            self._audits_adjusted += 1
        return {
            "approved": True,
            "reason": "passed" if not flags else f"approved with flags: {', '.join(flags)}",
            "flags": flags,
            "adjustments": adjustments,
        }

    def record_order(self, ticker: str, cost_cents: int, title: str = ""):
        """Record an executed order's cost against its category budget and daily volume."""
        category = detect_category(ticker, title)
        self._category_exposure[category] += cost_cents
        self._daily_volume_cents += cost_cents

    def get_max_size(self, ticker: str, price_cents: int = 0,
                     side: str = "yes") -> Optional[int]:
        """Liquidity-based position sizing: returns max contracts for this market.

        Uses three constraints and returns the tightest one:
        1. Book fraction — never take more than 10% of visible depth
        2. Price impact — only consume liquidity at or near the target price
        3. Balance cap — never risk more than 15% of account balance per trade

        Falls back to tier-based sizing if WS data is unavailable.
        """
        limits = []

        # ── 1. Orderbook-based sizing ──
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            ob = ws.get_orderbook(ticker)
            if ob and ob.last_updated:
                yes_depth_dollars = sum(ob.yes_levels.values())
                no_depth_dollars = sum(ob.no_levels.values())
                total_contracts = int((yes_depth_dollars + no_depth_dollars) * 2)

                # 1a. Fraction of total book depth
                fraction_limit = max(1, int(total_contracts * MAX_BOOK_FRACTION))
                limits.append(fraction_limit)

                # 1b. Available liquidity at/near target price (price impact)
                if price_cents > 0:
                    available = self._depth_near_price(ob, side, price_cents)
                    if available > 0:
                        limits.append(available)

                # 1c. Tier-based floor (legacy fallback)
                for min_depth, max_size in LIQUIDITY_TIERS:
                    if total_contracts >= min_depth:
                        if max_size is not None:
                            limits.append(max_size)
                        break

                logger.debug(
                    f"Liquidity sizing {ticker}: depth≈{total_contracts} contracts "
                    f"(${yes_depth_dollars+no_depth_dollars:.1f}), "
                    f"limits={limits}"
                )
        except Exception:
            pass

        # ── 2. Balance-based sizing ──
        try:
            from app.services.kalshi_client import get_async_kalshi_client
            client = get_async_kalshi_client()
            balance = client._sync.get_balance().get("balance", 0)
            if balance > 0 and price_cents > 0:
                max_cost = int(balance * MAX_BALANCE_FRACTION)
                balance_limit = max(1, max_cost // max(price_cents, 1))
                limits.append(balance_limit)
                logger.debug(
                    f"Balance sizing {ticker}: balance={balance}¢, "
                    f"max_cost={max_cost}¢ @{price_cents}¢ → {balance_limit} contracts"
                )
        except Exception:
            pass

        if limits:
            return min(limits)

        # Fallback: no data at all, be conservative
        return 5

    @staticmethod
    def _depth_near_price(ob, side: str, price_cents: int, slippage_cents: int = 3) -> int:
        """Count contracts available within slippage_cents of the target price.

        Returns the number of contracts we could fill without walking the book
        beyond an acceptable price impact.
        """
        levels = ob.yes_levels if side == "yes" else ob.no_levels
        target_dollars = price_cents / 100.0
        slip_dollars = slippage_cents / 100.0
        total = 0.0
        for price_str, qty_dollars in levels.items():
            level_price = float(price_str)
            if side == "yes" and level_price <= target_dollars + slip_dollars:
                total += qty_dollars
            elif side == "no" and level_price <= target_dollars + slip_dollars:
                total += qty_dollars
        # Convert dollar depth to approximate contract count
        avg_price = max(target_dollars, 0.05)
        return max(1, int(total / avg_price))

    def reset(self) -> dict:
        """Manually reset the circuit breaker to resume trading."""
        if not self._tripped:
            return {"reset": False, "message": "Circuit breaker is not tripped"}

        old_reason = self._trip_reason
        self._tripped = False
        self._tripped_at = None
        self._trip_reason = None
        self._trip_pnl_cents = 0
        logger.warning(f"Circuit breaker RESET manually (was: {old_reason})")
        return {"reset": True, "message": f"Circuit breaker reset. Previous trip: {old_reason}"}

    async def _alert(self, message: str):
        if not self.telegram_alerts:
            return
        try:
            from app.services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(message)
        except Exception:
            pass

    def get_status(self) -> dict:
        cat_status = {}
        for cat, limit in self.category_limits.items():
            used = self._category_exposure.get(cat, 0)
            cat_status[cat] = {
                "limit_cents": limit,
                "used_cents": used,
                "remaining_cents": limit - used,
                "usage_pct": round(used / limit * 100, 1) if limit > 0 else 0,
            }

        return {
            "enabled": self.enabled,
            "running": self._running,
            "tripped": self._tripped,
            "tripped_at": self._tripped_at,
            "trip_reason": self._trip_reason,
            "trip_pnl_cents": self._trip_pnl_cents,
            "max_daily_loss_cents": self.max_daily_loss_cents,
            "max_daily_loss_usd": round(self.max_daily_loss_cents / 100, 2),
            "max_active_exposure_cents": self.max_active_exposure_cents,
            "active_exposure_cents": sum(self._category_exposure.values()),
            "active_exposure_remaining_cents": max(0, self.max_active_exposure_cents - sum(self._category_exposure.values())),
            "max_daily_volume_cents": self.max_daily_volume_cents,
            "daily_volume_cents": self._daily_volume_cents,
            "daily_volume_remaining_cents": max(0, self.max_daily_volume_cents - self._daily_volume_cents),
            "allowed_ticker_prefixes": list(self.allowed_ticker_prefixes),
            "current_pnl_cents": self._get_aggregate_pnl(),
            "check_count": self._check_count,
            "last_checked": self._last_checked,
            "day": self._day_start,
            "categories": cat_status,
            "auditor": {
                "approved": self._audits_approved,
                "rejected": self._audits_rejected,
                "adjusted": self._audits_adjusted,
            },
        }


# Singleton
_risk_manager: Optional[KalshiRiskManager] = None


def get_risk_manager() -> KalshiRiskManager:
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = KalshiRiskManager()
    return _risk_manager
