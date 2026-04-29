"""Kalshi Spread Bot — Automated market-making on prediction markets.

Places resting limit orders on both YES and NO sides of a market,
capturing the bid-ask spread when both sides fill. Manages inventory
risk by skewing quotes away from accumulated positions.

How it works:
1. Estimate fair value from orderbook midpoint + recent trade VWAP
2. Place YES bid below fair value, NO bid below (100 - fair value)
3. When one side fills, skew the other side's price to reduce inventory
4. Cancel and replace stale orders when the market moves
5. Flatten inventory before market close or when max exposure hit

Risk controls:
- Max inventory per market (contracts on one side)
- Max total exposure across all markets (cents)
- Inventory skew: widen spread on heavy side, tighten on light side
- Auto-flatten: close out positions near market close time
- Kill switch: stop all quoting and cancel orders on error
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import get
from app.services.kalshi_market_maker import _empirical_edge_score

logger = logging.getLogger("bot.kalshi.spread")


class MarketState:
    """Tracks the state of a single market being quoted."""

    def __init__(self, ticker: str, title: str = ""):
        self.ticker = ticker
        self.title = title
        # Orderbook
        self.yes_bid: int = 0
        self.yes_ask: int = 0
        self.no_bid: int = 0
        self.no_ask: int = 0
        self.mid_price: float = 50.0
        # Inventory (unpaired only after _check_spread_capture nets matched legs)
        self.yes_position: int = 0  # net YES contracts held
        self.no_position: int = 0   # net NO contracts held
        self.yes_avg_entry: float = 0.0  # avg cost basis of unpaired YES
        self.no_avg_entry: float = 0.0   # avg cost basis of unpaired NO
        # Active orders
        self.yes_order_id: Optional[str] = None
        self.no_order_id: Optional[str] = None
        self.yes_order_price: int = 0
        self.no_order_price: int = 0
        # Stats
        self.fills_yes: int = 0
        self.fills_no: int = 0
        self.total_spread_captured_cents: int = 0
        self.realized_pnl_cents: int = 0  # actual exits (TP/SL) — not the virtual spread counter
        self.last_updated: Optional[str] = None
        self.errors: int = 0
        self.rejection_cooldown: int = 0  # Skip N cycles after 422 rejection

    def net_inventory(self) -> int:
        """Positive = long YES, negative = long NO."""
        return self.yes_position - self.no_position

    def total_exposure_cents(self) -> int:
        """Total cents at risk across both sides."""
        return (self.yes_position * self.yes_order_price +
                self.no_position * self.no_order_price)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "no_bid": self.no_bid,
            "no_ask": self.no_ask,
            "mid_price": self.mid_price,
            "yes_position": self.yes_position,
            "no_position": self.no_position,
            "yes_avg_entry": round(self.yes_avg_entry, 2),
            "no_avg_entry": round(self.no_avg_entry, 2),
            "net_inventory": self.net_inventory(),
            "yes_order_id": self.yes_order_id,
            "no_order_id": self.no_order_id,
            "yes_order_price": self.yes_order_price,
            "no_order_price": self.no_order_price,
            "fills_yes": self.fills_yes,
            "fills_no": self.fills_no,
            "spread_captured_cents": self.total_spread_captured_cents,
            "realized_pnl_cents": self.realized_pnl_cents,
            "last_updated": self.last_updated,
            "errors": self.errors,
        }


class KalshiSpreadBot:
    """Market-making spread bot for Kalshi prediction markets."""

    def __init__(self):
        cfg = get("kalshi") or {}
        spread_cfg = cfg.get("spread_bot", {})

        self.enabled = spread_cfg.get("enabled", False)
        self.poll_interval = spread_cfg.get("poll_interval_seconds", 15)
        self.default_spread_cents = spread_cfg.get("default_spread_cents", 4)
        self.min_spread_cents = spread_cfg.get("min_spread_cents", 2)
        self.contracts_per_side = spread_cfg.get("contracts_per_side", 5)
        self.max_inventory_per_market = spread_cfg.get("max_inventory_per_market", 20)
        self.max_total_exposure_cents = spread_cfg.get("max_total_exposure_cents", 2000)
        self.inventory_skew_cents = spread_cfg.get("inventory_skew_cents", 2)
        self.stale_order_threshold_cents = spread_cfg.get("stale_order_threshold_cents", 3)
        self.flatten_minutes_before_close = spread_cfg.get("flatten_minutes_before_close", 30)
        self.fee_per_contract_cents = spread_cfg.get("fee_per_contract_cents", 2)
        self.max_days_to_close = spread_cfg.get("max_days_to_close", 0)
        self.telegram_alerts = spread_cfg.get("telegram_alerts", True)
        self.target_tickers = spread_cfg.get("target_tickers", [])
        # Exit rules for unpaired inventory (added 2026-04-27)
        self.take_profit_cents = spread_cfg.get("take_profit_cents", 88)
        self.stop_loss_cents = spread_cfg.get("stop_loss_cents", 15)
        self.exit_telegram_alerts = spread_cfg.get("exit_telegram_alerts", True)
        # Hour-of-day gating (Becker 2026-04-28: maker edge ~50% larger overnight/evening
        # vs US business hours). Default: quote top-3 in active windows, top-1 during the
        # 8-15 ET efficient window. Set quiet_hours_et=[] to disable gating.
        self.active_max_markets = spread_cfg.get("max_markets", 3)
        self.quiet_hours_et = set(spread_cfg.get("quiet_hours_et", [8, 9, 10, 11, 12, 13, 14, 15]))
        self.quiet_max_markets = spread_cfg.get("quiet_hours_max_markets", 1)

        # Runtime state
        self._markets: dict[str, MarketState] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._kill_switch = False
        self._total_pnl_cents = 0
        self._cycle_count = 0
        self._start_time: Optional[str] = None

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def start(self) -> asyncio.Task:
        """Start the spread bot."""
        if self._task and not self._task.done():
            logger.warning("Spread bot already running")
            return self._task

        self._running = True
        self._kill_switch = False
        self._start_time = datetime.utcnow().isoformat()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Spread bot started: spread={self.default_spread_cents}¢, "
            f"contracts={self.contracts_per_side}, "
            f"targets={self.target_tickers or 'auto-select'}"
        )
        return self._task

    async def stop(self):
        """Stop the bot, cancel all resting orders."""
        self._running = False
        logger.info("Spread bot stopping — cancelling all orders...")
        await self._cancel_all_orders()
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Spread bot stopped")

    def kill(self):
        """Emergency kill switch — stops all activity immediately."""
        self._kill_switch = True
        self._running = False
        logger.warning("KILL SWITCH activated — spread bot halted")

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    async def _run_loop(self):
        """Main polling loop."""
        # Cancel any orphaned orders from a previous run
        try:
            await self._cancel_all_orders()
            logger.info("Spread bot: cleaned up orphaned orders on startup")
        except Exception as e:
            logger.warning(f"Spread bot startup cleanup failed: {e}")

        while self._running and not self._kill_switch:
            try:
                self._cycle_count += 1
                await self._cycle()
            except Exception as e:
                logger.error(f"Spread bot cycle error: {e}")
                # After 5 consecutive errors, activate kill switch
                consecutive_errors = sum(
                    1 for m in self._markets.values() if m.errors > 0
                )
                if consecutive_errors >= 5:
                    self.kill()
                    await self._alert(
                        "KILL SWITCH: Spread bot halted after 5 consecutive errors. "
                        f"Last error: {e}"
                    )
                    return

            await asyncio.sleep(self.poll_interval)

    async def _cycle(self):
        """Single market-making cycle: update state, requote, manage inventory."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        # 1. Select target markets
        tickers = self.target_tickers
        if not tickers:
            tickers = await self._auto_select_markets(client)

        # Auto-subscribe WS feed to selected markets
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            if ws.enabled:
                for t in tickers:
                    await ws.subscribe(t)
        except Exception:
            pass

        # 2. Update state and requote each market
        for ticker in tickers:
            try:
                if ticker not in self._markets:
                    market = await client.get_market(ticker)
                    self._markets[ticker] = MarketState(
                        ticker=ticker,
                        title=market.get("title", ticker),
                    )

                state = self._markets[ticker]
                await self._update_market_state(client, state)
                await self._check_fills(client, state)
                await self._check_exits(client, state)

                # Check total exposure
                total_exposure = sum(
                    m.total_exposure_cents() for m in self._markets.values()
                )
                if total_exposure >= self.max_total_exposure_cents:
                    logger.warning(
                        f"Max total exposure reached ({total_exposure}¢), skipping new quotes"
                    )
                    continue

                await self._requote(client, state)
                state.errors = 0

            except Exception as e:
                logger.error(f"Error on {ticker}: {e}")
                if ticker in self._markets:
                    self._markets[ticker].errors += 1

    # =========================================================================
    # MARKET SELECTION
    # =========================================================================

    # Categories with low maker edge (Becker dataset 2026-04-28: 0.17pp gap)
    LOW_EDGE_KEYWORDS = ["finance", "fed ", "federal reserve", "interest rate",
                         "gdp", "cpi", "inflation", "treasury", "earnings"]
    LOW_EDGE_PREFIXES = ("KXFED", "KXCPI", "KXGDP", "KXINX", "KXINXD",
                         "KXNDX", "KXSPY", "KXDJI")
    # Maker-edge keywords ranked by Becker 2026-04-28 per-category gap.
    # Top tier (4-7pp): world events, media, entertainment, science/tech
    # Mid tier (2-3pp): crypto, weather, sports
    # Politics (1pp) intentionally NOT here — modest edge.
    HIGH_EDGE_KEYWORDS = ["world", "war", "conflict", "geopolit",
                          "media", "press", "news",
                          "entertainment", "celebrity", "movie", "tv ",
                          "award", "oscar", "grammy",
                          "science", "tech", "ai ", "space",
                          "weather", "hurricane", "tornado",
                          "sports", "super bowl", "world series",
                          "playoff", "championship",
                          "crypto", "bitcoin", "ethereum"]
    # Ticker-prefix matching for high-edge series (catches markets whose
    # titles wouldn't match the keyword list above — e.g., KXNOBELPEACE,
    # KXEPSTEIN). Aligned with Becker classifier.
    HIGH_EDGE_PREFIXES = (
        "KXNOBEL", "KXNEXTPOPE", "KXPOPE", "KXEPSTEIN", "KXOTEEPSTEIN",
        "KXZELENSKYYPUTINMEET", "KXBOLIVIAPRES", "KXSKPRES",
        "KXLAGODAYS", "KXARREST",
        "KXMENTION", "KXHEADLINE", "KXGOOGLESEARCH", "KX538APPROVE", "KXAPRPOTUS",
        "KXOSCAR", "KXGRAMMY", "KXEMMY", "KXBAFTA", "KXGAMEAWARDS",
        "KXSPOTIFY", "KXNETFLIX", "KXRT", "KXTOPSONG", "KXTOPALBUM", "KXTOPARTIST",
        "KXBILLBOARD",
        "KXLLM", "KXAI", "KXSPACEX", "KXALIENS", "KXAPPLE",
        "KXHIGH", "KXRAIN", "KXSNOW", "KXTORNADO", "KXHURCAT", "KXARCTICICE", "KXWEATHER",
        "KXBTCMAX", "KXBTCMIN", "KXETHMAX", "KXETHMIN", "KXBTCRESERVE",
    )

    async def _auto_select_markets(self, client) -> list[str]:
        """Auto-select the best markets for spread-capturing.

        Empirical scoring (Becker dataset 2026-04-28, mispricing_by_price.csv):
        - True dead zone is 41-50¢ (+0.16pp). 51-60¢ is +2.40pp — best band.
        - Best maker bands: 51-60¢ (+2.40pp), 31-40¢ (+2.14pp), 81-90¢ (+1.93pp)
        - Drop "always prefer 1-20¢ tails" — 1-15¢ is weak (+0.06–0.48pp)
        - Avoid finance (0.17pp) and 21-25¢ (negative maker edge)
        """
        markets = await client.discover_active_markets(min_volume=10, max_days_to_close=self.max_days_to_close)
        candidates = []

        for m in markets:
            yes_bid = int(round(float(m.get("yes_bid_dollars", "0") or "0") * 100))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            volume = int(float(m.get("volume_fp", "0") or "0"))

            if yes_bid <= 0 or yes_ask <= 0:
                continue
            if volume < 25:  # Raised — thin markets hurt makers
                continue

            spread = yes_ask - yes_bid
            if spread < self.min_spread_cents:
                continue  # Too tight, no room to make money
            if spread > 40:
                continue  # Too wide, likely illiquid/stale

            mid = (yes_bid + yes_ask) / 2
            if mid < 5 or mid > 95:
                continue  # Extreme illiquid tails

            # True dead zone: 41-50¢ has near-zero maker edge (+0.16pp avg).
            # 51-60¢ is the BEST band (+2.40pp), so don't lump them together.
            if 41 <= mid <= 50:
                continue

            # Category filtering (ticker prefix + title keyword)
            ticker = m.get("ticker", "")
            if ticker.startswith(self.LOW_EDGE_PREFIXES):
                continue
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            if any(kw in title for kw in self.LOW_EDGE_KEYWORDS):
                continue  # Skip near-efficient finance markets

            # Category bonus for high-bias markets — ticker-prefix match first
            # (catches series whose titles don't contain the keyword), then keyword fallback.
            category_bonus = 0.0
            if ticker.startswith(self.HIGH_EDGE_PREFIXES) or \
               any(kw in title for kw in self.HIGH_EDGE_KEYWORDS):
                category_bonus = 0.2

            # Empirical maker-edge score by price band (Becker mispricing curve).
            # Replaces old linear `tail_distance` heuristic, which the data doesn't support.
            edge_score = _empirical_edge_score(mid)
            volume_score = min(volume, 5000) / 5000
            spread_score = min(spread, 15) / 15

            score = (volume_score * 0.4 + spread_score * 0.2 +
                     edge_score * 0.3 + category_bonus + 0.1)

            candidates.append({
                "ticker": m.get("ticker", ""),
                "spread": spread,
                "volume": volume,
                "mid": mid,
                "score": score,
            })

        # Sort by score descending. Pick top-N where N depends on hour:
        # high-edge windows get the full slate; the 8-15 ET efficient window
        # gets a tighter subset (avoids burning quota when markets are most efficient).
        candidates.sort(key=lambda c: c["score"], reverse=True)
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            hour_et = datetime.now(ZoneInfo("America/New_York")).hour
        except Exception:
            hour_et = -1  # Fall back to active behavior if tz lookup fails
        is_quiet = hour_et in self.quiet_hours_et
        limit = self.quiet_max_markets if is_quiet else self.active_max_markets
        selected = [c["ticker"] for c in candidates[:limit]]

        if selected and self._cycle_count % 20 == 1:  # Log every ~5 min
            window = "quiet" if is_quiet else "active"
            logger.info(
                f"Auto-selected markets ({window} window, hour {hour_et} ET, "
                f"top-{limit}): {selected} (from {len(candidates)} candidates)"
            )

        return selected

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    async def _update_market_state(self, client, state: MarketState):
        """Fetch latest orderbook and update market state.

        Tries WebSocket live orderbook first; falls back to REST API.
        """
        ws_used = False
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            live_book = ws.get_orderbook(state.ticker)
            if live_book and live_book.last_updated:
                state.yes_bid = live_book.best_yes_bid()
                state.yes_ask = live_book.best_yes_ask()
                state.no_bid = 100 - state.yes_ask if state.yes_ask > 0 else 0
                state.no_ask = 100 - state.yes_bid if state.yes_bid > 0 else 0
                if state.yes_bid > 0 and state.yes_ask > 0:
                    state.mid_price = (state.yes_bid + state.yes_ask) / 2
                state.last_updated = datetime.utcnow().isoformat()
                ws_used = True
        except Exception:
            pass

        if ws_used:
            return

        # Fallback: REST API
        book = await client.get_orderbook(state.ticker)

        # Kalshi orderbook format: {yes: [[price, quantity], ...], no: [[price, quantity], ...]}
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

        if yes_levels:
            # Best bid = highest price someone wants to buy YES
            # Best ask = lowest price someone wants to sell YES
            # In Kalshi's format, the orderbook may list bids and asks together
            state.yes_bid = yes_levels[-1][0] if yes_levels else 0
            state.yes_ask = yes_levels[0][0] if yes_levels else 0
        if no_levels:
            state.no_bid = no_levels[-1][0] if no_levels else 0
            state.no_ask = no_levels[0][0] if no_levels else 0

        # Also get from market data via direct API for reliable bid/ask
        market = await client.get_market_full(state.ticker)
        yb = int(round(float(market.get("yes_bid_dollars", "0") or "0") * 100))
        ya = int(round(float(market.get("yes_ask_dollars", "0") or "0") * 100))
        nb = int(round(float(market.get("no_bid_dollars", "0") or "0") * 100))
        na = int(round(float(market.get("no_ask_dollars", "0") or "0") * 100))
        state.yes_bid = yb or state.yes_bid
        state.yes_ask = ya or state.yes_ask
        state.no_bid = nb or state.no_bid
        state.no_ask = na or state.no_ask

        # Calculate fair value as midpoint
        if state.yes_bid > 0 and state.yes_ask > 0:
            state.mid_price = (state.yes_bid + state.yes_ask) / 2
        else:
            lp = int(round(float(market.get("last_price_dollars", "0") or "0") * 100))
            if lp:
                state.mid_price = lp

        state.last_updated = datetime.utcnow().isoformat()

    async def _check_fills(self, client, state: MarketState):
        """Check if any of our resting orders were filled."""
        if not state.yes_order_id and not state.no_order_id:
            return

        open_orders = await client.get_open_orders()
        open_ids = {o.get("order_id") for o in open_orders}

        # If YES order is gone, it was filled
        if state.yes_order_id and state.yes_order_id not in open_ids:
            fill_qty = self.contracts_per_side
            fill_price = float(state.yes_order_price)
            new_pos = state.yes_position + fill_qty
            state.yes_avg_entry = (
                state.yes_avg_entry * state.yes_position + fill_price * fill_qty
            ) / new_pos
            state.yes_position = new_pos
            state.fills_yes += fill_qty
            logger.info(
                f"YES fill on {state.ticker} @ {state.yes_order_price}¢ "
                f"x{fill_qty} (inventory: {state.net_inventory()}, avg_entry: {state.yes_avg_entry:.1f}¢)"
            )
            state.yes_order_id = None
            state.yes_order_price = 0

            # Check if both sides filled (spread captured!)
            self._check_spread_capture(state)

        # If NO order is gone, it was filled
        if state.no_order_id and state.no_order_id not in open_ids:
            fill_qty = self.contracts_per_side
            fill_price = float(state.no_order_price)
            new_pos = state.no_position + fill_qty
            state.no_avg_entry = (
                state.no_avg_entry * state.no_position + fill_price * fill_qty
            ) / new_pos
            state.no_position = new_pos
            state.fills_no += fill_qty
            logger.info(
                f"NO fill on {state.ticker} @ {state.no_order_price}¢ "
                f"x{fill_qty} (inventory: {state.net_inventory()}, avg_entry: {state.no_avg_entry:.1f}¢)"
            )
            state.no_order_id = None
            state.no_order_price = 0

            self._check_spread_capture(state)

    def _check_spread_capture(self, state: MarketState):
        """When we hold both YES and NO, we've captured the spread."""
        # Minimum of both sides = number of completed round trips
        paired = min(state.yes_position, state.no_position)
        if paired > 0:
            # Each pair pays out 100¢ at settlement, we paid (yes_price + no_price)
            # Net profit = 100 - total_cost - fees
            # Since we track per-fill, approximate with mid_price
            spread_per_pair = self.default_spread_cents - self.fee_per_contract_cents
            captured = paired * max(spread_per_pair, 0)
            state.total_spread_captured_cents += captured
            self._total_pnl_cents += captured

            # Net out the paired inventory
            state.yes_position -= paired
            state.no_position -= paired

            logger.info(
                f"Spread captured on {state.ticker}: {paired} pairs, "
                f"+{captured}¢ (total P&L: {self._total_pnl_cents}¢ / "
                f"${self._total_pnl_cents/100:.2f})"
            )

    # =========================================================================
    # EXIT RULES (added 2026-04-27)
    # =========================================================================

    async def _check_exits(self, client, state: MarketState):
        """Close unpaired inventory at take-profit or stop-loss thresholds.

        Runs after _check_fills/_check_spread_capture, so yes_position and
        no_position represent UNPAIRED directional risk only. Paired pairs are
        already netted to 0 and held to settlement for the guaranteed $1 payout.
        """
        if state.yes_position > 0 and state.yes_bid > 0:
            if state.yes_bid >= self.take_profit_cents:
                await self._exit_position(client, state, "yes", state.yes_bid, "TP")
            elif state.yes_bid <= self.stop_loss_cents:
                await self._exit_position(client, state, "yes", state.yes_bid, "SL")

        if state.no_position > 0 and state.no_bid > 0:
            if state.no_bid >= self.take_profit_cents:
                await self._exit_position(client, state, "no", state.no_bid, "TP")
            elif state.no_bid <= self.stop_loss_cents:
                await self._exit_position(client, state, "no", state.no_bid, "SL")

    async def _exit_position(self, client, state: MarketState, side: str, bid: int, reason: str):
        """Sell unpaired inventory aggressively at the bid."""
        if side == "yes":
            qty = state.yes_position
            entry = state.yes_avg_entry
        else:
            qty = state.no_position
            entry = state.no_avg_entry

        if qty <= 0:
            return

        # Aggressive sell — undercut bid by 1¢ to ensure fill, clamped to [1, 99]
        sell_price = max(1, min(99, bid - 1))

        # Cancel any resting buy on this side first (would otherwise add to inventory)
        existing_order = state.yes_order_id if side == "yes" else state.no_order_id
        if existing_order:
            try:
                await client.cancel_order(existing_order)
            except Exception:
                pass
            if side == "yes":
                state.yes_order_id = None
                state.yes_order_price = 0
            else:
                state.no_order_id = None
                state.no_order_price = 0

        try:
            client_id = f"sb-x{reason.lower()}-{state.ticker[:10]}-{uuid.uuid4().hex[:6]}"
            kwargs = {
                "ticker": state.ticker,
                "side": side,
                "action": "sell",
                "count": qty,
                "order_type": "limit",
                "client_order_id": client_id,
            }
            if side == "yes":
                kwargs["yes_price"] = sell_price
            else:
                kwargs["no_price"] = sell_price

            await client.place_order(**kwargs)

            realized = int(round((sell_price - entry) * qty))
            state.realized_pnl_cents += realized
            self._total_pnl_cents += realized

            if side == "yes":
                state.yes_position = 0
                state.yes_avg_entry = 0.0
            else:
                state.no_position = 0
                state.no_avg_entry = 0.0

            sign = "+" if realized >= 0 else ""
            logger.info(
                f"{reason} exit on {state.ticker}: sold {qty} {side.upper()} @ {sell_price}¢ "
                f"(entry {entry:.1f}¢, realized {sign}{realized}¢)"
            )
            if self.exit_telegram_alerts:
                await self._alert(
                    f"<b>{reason}</b> {sign}{realized}¢ on {state.ticker}\n"
                    f"Sold {qty} {side.upper()} @ {sell_price}¢ (entry {entry:.1f}¢)"
                )
        except Exception as e:
            logger.error(f"Failed {reason} exit on {state.ticker} {side}: {e}")

    # =========================================================================
    # QUOTING
    # =========================================================================

    async def _requote(self, client, state: MarketState):
        """Place or update quotes on both sides of the market."""
        # Skip if cooling down after API rejection (422/insufficient funds)
        if state.rejection_cooldown > 0:
            state.rejection_cooldown -= 1
            return

        mid = state.mid_price
        half_spread = self.default_spread_cents / 2

        # Inventory skew: push price away from the side we're heavy on
        skew = 0
        net = state.net_inventory()
        if abs(net) > 0:
            # Skew proportional to inventory: +net = long YES, make YES bid lower
            skew_factor = min(abs(net) / self.max_inventory_per_market, 1.0)
            skew = int(self.inventory_skew_cents * skew_factor)
            if net > 0:
                skew = -skew  # Long YES → lower YES bid, higher NO bid
            # (if net < 0, we're long NO → skew is already positive for YES)

        # Calculate quote prices
        yes_bid_price = max(1, int(mid - half_spread + skew))
        no_bid_price = max(1, int((100 - mid) - half_spread - skew))

        # Ensure we're not crossing the book
        if state.yes_ask > 0:
            yes_bid_price = min(yes_bid_price, state.yes_ask - 1)
        if state.no_ask > 0:
            no_bid_price = min(no_bid_price, state.no_ask - 1)

        # Clamp to valid range
        yes_bid_price = max(1, min(99, yes_bid_price))
        no_bid_price = max(1, min(99, no_bid_price))

        # Check if existing orders are stale (price moved too far)
        yes_stale = (
            state.yes_order_id is None or
            abs(state.yes_order_price - yes_bid_price) >= self.stale_order_threshold_cents
        )
        no_stale = (
            state.no_order_id is None or
            abs(state.no_order_price - no_bid_price) >= self.stale_order_threshold_cents
        )

        # Check inventory limits
        yes_ok = state.yes_position < self.max_inventory_per_market
        no_ok = state.no_position < self.max_inventory_per_market

        # Cancel stale orders and place new ones
        if yes_stale and yes_ok:
            if state.yes_order_id:
                try:
                    await client.cancel_order(state.yes_order_id)
                except Exception:
                    pass
                state.yes_order_id = None

            try:
                client_id = f"sb-y-{state.ticker[:10]}-{uuid.uuid4().hex[:8]}"
                result = await client.place_order(
                    ticker=state.ticker,
                    side="yes",
                    action="buy",
                    yes_price=yes_bid_price,
                    count=self.contracts_per_side,
                    order_type="limit",
                    client_order_id=client_id,
                )
                order = result.get("order", {})
                state.yes_order_id = order.get("order_id", client_id)
                state.yes_order_price = yes_bid_price
            except Exception as e:
                err_str = str(e)
                if "422" in err_str or "insufficient" in err_str.lower() or "exposure" in err_str.lower():
                    logger.warning(f"YES quote rejected on {state.ticker}: {e}")
                    state.rejection_cooldown = 10  # Skip 10 cycles (~5 min at 30s interval)
                else:
                    logger.error(f"YES quote failed on {state.ticker}: {e}")

        if no_stale and no_ok:
            if state.no_order_id:
                try:
                    await client.cancel_order(state.no_order_id)
                except Exception:
                    pass
                state.no_order_id = None

            try:
                client_id = f"sb-n-{state.ticker[:10]}-{uuid.uuid4().hex[:8]}"
                result = await client.place_order(
                    ticker=state.ticker,
                    side="no",
                    action="buy",
                    no_price=no_bid_price,
                    count=self.contracts_per_side,
                    order_type="limit",
                    client_order_id=client_id,
                )
                order = result.get("order", {})
                state.no_order_id = order.get("order_id", client_id)
                state.no_order_price = no_bid_price
            except Exception as e:
                err_str = str(e)
                if "422" in err_str or "insufficient" in err_str.lower() or "exposure" in err_str.lower():
                    logger.warning(f"NO quote rejected on {state.ticker}: {e}")
                    state.rejection_cooldown = 10  # Skip 10 cycles (~5 min at 30s interval)
                else:
                    logger.error(f"NO quote failed on {state.ticker}: {e}")

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    async def _cancel_all_orders(self):
        """Cancel all resting spread bot orders (tracked + orphaned from prior runs)."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        # Cancel tracked orders
        for ticker, state in list(self._markets.items()):
            for order_id in [state.yes_order_id, state.no_order_id]:
                if order_id:
                    try:
                        await client.cancel_order(order_id)
                    except Exception:
                        pass
            state.yes_order_id = None
            state.no_order_id = None
            state.yes_order_price = 0
            state.no_order_price = 0

        # Also cancel any orphaned sb- orders from prior runs
        try:
            orders = await client.get_open_orders()
            cancelled = 0
            for o in orders:
                cid = o.get("client_order_id", "")
                if cid.startswith("sb-"):
                    try:
                        await client.cancel_order(o.get("order_id", ""))
                        cancelled += 1
                    except Exception:
                        pass
            if cancelled:
                logger.info(f"Cancelled {cancelled} orphaned spread bot orders")
        except Exception as e:
            logger.warning(f"Failed to clean up orphaned orders: {e}")

    async def flatten_market(self, ticker: str):
        """Close out all inventory in a specific market at market price."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        if ticker not in self._markets:
            return

        state = self._markets[ticker]

        # Cancel resting orders first
        for order_id in [state.yes_order_id, state.no_order_id]:
            if order_id:
                try:
                    await client.cancel_order(order_id)
                except Exception:
                    pass

        # Sell any YES inventory
        if state.yes_position > 0:
            try:
                await client.place_order(
                    ticker=ticker,
                    side="yes",
                    action="sell",
                    yes_price=max(1, state.yes_bid - 1),  # Aggressive sell
                    count=state.yes_position,
                )
                logger.info(f"Flattened {state.yes_position} YES on {ticker}")
                state.yes_position = 0
            except Exception as e:
                logger.error(f"Failed to flatten YES on {ticker}: {e}")

        # Sell any NO inventory
        if state.no_position > 0:
            try:
                await client.place_order(
                    ticker=ticker,
                    side="no",
                    action="sell",
                    no_price=max(1, state.no_bid - 1),
                    count=state.no_position,
                )
                logger.info(f"Flattened {state.no_position} NO on {ticker}")
                state.no_position = 0
            except Exception as e:
                logger.error(f"Failed to flatten NO on {ticker}: {e}")

        state.yes_order_id = None
        state.no_order_id = None

    async def flatten_all(self):
        """Flatten all markets and stop quoting."""
        for ticker in list(self._markets.keys()):
            await self.flatten_market(ticker)
        await self._alert("Spread bot: flattened all positions")

    # =========================================================================
    # MARKET MANAGEMENT
    # =========================================================================

    def add_market(self, ticker: str):
        """Add a market to the target list."""
        if ticker not in self.target_tickers:
            self.target_tickers.append(ticker)
            logger.info(f"Added {ticker} to spread bot targets")

    def remove_market(self, ticker: str):
        """Remove a market and flatten its position."""
        if ticker in self.target_tickers:
            self.target_tickers.remove(ticker)
        logger.info(f"Removed {ticker} from spread bot targets")

    # =========================================================================
    # ALERTS
    # =========================================================================

    async def _alert(self, message: str):
        """Send Telegram alert."""
        if not self.telegram_alerts:
            return
        try:
            from app.services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(f"<b>Spread Bot</b>\n{message}")
        except Exception:
            pass

    # =========================================================================
    # STATUS
    # =========================================================================

    def get_status(self) -> dict:
        """Return bot status and all market states."""
        return {
            "enabled": self.enabled,
            "running": self._running,
            "kill_switch": self._kill_switch,
            "cycle_count": self._cycle_count,
            "start_time": self._start_time,
            "total_pnl_cents": self._total_pnl_cents,
            "total_pnl_usd": round(self._total_pnl_cents / 100, 2),
            "active_markets": len(self._markets),
            "target_tickers": self.target_tickers,
            "config": {
                "spread_cents": self.default_spread_cents,
                "contracts_per_side": self.contracts_per_side,
                "max_inventory": self.max_inventory_per_market,
                "max_exposure_cents": self.max_total_exposure_cents,
                "poll_interval": self.poll_interval,
                "take_profit_cents": self.take_profit_cents,
                "stop_loss_cents": self.stop_loss_cents,
            },
            "markets": {
                ticker: state.to_dict()
                for ticker, state in self._markets.items()
            },
        }


# Singleton
_spread_bot: Optional[KalshiSpreadBot] = None


def get_spread_bot() -> KalshiSpreadBot:
    global _spread_bot
    if _spread_bot is None:
        _spread_bot = KalshiSpreadBot()
    return _spread_bot
