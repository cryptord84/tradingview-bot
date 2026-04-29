"""Kalshi Market Maker Framework — advanced multi-strategy automated market-making.

Extends the basic spread bot with:
- Multi-strategy quoting: midpoint, VWAP-anchored, volatility-adjusted
- Dynamic spread sizing based on market conditions
- Portfolio-level risk management across all quoted markets
- Adverse selection detection (cancel quotes after large directional trades)
- P&L attribution by market and strategy
- Configurable market scoring and auto-rotation
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.mm")


# =========================================================================
# DATA CLASSES
# =========================================================================

class QuoteLevel:
    """A single quote (bid or ask) placed by the market maker."""

    def __init__(self, side: str, price: int, count: int, order_id: str = "",
                 client_id: str = "", strategy: str = "midpoint"):
        self.side = side           # "yes" or "no"
        self.price = price         # cents
        self.count = count
        self.order_id = order_id
        self.client_id = client_id
        self.strategy = strategy
        self.placed_at = time.time()
        self.filled = False

    def age_seconds(self) -> float:
        return time.time() - self.placed_at


class Fill:
    """A recorded fill event."""

    def __init__(self, ticker: str, side: str, price: int, count: int,
                 strategy: str, ts: float):
        self.ticker = ticker
        self.side = side
        self.price = price
        self.count = count
        self.strategy = strategy
        self.ts = ts

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "price": self.price,
            "count": self.count,
            "strategy": self.strategy,
            "ts": datetime.fromtimestamp(self.ts).isoformat(),
        }


class MMMarketState:
    """Extended market state for the market maker."""

    def __init__(self, ticker: str, title: str = ""):
        self.ticker = ticker
        self.title = title
        # Orderbook
        self.yes_bid: int = 0
        self.yes_ask: int = 0
        self.no_bid: int = 0
        self.no_ask: int = 0
        self.yes_bid_size: int = 0
        self.yes_ask_size: int = 0
        self.spread: int = 0
        self.mid_price: float = 50.0
        self.vwap: float = 50.0
        # Trade history for VWAP / adverse selection
        self._recent_trades: list[dict] = []
        self._last_trade_side: Optional[str] = None
        self._consecutive_same_side: int = 0
        # Volatility (price std dev over recent trades)
        self.volatility: float = 0.0
        # Inventory
        self.yes_position: int = 0
        self.no_position: int = 0
        # Active quotes
        self.quotes: list[QuoteLevel] = []
        # Fill history
        self.fills: list[Fill] = []
        # P&L
        self.realized_pnl_cents: int = 0
        self.spread_captured_cents: int = 0
        self.adverse_selection_count: int = 0
        # Scoring
        self.score: float = 0.0
        self.volume_24h: int = 0
        self.last_updated: Optional[str] = None
        self.errors: int = 0
        self.active_strategy: str = "midpoint"

    def net_inventory(self) -> int:
        return self.yes_position - self.no_position

    def abs_inventory(self) -> int:
        return abs(self.net_inventory())

    def total_exposure_cents(self) -> int:
        return self.yes_position * 50 + self.no_position * 50  # rough estimate

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "no_bid": self.no_bid,
            "no_ask": self.no_ask,
            "spread": self.spread,
            "mid_price": round(self.mid_price, 1),
            "vwap": round(self.vwap, 1),
            "volatility": round(self.volatility, 2),
            "yes_position": self.yes_position,
            "no_position": self.no_position,
            "net_inventory": self.net_inventory(),
            "active_quotes": len([q for q in self.quotes if not q.filled]),
            "total_fills": len(self.fills),
            "realized_pnl_cents": self.realized_pnl_cents,
            "spread_captured_cents": self.spread_captured_cents,
            "adverse_selections": self.adverse_selection_count,
            "score": round(self.score, 2),
            "volume_24h": self.volume_24h,
            "active_strategy": self.active_strategy,
            "last_updated": self.last_updated,
            "errors": self.errors,
        }


# =========================================================================
# QUOTING STRATEGIES
# =========================================================================

def strategy_midpoint(state: MMMarketState, half_spread: float, skew: int) -> tuple[int, int]:
    """Simple midpoint strategy: quote symmetrically around mid."""
    yes_bid = max(1, int(state.mid_price - half_spread + skew))
    no_bid = max(1, int((100 - state.mid_price) - half_spread - skew))
    return yes_bid, no_bid


def strategy_vwap(state: MMMarketState, half_spread: float, skew: int) -> tuple[int, int]:
    """VWAP-anchored: quote around volume-weighted average price."""
    anchor = state.vwap if state.vwap > 0 else state.mid_price
    yes_bid = max(1, int(anchor - half_spread + skew))
    no_bid = max(1, int((100 - anchor) - half_spread - skew))
    return yes_bid, no_bid


def strategy_volatility(state: MMMarketState, base_half_spread: float, skew: int) -> tuple[int, int]:
    """Volatility-adjusted: widen spread when market is volatile."""
    vol_multiplier = 1.0 + min(state.volatility * 2, 3.0)  # Cap at 4x spread
    half_spread = base_half_spread * vol_multiplier
    yes_bid = max(1, int(state.mid_price - half_spread + skew))
    no_bid = max(1, int((100 - state.mid_price) - half_spread - skew))
    return yes_bid, no_bid


STRATEGIES = {
    "midpoint": strategy_midpoint,
    "vwap": strategy_vwap,
    "volatility": strategy_volatility,
}


# Empirical maker-edge curve from Becker dataset (mispricing_by_price.csv,
# 2026-04-28 analysis). Maps each 10¢ band to its avg maker mispricing in pp.
# Higher = more edge. Used by _select_markets to score candidates beyond
# the legacy "linear distance from 50¢" rule, which the data doesn't support.
_EDGE_BANDS_PP = {
    (1, 5):   0.48,
    (6, 10):  0.12,
    (11, 15): 0.06,
    (16, 20): 0.53,
    (21, 25): -0.29,  # avoid: negative maker edge
    (26, 30): 1.02,
    (31, 40): 2.14,
    (41, 50): 0.16,   # dead zone (already filtered upstream)
    (51, 60): 2.40,   # peak
    (61, 70): 0.44,
    (71, 80): 1.39,
    (81, 90): 1.93,
    (91, 99): 1.61,
}
_EDGE_MAX_PP = max(_EDGE_BANDS_PP.values())


def _empirical_edge_score(mid: float) -> float:
    """Empirical maker-edge score (0.0–1.0) based on Becker mispricing curve."""
    for (lo, hi), pp in _EDGE_BANDS_PP.items():
        if lo <= mid <= hi:
            return max(0.0, pp / _EDGE_MAX_PP)
    return 0.0


# =========================================================================
# MARKET MAKER
# =========================================================================

class KalshiMarketMaker:
    """Advanced market-making framework for Kalshi prediction markets."""

    def __init__(self):
        cfg = get("kalshi") or {}
        mm_cfg = cfg.get("market_maker", {})

        self.enabled = mm_cfg.get("enabled", False)
        self.poll_interval = mm_cfg.get("poll_interval_seconds", 10)
        self.default_strategy = mm_cfg.get("default_strategy", "midpoint")
        self.fallback_strategy = mm_cfg.get("fallback_strategy", "volatility")

        # Spread config
        self.base_spread_cents = mm_cfg.get("base_spread_cents", 4)
        self.min_spread_cents = mm_cfg.get("min_spread_cents", 2)
        self.max_spread_cents = mm_cfg.get("max_spread_cents", 12)
        self.dynamic_spread = mm_cfg.get("dynamic_spread", True)
        self.max_days_to_close = mm_cfg.get("max_days_to_close", 0)

        # Order sizing
        self.contracts_per_level = mm_cfg.get("contracts_per_level", 5)
        self.quote_levels = mm_cfg.get("quote_levels", 1)  # Number of price levels to quote

        # Inventory limits
        self.max_inventory_per_market = mm_cfg.get("max_inventory_per_market", 25)
        self.max_total_inventory = mm_cfg.get("max_total_inventory", 100)
        self.max_total_exposure_cents = mm_cfg.get("max_total_exposure_cents", 5000)
        self.inventory_skew_cents = mm_cfg.get("inventory_skew_cents", 2)

        # Risk controls
        self.adverse_selection_threshold = mm_cfg.get("adverse_selection_trades", 3)
        self.stale_quote_seconds = mm_cfg.get("stale_quote_seconds", 60)
        self.stale_price_threshold_cents = mm_cfg.get("stale_price_threshold_cents", 3)
        self.flatten_minutes_before_close = mm_cfg.get("flatten_minutes_before_close", 30)
        self.fee_per_contract_cents = mm_cfg.get("fee_per_contract_cents", 2)

        # Market selection
        self.target_tickers = mm_cfg.get("target_tickers", [])
        self.max_markets = mm_cfg.get("max_markets", 5)
        self.min_market_volume = mm_cfg.get("min_market_volume", 100)
        self.rotation_interval_cycles = mm_cfg.get("rotation_interval_cycles", 50)

        # Alerts
        self.telegram_alerts = mm_cfg.get("telegram_alerts", True)

        # Runtime state
        self._markets: dict[str, MMMarketState] = {}
        self._all_fills: list[Fill] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._kill_switch = False
        self._cycle_count = 0
        self._start_time: Optional[str] = None
        self._total_pnl_cents = 0
        self._total_spread_captured = 0
        self._total_adverse_selections = 0

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._kill_switch = False
        self._start_time = datetime.utcnow().isoformat()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Market maker started: strategy={self.default_strategy}, "
            f"spread={self.base_spread_cents}\u00a2, levels={self.quote_levels}, "
            f"contracts={self.contracts_per_level}, max_markets={self.max_markets}"
        )
        return self._task

    async def stop(self):
        self._running = False
        logger.info("Market maker stopping \u2014 cancelling all orders...")
        await self._cancel_all_orders()
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Market maker stopped")

    def kill(self):
        self._kill_switch = True
        self._running = False
        logger.warning("KILL SWITCH \u2014 market maker halted immediately")

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    async def _run_loop(self):
        # Cancel orphaned orders from prior runs
        try:
            await self._cancel_all_orders()
            logger.info("Market maker: cleaned up orphaned orders on startup")
        except Exception as e:
            logger.warning(f"Market maker startup cleanup failed: {e}")

        while self._running and not self._kill_switch:
            try:
                self._cycle_count += 1
                await self._cycle()
            except Exception as e:
                logger.error(f"MM cycle error: {e}")
                error_count = sum(1 for m in self._markets.values() if m.errors > 3)
                if error_count >= 3:
                    self.kill()
                    await self._alert(
                        "\u26a0\ufe0f KILL SWITCH: Market maker halted after repeated errors.\n"
                        f"Last error: {e}"
                    )
                    return
            await asyncio.sleep(self.poll_interval)

    async def _cycle(self):
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        # Rotate markets periodically
        tickers = self.target_tickers
        if not tickers or (self._cycle_count % self.rotation_interval_cycles == 1):
            tickers = await self._select_markets(client)
            if not self.target_tickers:
                if self._cycle_count % 20 == 1:
                    logger.info(f"MM auto-selected: {tickers}")

        # Auto-subscribe WS feed to selected markets
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            if ws.enabled:
                for t in tickers:
                    await ws.subscribe(t)
        except Exception:
            pass

        # Process each market
        for ticker in tickers:
            if self._kill_switch:
                return

            try:
                if ticker not in self._markets:
                    market_data = await client.get_market(ticker)
                    self._markets[ticker] = MMMarketState(
                        ticker=ticker,
                        title=market_data.get("title", ticker),
                    )

                state = self._markets[ticker]
                await self._update_state(client, state)
                await self._detect_fills(client, state)
                self._detect_adverse_selection(state)

                # Check portfolio-level limits
                total_inv = sum(m.abs_inventory() for m in self._markets.values())
                total_exp = sum(m.total_exposure_cents() for m in self._markets.values())

                if total_inv >= self.max_total_inventory:
                    logger.warning(f"Portfolio inventory limit reached: {total_inv}")
                    continue
                if total_exp >= self.max_total_exposure_cents:
                    logger.warning(f"Portfolio exposure limit reached: {total_exp}\u00a2")
                    continue

                # Select strategy for this market
                strategy = self._select_strategy(state)
                state.active_strategy = strategy

                await self._place_quotes(client, state, strategy)
                state.errors = 0

            except Exception as e:
                logger.error(f"MM error on {ticker}: {e}")
                if ticker in self._markets:
                    self._markets[ticker].errors += 1

    # =========================================================================
    # MARKET SELECTION & SCORING
    # =========================================================================

    # Categories with low maker edge (research: jbecker.dev/prediction-market-microstructure)
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
    # KXEPSTEIN). Aligned with Becker classifier's World Events / Media /
    # Entertainment / Sci-Tech / Weather groups.
    HIGH_EDGE_PREFIXES = (
        # World Events (4-7pp)
        "KXNOBEL", "KXNEXTPOPE", "KXPOPE", "KXEPSTEIN", "KXOTEEPSTEIN",
        "KXZELENSKYYPUTINMEET", "KXBOLIVIAPRES", "KXSKPRES",
        "KXLAGODAYS", "KXARREST",
        # Media (7pp)
        "KXMENTION", "KXHEADLINE", "KXGOOGLESEARCH", "KX538APPROVE", "KXAPRPOTUS",
        # Entertainment (4-5pp)
        "KXOSCAR", "KXGRAMMY", "KXEMMY", "KXBAFTA", "KXGAMEAWARDS",
        "KXSPOTIFY", "KXNETFLIX", "KXRT", "KXTOPSONG", "KXTOPALBUM", "KXTOPARTIST",
        "KXBILLBOARD",
        # Science/Tech (4pp)
        "KXLLM", "KXAI", "KXSPACEX", "KXALIENS", "KXAPPLE",
        # Weather (2.5pp)
        "KXHIGH", "KXRAIN", "KXSNOW", "KXTORNADO", "KXHURCAT", "KXARCTICICE", "KXWEATHER",
        # Crypto non-daily strikes (2.7pp; KXBTCD/KXETHD already in own category)
        "KXBTCMAX", "KXBTCMIN", "KXETHMAX", "KXETHMIN", "KXBTCRESERVE",
    )

    async def _select_markets(self, client) -> list[str]:
        """Score and select the best markets for market-making.

        Empirical scoring (Becker dataset 2026-04-28, mispricing_by_price.csv):
        - True dead zone is 41-50¢ (+0.16pp), NOT 40-60¢ as previously assumed
        - Best maker bands: 51-60¢ (+2.40pp), 31-40¢ (+2.14pp), 81-90¢ (+1.93pp)
        - 1-15¢ tails are WEAK (+0.06 to +0.48pp) — old "longshot bias" rule overstated this
        - Avoid finance (0.17pp gap, nearly efficient) and 21-25¢ (negative maker edge)
        """
        markets = await client.discover_active_markets(min_volume=10, max_days_to_close=self.max_days_to_close)
        scored = []

        for m in markets:
            yes_bid = int(round(float(m.get("yes_bid_dollars", "0") or "0") * 100))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            volume = int(float(m.get("volume_fp", "0") or "0"))

            if yes_bid <= 0 or yes_ask <= 0:
                continue
            if volume < 50:  # Raised from 10 — thin markets hurt makers
                continue

            spread = yes_ask - yes_bid
            if spread < self.min_spread_cents or spread > 50:
                continue

            mid = (yes_bid + yes_ask) / 2

            # True dead zone: 41-50¢ has near-zero maker edge (+0.16pp avg).
            # 51-60¢ is the BEST band (+2.40pp), so don't lump them together.
            if 41 <= mid <= 50:
                continue

            # Skip extreme illiquid tails
            if mid < 5 or mid > 95:
                continue

            # Skip low-edge prefix before scoring (hard exclude for finance)
            ticker = m.get("ticker", "")
            if ticker.startswith(self.LOW_EDGE_PREFIXES):
                continue
            # Category scoring: ticker-prefix match first (catches series whose
            # titles don't contain the keyword), then title keyword fallback.
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            category_bonus = 0.0
            is_low_edge = any(kw in title for kw in self.LOW_EDGE_KEYWORDS)
            is_high_edge = ticker.startswith(self.HIGH_EDGE_PREFIXES) or \
                           any(kw in title for kw in self.HIGH_EDGE_KEYWORDS)
            if is_low_edge:
                category_bonus = -0.3  # Penalize efficient markets
            elif is_high_edge:
                category_bonus = 0.2   # Reward high-bias categories

            # Empirical maker-edge score by price band (Becker mispricing curve).
            # Range 0.0 (no edge) → 1.0 (best band).
            tail_score = _empirical_edge_score(mid)

            spread_score = min(spread, 15) / 15
            volume_score = min(volume, 5000) / 5000

            score = (volume_score * 0.4 + spread_score * 0.2 +
                     tail_score * 0.3 + category_bonus + 0.1)

            scored.append({
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "score": score,
                "volume": volume,
                "spread": spread,
                "mid": mid,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        selected = [s["ticker"] for s in scored[:self.max_markets]]

        # Update scores in state
        for s in scored[:self.max_markets]:
            if s["ticker"] in self._markets:
                self._markets[s["ticker"]].score = s["score"]
                self._markets[s["ticker"]].volume_24h = s["volume"]

        return selected

    def _select_strategy(self, state: MMMarketState) -> str:
        """Choose the best quoting strategy based on market conditions."""
        # High volatility → use volatility strategy
        if state.volatility > 0.5:
            return "volatility"
        # Good VWAP data → use VWAP
        if len(state._recent_trades) >= 10 and abs(state.vwap - state.mid_price) > 1:
            return "vwap"
        # Default
        return self.default_strategy

    # =========================================================================
    # STATE UPDATE
    # =========================================================================

    async def _update_state(self, client, state: MMMarketState):
        """Fetch orderbook, recent trades, and compute derived metrics.

        Tries the WebSocket live orderbook first for speed; falls back to
        REST API if the WS feed doesn't have data for this ticker.
        """
        ws_used = False
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            book = ws.get_orderbook(state.ticker)
            if book and book.last_updated:
                state.yes_bid = book.best_yes_bid()
                state.yes_ask = book.best_yes_ask()
                state.no_bid = 100 - state.yes_ask if state.yes_ask > 0 else 0
                state.no_ask = 100 - state.yes_bid if state.yes_bid > 0 else 0
                if state.yes_bid > 0 and state.yes_ask > 0:
                    state.spread = state.yes_ask - state.yes_bid
                    state.mid_price = (state.yes_bid + state.yes_ask) / 2
                state.yes_bid_size = sum(book.yes_levels.values())
                state.yes_ask_size = sum(book.no_levels.values())
                ws_used = True
        except Exception:
            pass

        if not ws_used:
            # Fallback: REST API
            market = await client.get_market_full(state.ticker)
            state.yes_bid = int(round(float(market.get("yes_bid_dollars", "0") or "0") * 100))
            state.yes_ask = int(round(float(market.get("yes_ask_dollars", "0") or "0") * 100))
            state.no_bid = int(round(float(market.get("no_bid_dollars", "0") or "0") * 100))
            state.no_ask = int(round(float(market.get("no_ask_dollars", "0") or "0") * 100))
            state.volume_24h = int(float(market.get("volume_fp", "0") or "0"))

            if state.yes_bid > 0 and state.yes_ask > 0:
                state.spread = state.yes_ask - state.yes_bid
                state.mid_price = (state.yes_bid + state.yes_ask) / 2

            try:
                rest_book = await client.get_orderbook(state.ticker)
                yes_levels = rest_book.get("yes", [])
                if yes_levels:
                    state.yes_bid_size = sum(lvl[1] for lvl in yes_levels if len(lvl) > 1)
                    state.yes_ask_size = state.yes_bid_size
            except Exception:
                pass

        # Recent trades for VWAP and volatility
        try:
            trades = await client.get_market_trades(state.ticker, limit=30)
            state._recent_trades = trades

            # VWAP
            total_value = 0
            total_volume = 0
            prices = []
            for t in trades:
                price = t.get("yes_price", 0) or t.get("no_price", 0) or 0
                count = t.get("count", 1) or 1
                if price > 0:
                    total_value += price * count
                    total_volume += count
                    prices.append(price)

            if total_volume > 0:
                state.vwap = total_value / total_volume

            # Volatility (std dev of recent prices)
            if len(prices) >= 5:
                mean = sum(prices) / len(prices)
                variance = sum((p - mean) ** 2 for p in prices) / len(prices)
                state.volatility = variance ** 0.5

            # Track consecutive same-side trades (adverse selection signal)
            if trades:
                latest_side = "yes" if trades[0].get("yes_price") else "no"
                if latest_side == state._last_trade_side:
                    state._consecutive_same_side += 1
                else:
                    state._consecutive_same_side = 0
                state._last_trade_side = latest_side

        except Exception:
            pass

        state.last_updated = datetime.utcnow().isoformat()

    # =========================================================================
    # FILL DETECTION & P&L
    # =========================================================================

    async def _detect_fills(self, client, state: MMMarketState):
        """Check for filled orders and update inventory + P&L."""
        if not state.quotes:
            return

        try:
            open_orders = await client.get_open_orders()
            open_ids = {o.get("order_id") for o in open_orders}
        except Exception:
            return

        newly_filled = []
        remaining_quotes = []

        for q in state.quotes:
            if q.filled:
                continue
            if q.order_id and q.order_id not in open_ids:
                # Order no longer open → filled
                q.filled = True
                fill = Fill(
                    ticker=state.ticker,
                    side=q.side,
                    price=q.price,
                    count=q.count,
                    strategy=q.strategy,
                    ts=time.time(),
                )
                state.fills.append(fill)
                self._all_fills.append(fill)
                newly_filled.append(q)

                if q.side == "yes":
                    state.yes_position += q.count
                else:
                    state.no_position += q.count

                logger.info(
                    f"MM FILL: {q.side.upper()} {q.count}x @{q.price}\u00a2 on {state.ticker} "
                    f"[{q.strategy}] (inv: {state.net_inventory()})"
                )
            else:
                remaining_quotes.append(q)

        state.quotes = remaining_quotes

        # Check for spread capture (paired YES + NO fills)
        self._capture_spread(state)

        # Trim fill history
        if len(state.fills) > 200:
            state.fills = state.fills[-200:]
        if len(self._all_fills) > 1000:
            self._all_fills = self._all_fills[-500:]

    def _capture_spread(self, state: MMMarketState):
        """Net out paired YES/NO inventory as captured spread."""
        paired = min(state.yes_position, state.no_position)
        if paired <= 0:
            return

        # Each paired contract pays 100¢ at settlement
        # Approximate spread captured = base_spread - fees
        spread_per_pair = max(self.base_spread_cents - self.fee_per_contract_cents * 2, 0)
        captured = paired * spread_per_pair
        state.spread_captured_cents += captured
        state.realized_pnl_cents += captured
        self._total_pnl_cents += captured
        self._total_spread_captured += captured

        state.yes_position -= paired
        state.no_position -= paired

        logger.info(
            f"MM spread captured on {state.ticker}: {paired} pairs, "
            f"+{captured}\u00a2 (total: ${self._total_pnl_cents/100:.2f})"
        )

    # =========================================================================
    # ADVERSE SELECTION
    # =========================================================================

    def _detect_adverse_selection(self, state: MMMarketState):
        """Detect when informed traders are picking off our stale quotes.

        If we see several consecutive trades on the same side, the market
        is moving directionally and our quotes are likely getting adversely
        selected. Pull quotes temporarily.
        """
        if state._consecutive_same_side >= self.adverse_selection_threshold:
            state.adverse_selection_count += 1
            self._total_adverse_selections += 1
            # Cancel all quotes for this market — they'll be re-placed next cycle
            # with updated prices
            state.quotes = [q for q in state.quotes if q.filled]
            logger.warning(
                f"Adverse selection on {state.ticker}: {state._consecutive_same_side} "
                f"consecutive {state._last_trade_side} trades, pulling quotes"
            )
            state._consecutive_same_side = 0

    # =========================================================================
    # QUOTING
    # =========================================================================

    async def _place_quotes(self, client, state: MMMarketState, strategy: str):
        """Place or refresh quotes using the selected strategy."""
        # Calculate dynamic spread
        half_spread = self.base_spread_cents / 2
        if self.dynamic_spread:
            # Widen spread for volatile markets, tighten for calm ones
            vol_factor = 1.0 + min(state.volatility, 3.0)
            # Widen if inventory is heavy
            inv_factor = 1.0 + (state.abs_inventory() / max(self.max_inventory_per_market, 1)) * 0.5
            half_spread = max(self.min_spread_cents / 2,
                            min(self.max_spread_cents / 2, half_spread * vol_factor * inv_factor))

        # Inventory skew
        skew = 0
        net = state.net_inventory()
        if abs(net) > 0:
            skew_factor = min(abs(net) / self.max_inventory_per_market, 1.0)
            skew = int(self.inventory_skew_cents * skew_factor * (3 if abs(net) > self.max_inventory_per_market * 0.7 else 1))
            if net > 0:
                skew = -skew

        # NO-side bias at price extremes (research: longshot bias)
        # At low prices (YES cheap), takers overpay for YES → favor NO quotes
        # At high prices (YES expensive), takers overpay for NO → favor YES quotes
        no_bias = 0
        if state.mid_price <= 20:
            # Low-price regime: NO contracts outperform — tighten NO, widen YES
            no_bias = -1  # Make NO quote more aggressive (cheaper for us)
        elif state.mid_price >= 80:
            # High-price regime: YES contracts outperform — tighten YES, widen NO
            no_bias = 1

        # Get quote prices from strategy
        strat_fn = STRATEGIES.get(strategy, STRATEGIES["midpoint"])
        yes_bid_price, no_bid_price = strat_fn(state, half_spread, skew)

        # Apply NO-side bias
        yes_bid_price += no_bias  # Positive = more aggressive YES
        no_bid_price -= no_bias   # Negative = more aggressive NO

        # Clamp and prevent crossing
        if state.yes_ask > 0:
            yes_bid_price = min(yes_bid_price, state.yes_ask - 1)
        if state.no_ask > 0:
            no_bid_price = min(no_bid_price, state.no_ask - 1)
        yes_bid_price = max(1, min(99, yes_bid_price))
        no_bid_price = max(1, min(99, no_bid_price))

        # Check which quotes need refreshing
        active_yes = [q for q in state.quotes if q.side == "yes" and not q.filled]
        active_no = [q for q in state.quotes if q.side == "no" and not q.filled]

        yes_needs_update = (
            not active_yes or
            any(abs(q.price - yes_bid_price) >= self.stale_price_threshold_cents for q in active_yes) or
            any(q.age_seconds() > self.stale_quote_seconds for q in active_yes)
        )
        no_needs_update = (
            not active_no or
            any(abs(q.price - no_bid_price) >= self.stale_price_threshold_cents for q in active_no) or
            any(q.age_seconds() > self.stale_quote_seconds for q in active_no)
        )

        # Check inventory limits per market
        yes_ok = state.yes_position < self.max_inventory_per_market
        no_ok = state.no_position < self.max_inventory_per_market

        # Cancel and replace YES quotes
        if yes_needs_update and yes_ok:
            for q in active_yes:
                if q.order_id:
                    try:
                        await client.cancel_order(q.order_id)
                    except Exception:
                        pass
            state.quotes = [q for q in state.quotes if q.side != "yes" or q.filled]

            try:
                client_id = f"mm-y-{state.ticker[:8]}-{uuid.uuid4().hex[:6]}"
                result = await client.place_order(
                    ticker=state.ticker,
                    side="yes",
                    action="buy",
                    yes_price=yes_bid_price,
                    count=self.contracts_per_level,
                    order_type="limit",
                    client_order_id=client_id,
                )
                order = result.get("order", {})
                quote = QuoteLevel(
                    side="yes",
                    price=yes_bid_price,
                    count=self.contracts_per_level,
                    order_id=order.get("order_id", client_id),
                    client_id=client_id,
                    strategy=strategy,
                )
                state.quotes.append(quote)
            except Exception as e:
                logger.error(f"MM YES quote failed on {state.ticker}: {e}")

        # Cancel and replace NO quotes
        if no_needs_update and no_ok:
            for q in active_no:
                if q.order_id:
                    try:
                        await client.cancel_order(q.order_id)
                    except Exception:
                        pass
            state.quotes = [q for q in state.quotes if q.side != "no" or q.filled]

            try:
                client_id = f"mm-n-{state.ticker[:8]}-{uuid.uuid4().hex[:6]}"
                result = await client.place_order(
                    ticker=state.ticker,
                    side="no",
                    action="buy",
                    no_price=no_bid_price,
                    count=self.contracts_per_level,
                    order_type="limit",
                    client_order_id=client_id,
                )
                order = result.get("order", {})
                quote = QuoteLevel(
                    side="no",
                    price=no_bid_price,
                    count=self.contracts_per_level,
                    order_id=order.get("order_id", client_id),
                    client_id=client_id,
                    strategy=strategy,
                )
                state.quotes.append(quote)
            except Exception as e:
                logger.error(f"MM NO quote failed on {state.ticker}: {e}")

    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================

    async def _cancel_all_orders(self):
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        for state in self._markets.values():
            for q in state.quotes:
                if q.order_id and not q.filled:
                    try:
                        await client.cancel_order(q.order_id)
                    except Exception as e:
                        logger.error(f"Failed to cancel {q.order_id}: {e}")
            state.quotes = []

    async def flatten_market(self, ticker: str):
        """Close all inventory in a market."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()

        if ticker not in self._markets:
            return {"error": "Market not tracked"}

        state = self._markets[ticker]

        # Cancel quotes
        for q in state.quotes:
            if q.order_id and not q.filled:
                try:
                    await client.cancel_order(q.order_id)
                except Exception:
                    pass
        state.quotes = []

        # Sell inventory
        if state.yes_position > 0:
            try:
                await client.place_order(
                    ticker=ticker, side="yes", action="sell",
                    yes_price=max(1, state.yes_bid - 1),
                    count=state.yes_position,
                )
                state.yes_position = 0
            except Exception as e:
                logger.error(f"Flatten YES failed on {ticker}: {e}")

        if state.no_position > 0:
            try:
                await client.place_order(
                    ticker=ticker, side="no", action="sell",
                    no_price=max(1, state.no_bid - 1),
                    count=state.no_position,
                )
                state.no_position = 0
            except Exception as e:
                logger.error(f"Flatten NO failed on {ticker}: {e}")

        return {"status": "flattened", "ticker": ticker}

    async def flatten_all(self):
        for ticker in list(self._markets.keys()):
            await self.flatten_market(ticker)
        await self._alert("Market maker: flattened all positions")

    # =========================================================================
    # ALERTS
    # =========================================================================

    async def _alert(self, message: str):
        if not self.telegram_alerts:
            return
        try:
            from app.services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(f"<b>\U0001F3ED Market Maker</b>\n{message}")
        except Exception:
            pass

    # =========================================================================
    # STATUS & DATA
    # =========================================================================

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "kill_switch": self._kill_switch,
            "cycle_count": self._cycle_count,
            "start_time": self._start_time,
            "total_pnl_cents": self._total_pnl_cents,
            "total_pnl_usd": round(self._total_pnl_cents / 100, 2),
            "total_spread_captured": self._total_spread_captured,
            "total_adverse_selections": self._total_adverse_selections,
            "total_fills": len(self._all_fills),
            "active_markets": len(self._markets),
            "default_strategy": self.default_strategy,
            "max_markets": self.max_markets,
            "config": {
                "base_spread_cents": self.base_spread_cents,
                "dynamic_spread": self.dynamic_spread,
                "contracts_per_level": self.contracts_per_level,
                "quote_levels": self.quote_levels,
                "max_inventory": self.max_inventory_per_market,
                "max_total_exposure_cents": self.max_total_exposure_cents,
                "poll_interval": self.poll_interval,
            },
            "markets": {
                ticker: state.to_dict()
                for ticker, state in self._markets.items()
            },
        }

    def get_fills(self, limit: int = 50) -> list[dict]:
        return [f.to_dict() for f in self._all_fills[-limit:]]

    def get_pnl_by_market(self) -> list[dict]:
        return [
            {
                "ticker": state.ticker,
                "title": state.title,
                "realized_pnl_cents": state.realized_pnl_cents,
                "spread_captured_cents": state.spread_captured_cents,
                "total_fills": len(state.fills),
                "adverse_selections": state.adverse_selection_count,
                "strategy": state.active_strategy,
                "score": round(state.score, 2),
            }
            for state in sorted(
                self._markets.values(),
                key=lambda m: m.realized_pnl_cents,
                reverse=True,
            )
        ]

    def get_pnl_by_strategy(self) -> dict:
        """Attribute P&L to each strategy based on fills."""
        strat_pnl: dict[str, dict] = {}
        for f in self._all_fills:
            if f.strategy not in strat_pnl:
                strat_pnl[f.strategy] = {"fills": 0, "volume_cents": 0}
            strat_pnl[f.strategy]["fills"] += f.count
            strat_pnl[f.strategy]["volume_cents"] += f.price * f.count
        return strat_pnl


_mm: Optional[KalshiMarketMaker] = None


def get_market_maker() -> KalshiMarketMaker:
    global _mm
    if _mm is None:
        _mm = KalshiMarketMaker()
    return _mm
