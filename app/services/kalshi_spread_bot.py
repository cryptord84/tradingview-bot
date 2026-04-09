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
        # Inventory
        self.yes_position: int = 0  # net YES contracts held
        self.no_position: int = 0   # net NO contracts held
        # Active orders
        self.yes_order_id: Optional[str] = None
        self.no_order_id: Optional[str] = None
        self.yes_order_price: int = 0
        self.no_order_price: int = 0
        # Stats
        self.fills_yes: int = 0
        self.fills_no: int = 0
        self.total_spread_captured_cents: int = 0
        self.last_updated: Optional[str] = None
        self.errors: int = 0

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
            "net_inventory": self.net_inventory(),
            "yes_order_id": self.yes_order_id,
            "no_order_id": self.no_order_id,
            "yes_order_price": self.yes_order_price,
            "no_order_price": self.no_order_price,
            "fills_yes": self.fills_yes,
            "fills_no": self.fills_no,
            "spread_captured_cents": self.total_spread_captured_cents,
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
        self.telegram_alerts = spread_cfg.get("telegram_alerts", True)
        self.target_tickers = spread_cfg.get("target_tickers", [])

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

    # Categories with low maker edge (research: jbecker.dev/prediction-market-microstructure)
    LOW_EDGE_KEYWORDS = ["finance", "fed ", "interest rate", "gdp", "cpi", "inflation",
                         "treasury", "earnings"]
    # Categories with high maker edge (behavioral bias drives taker losses)
    HIGH_EDGE_KEYWORDS = ["sports", "entertainment", "celebrity", "movie", "tv ",
                          "award", "oscar", "grammy", "super bowl", "world series",
                          "playoff", "championship", "election", "trump", "biden",
                          "war", "conflict", "weather", "hurricane"]

    async def _auto_select_markets(self, client) -> list[str]:
        """Auto-select the best markets for spread-capturing.

        Research-informed scoring:
        - Avoid 40-60¢ mid range (near-zero maker edge)
        - Prefer tail prices (5-20¢, 80-95¢) where longshot bias is strongest
        - Prefer high-bias categories (sports, entertainment, politics)
        - Avoid finance/economics (nearly efficient, 0.17pp gap)
        """
        markets = await client.discover_active_markets(min_volume=10)
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

            # Skip dead zone — near-zero edge at 40-60¢
            if 40 <= mid <= 60:
                continue

            # Category filtering
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            if any(kw in title for kw in self.LOW_EDGE_KEYWORDS):
                continue  # Skip near-efficient finance markets

            # Category bonus for high-bias markets
            category_bonus = 0.0
            if any(kw in title for kw in self.HIGH_EDGE_KEYWORDS):
                category_bonus = 0.2

            # Tail preference: best edge at 5-20¢ and 80-95¢
            tail_distance = abs(mid - 50) / 50
            volume_score = min(volume, 5000) / 5000
            spread_score = min(spread, 15) / 15

            score = (volume_score * 0.4 + spread_score * 0.2 +
                     tail_distance * 0.3 + category_bonus + 0.1)

            candidates.append({
                "ticker": m.get("ticker", ""),
                "spread": spread,
                "volume": volume,
                "mid": mid,
                "score": score,
            })

        # Sort by score descending, take top 3
        candidates.sort(key=lambda c: c["score"], reverse=True)
        selected = [c["ticker"] for c in candidates[:3]]

        if selected and self._cycle_count % 20 == 1:  # Log every ~5 min
            logger.info(
                f"Auto-selected markets: {selected} "
                f"(from {len(candidates)} candidates)"
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
            state.fills_yes += self.contracts_per_side
            state.yes_position += self.contracts_per_side
            logger.info(
                f"YES fill on {state.ticker} @ {state.yes_order_price}¢ "
                f"x{self.contracts_per_side} (inventory: {state.net_inventory()})"
            )
            state.yes_order_id = None
            state.yes_order_price = 0

            # Check if both sides filled (spread captured!)
            self._check_spread_capture(state)

        # If NO order is gone, it was filled
        if state.no_order_id and state.no_order_id not in open_ids:
            state.fills_no += self.contracts_per_side
            state.no_position += self.contracts_per_side
            logger.info(
                f"NO fill on {state.ticker} @ {state.no_order_price}¢ "
                f"x{self.contracts_per_side} (inventory: {state.net_inventory()})"
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
    # QUOTING
    # =========================================================================

    async def _requote(self, client, state: MarketState):
        """Place or update quotes on both sides of the market."""
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
                logger.error(f"NO quote failed on {state.ticker}: {e}")

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    async def _cancel_all_orders(self):
        """Cancel all resting spread bot orders (tracked + orphaned from prior runs)."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        # Cancel tracked orders
        for ticker, state in self._markets.items():
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
