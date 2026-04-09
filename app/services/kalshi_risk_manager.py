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

# Ticker prefix → category mapping
TICKER_CATEGORY_MAP = {
    "KXMLB": "sports", "KXNBA": "sports", "KXNFL": "sports", "KXNHL": "sports",
    "KXWNBA": "sports", "KXUFC": "sports", "KXNCAA": "sports", "KXATP": "sports",
    "KXWTA": "sports", "KXSOCCER": "sports", "KXARG": "sports",
    "KXCS2": "esports", "KXDOTA": "esports", "KXLOL": "esports",
    "KXVALORANT": "esports", "KXOVERWATCH": "esports", "KXMVE": "esports",
    "KXBTC": "crypto", "KXETH": "crypto", "KXSOL": "crypto",
    "KXFED": "finance", "KXCPI": "finance", "KXGDP": "finance",
}

# Title keyword fallback
TITLE_CATEGORY_KEYWORDS = {
    "sports": ["mlb", "nba", "nfl", "nhl", "soccer", "ufc", "wnba", "tennis",
               "game", "match", "playoff", "championship", "super bowl", "world series"],
    "esports": ["cs2", "dota", "lol", "valorant", "overwatch", "esport"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"],
    "finance": ["fed ", "interest rate", "gdp", "cpi", "inflation", "treasury", "earnings"],
    "politics": ["election", "trump", "biden", "president", "senate", "congress", "vote"],
    "weather": ["hurricane", "temperature", "weather", "tornado", "storm"],
}


def detect_category(ticker: str, title: str = "") -> str:
    """Detect market category from ticker prefix or title keywords."""
    ticker_upper = ticker.upper()
    for prefix, cat in TICKER_CATEGORY_MAP.items():
        if ticker_upper.startswith(prefix):
            return cat

    title_lower = title.lower()
    for cat, keywords in TITLE_CATEGORY_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return cat

    return "other"


# ─── Liquidity Sizing ────────────────────────────────────────────────────────

# Depth thresholds: (min_total_depth_contracts, max_order_size)
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
            "finance": cat_limits.get("finance_cents", 1500),
            "politics": cat_limits.get("politics_cents", 1500),
            "weather": cat_limits.get("weather_cents", 1000),
            "other": cat_limits.get("other_cents", 1000),
        }

        # Category exposure tracking (reset daily)
        self._category_exposure: dict[str, int] = defaultdict(int)

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
            if self._tripped:
                logger.info("New day — auto-resetting circuit breaker")
                self._tripped = False
                self._tripped_at = None
                self._trip_reason = None
                self._trip_pnl_cents = 0

        if self._tripped:
            return  # Already halted, waiting for reset

        total_pnl = self._get_aggregate_pnl()

        if total_pnl <= -self.max_daily_loss_cents:
            await self._trip(total_pnl)

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

        # 4. Liquidity check — cap size to book depth
        max_size = self.get_max_size(ticker)
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
        """Record an executed order's cost against its category budget."""
        category = detect_category(ticker, title)
        self._category_exposure[category] += cost_cents

    def get_max_size(self, ticker: str) -> Optional[int]:
        """Liquidity-based position sizing: returns max contracts for this market.

        Reads orderbook depth from WS cache (or returns conservative default).
        Thin books get smaller orders to avoid moving the market.
        Dollar depth is converted to approximate contract count at ~$0.50 avg price.
        """
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            ob = ws.get_orderbook(ticker)
            if ob and ob.last_updated:
                # Depth values are in dollars — convert to approx contract count
                # (1 contract ≈ $0.01–$0.99, use $0.50 avg → 2 contracts per $1)
                yes_depth_dollars = sum(ob.yes_levels.values())
                no_depth_dollars = sum(ob.no_levels.values())
                total_contracts = int((yes_depth_dollars + no_depth_dollars) * 2)

                for min_depth, max_size in LIQUIDITY_TIERS:
                    if total_contracts >= min_depth:
                        logger.debug(
                            f"Liquidity sizing {ticker}: depth≈{total_contracts} contracts "
                            f"(${yes_depth_dollars+no_depth_dollars:.1f}), max_size={max_size}"
                        )
                        return max_size  # None means "use bot's configured max"
                logger.debug(f"Liquidity sizing {ticker}: very thin ({total_contracts} contracts), capping at 5")
                return 5  # Less than minimum tier
        except Exception:
            pass

        # Fallback: no WS data, be conservative
        return 20

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
