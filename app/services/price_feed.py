"""Real-time price feed via Binance WebSocket + CoinGecko polling fallback."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import get

logger = logging.getLogger("bot.price_feed")


@dataclass
class PriceData:
    """Snapshot of a token's price and 24h stats."""

    price: float = 0.0
    change_24h: float = 0.0
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    volume_24h: Optional[float] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "change_24h": self.change_24h,
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "volume_24h": self.volume_24h,
        }


# Mapping of token symbol -> Binance stream symbol (lowercase for stream names)
#
# Per CLAUDE.md hard rule #8 (monitor price source must match execution source):
# - Binance.US-routed tokens (FLOKI): Binance.US WS = exact execution price ✓
# - Solana-routed (most others): Binance.US WS as close-enough proxy. The 2026-05-06
#   incident was a zombie-listing (JUPUSDT untraded on Binance.US) — actively traded
#   tokens like BTC/ETH/DOGE/PNUT track Solana within bps. JupiterClient.get_token_price
#   falls back via Jupiter aggregator for Solana SPL tokens if WS feed is missing them.
# - EVM-routed (UNI/ARB/LDO/COMP/AAVE/LINK): Binance.US WS as close-enough proxy. Future
#   improvement: route to OpenOcean quote for exact Arbitrum-side price, but high-volume
#   Binance.US listings track Arbitrum closely.
#
# 2026-05-10: added 10 tokens (LINK/UNI/AAVE/COMP/LDO/ARB/FLOKI/BTC/OP/DOGE) after
# audit found 9 of 19 alerted tokens had NO price source — TP/SL would never fire.
BINANCE_TOKENS = {
    "SOL": "solusdt",
    "JTO": "jtousdt",
    "BONK": "bonkusdt",
    "ETH": "ethusdt",
    "ORCA": "orcausdt",
    "JUP": "jupusdt",
    "PENGU": "penguusdt",
    "FARTCOIN": "fartcoinusdt",
    "POPCAT": "popcatusdt",
    "MEW": "mewusdt",
    "PNUT": "pnutusdt",
    "MOODENG": "moodengusdt",
    # 2026-05-10 batch — covers all currently-alerted tokens
    "BTC": "btcusdt",
    "DOGE": "dogeusdt",
    "OP": "opusdt",
    "FLOKI": "flokiusdt",      # Binance.US-routed (exact execution price)
    "UNI": "uniusdt",          # EVM-routed (close-enough proxy)
    "ARB": "arbusdt",          # EVM-routed
    "LDO": "ldousdt",          # EVM-routed
    "COMP": "compusdt",        # EVM-routed
    "AAVE": "aaveusdt",        # EVM-routed
    "LINK": "linkusdt",        # EVM-routed
}

# Reverse lookup: Binance uppercase symbol -> our token symbol
_BINANCE_SYMBOL_MAP = {v.upper(): k for k, v in BINANCE_TOKENS.items()}

# CoinGecko-only tokens (not on Binance.us or delisted)
COINGECKO_ONLY = {
    "PYTH": "pyth-network",
    "RAY": "raydium",
    "WIF": "dogwifcoin",
    "RENDER": "render-token",
    "W": "wormhole",
    "DOG": "dog-go-to-the-moon-rune",
}


class PriceFeed:
    """Real-time price feed using Binance WebSocket for main tokens
    and CoinGecko polling for tokens not listed on Binance."""

    def __init__(self):
        cfg = get("price_feed") or {}
        self.enabled = cfg.get("enabled", False)
        self._ws_base = cfg.get("binance_ws", "wss://stream.binance.us:9443")
        self._cg_poll_seconds = cfg.get("coingecko_poll_seconds", 30)
        self._reconnect_max = cfg.get("reconnect_max_seconds", 30)

        self._prices: dict[str, PriceData] = {}
        self._ws_task: Optional[asyncio.Task] = None
        self._cg_task: Optional[asyncio.Task] = None
        self._running = False
        self._http: Optional[httpx.AsyncClient] = None

    def start(self):
        """Launch WebSocket and CoinGecko polling as background tasks."""
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=15)
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._cg_task = asyncio.create_task(self._cg_loop())
        logger.info(
            "Price feed started (Binance WS for %s, CoinGecko poll for %s)",
            ", ".join(BINANCE_TOKENS.keys()),
            ", ".join(COINGECKO_ONLY.keys()),
        )

    async def stop(self):
        """Shut down all background tasks and connections."""
        self._running = False
        for task in (self._ws_task, self._cg_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("Price feed stopped")

    def get_price(self, symbol: str) -> Optional[PriceData]:
        """Instant lookup — no network call. Returns None if not yet received."""
        return self._prices.get(symbol.upper())

    def get_all_prices(self) -> dict[str, dict]:
        """Return all current prices as plain dicts (dashboard-friendly format)."""
        return {sym: pd.to_dict() for sym, pd in self._prices.items()}

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ #
    # Binance WebSocket
    # ------------------------------------------------------------------ #

    async def _ws_loop(self):
        """Connect to Binance combined stream with auto-reconnect."""
        backoff = 1
        while self._running:
            try:
                await self._ws_connect()
                # If we return cleanly, reset backoff
                backoff = 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance WS error: %s — reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max)

    async def _ws_connect(self):
        """Single WebSocket session against the Binance combined stream."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package not installed — falling back to CoinGecko-only. "
                "Install with: pip install websockets"
            )
            # Park this coroutine so _cg_loop handles everything
            while self._running:
                await asyncio.sleep(60)
            return

        # Seed _prices via REST before opening the WS. Binance.US WS only pushes
        # @ticker on actual trades, so low-volume listings (LDO/COMP/PNUT etc.)
        # might never get an initial tick. Pre-seeding ensures every subscribed
        # token has a baseline price + freshness from the moment the feed starts.
        await self._seed_prices_via_rest()

        streams = "/".join(f"{s}@ticker" for s in BINANCE_TOKENS.values())
        url = f"{self._ws_base}/stream?streams={streams}"
        logger.info("Connecting to Binance WS: %s", url)

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            logger.info("Binance WS connected")
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # No data in 30s — send a pong to keep alive; loop back
                    continue

                try:
                    msg = json.loads(raw)
                    data = msg.get("data", msg)  # combined stream wraps in {"stream":..,"data":..}
                    if data.get("e") == "24hrTicker":
                        self._handle_ticker(data)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug("WS parse error: %s", e)

    async def _seed_prices_via_rest(self):
        """Bootstrap _prices for all subscribed tokens via Binance.US REST.

        WS @ticker events only fire on trades. Low-volume listings (LDO/COMP/
        PNUT/JUP at $1-$500/24h on Binance.US) may go hours without a trade,
        leaving _prices empty for them and the dashboard's per-token Feed dot
        showing "—" even though we ARE subscribed. This REST seed gives every
        token an initial baseline at WS-connect time. Subsequent WS ticks
        refresh as trades fire.

        One REST call (batch ticker), no per-token spam.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        try:
            symbols = [s.upper() for s in BINANCE_TOKENS.values()]
            resp = await self._http.get(
                "https://api.binance.us/api/v3/ticker/24hr",
                params={"symbols": json.dumps(symbols, separators=(",", ":"))},
            )
            resp.raise_for_status()
            data = resp.json() if not isinstance(resp.json(), dict) else [resp.json()]
            seeded = 0
            for ticker in data:
                self._handle_ticker(ticker)  # reuses parsing + _BINANCE_SYMBOL_MAP
                seeded += 1
            logger.info(f"REST-seeded {seeded} tokens into price feed")
        except Exception as e:
            logger.warning(f"REST price seed failed: {e} — WS will populate as trades occur")

    def _handle_ticker(self, data: dict):
        """Parse a Binance 24hrTicker event and update in-memory prices.

        Accepts both WS @ticker payload shape (lowercase 1-letter keys) and
        REST /ticker/24hr shape (camelCase keys). The REST seed re-uses this
        method by mapping its fields to the same single-letter keys via the
        equivalence: c=lastPrice, P=priceChangePercent, h=highPrice, l=lowPrice,
        q=quoteVolume, s=symbol.
        """
        binance_sym = data.get("s") or data.get("symbol", "")  # e.g. "SOLUSDT"
        token = _BINANCE_SYMBOL_MAP.get(binance_sym.upper())
        if not token:
            return

        # REST shape uses camelCase; WS uses single letters. Read both.
        last = data.get("c") or data.get("lastPrice", 0)
        change = data.get("P") or data.get("priceChangePercent", 0)
        high = data.get("h") or data.get("highPrice", 0)
        low = data.get("l") or data.get("lowPrice", 0)
        quote_vol = data.get("q") or data.get("quoteVolume", 0)

        self._prices[token] = PriceData(
            price=float(last or 0),
            change_24h=float(change or 0),
            high_24h=float(high or 0),
            low_24h=float(low or 0),                 # 24h low
            volume_24h=float(quote_vol or 0),         # 24h quote volume (USDT)
            updated_at=time.time(),
        )

    # ------------------------------------------------------------------ #
    # CoinGecko polling (for PYTH, RAY)
    # ------------------------------------------------------------------ #

    async def _cg_loop(self):
        """Poll CoinGecko for tokens not on Binance."""
        while self._running:
            try:
                await self._cg_fetch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("CoinGecko poll error: %s", e)
            await asyncio.sleep(self._cg_poll_seconds)

    async def _cg_fetch(self):
        """Fetch prices from CoinGecko in a single batched request.

        Combines CoinGecko-only tokens with stale Binance backfills into one API
        call to stay well within free-tier rate limits (~10-30 req/min).
        """
        if not self._http:
            return

        # Build a single ID list: CoinGecko-only tokens + stale Binance backfills
        _BINANCE_BACKFILL_IDS = {
            "SOL": "solana", "JTO": "jito-governance-token",
            "BONK": "bonk", "ETH": "ethereum", "ORCA": "orca",
            "JUP": "jupiter-exchange-solana", "PENGU": "pudgy-penguins",
            "FARTCOIN": "fartcoin", "POPCAT": "popcat",
            "MEW": "cat-in-a-dogs-world", "PNUT": "peanut-the-squirrel",
            "MOODENG": "moo-deng",
        }
        all_cg_ids = {**COINGECKO_ONLY, **_BINANCE_BACKFILL_IDS}
        stale_threshold = time.time() - 120
        needed = {
            sym: cg_id for sym, cg_id in all_cg_ids.items()
            if sym in COINGECKO_ONLY
            or sym not in self._prices
            or self._prices[sym].updated_at < stale_threshold
        }
        if not needed:
            return

        ids_str = ",".join(set(needed.values()))
        try:
            resp = await self._http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": ids_str,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            )
            if resp.status_code == 429:
                # Rate limited — back off, will retry next cycle
                logger.debug("CoinGecko 429 rate limit, backing off")
                self._cg_backoff = min(getattr(self, '_cg_backoff', 0) + 30, 120)
                await asyncio.sleep(self._cg_backoff)
                return
            resp.raise_for_status()
            self._cg_backoff = 0  # Reset backoff on success
        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            logger.debug("CoinGecko fetch failed: %s", e)
            return

        cg_data = resp.json()
        id_to_sym = {cg_id: sym for sym, cg_id in needed.items()}
        for cg_id, vals in cg_data.items():
            sym = id_to_sym.get(cg_id)
            if sym:
                self._prices[sym] = PriceData(
                    price=vals.get("usd", 0),
                    change_24h=vals.get("usd_24h_change", 0),
                    high_24h=None,
                    low_24h=None,
                    volume_24h=None,
                    updated_at=time.time(),
                )


# ------------------------------------------------------------------ #
# Singleton
# ------------------------------------------------------------------ #
_feed: Optional[PriceFeed] = None


def get_price_feed() -> PriceFeed:
    global _feed
    if _feed is None:
        _feed = PriceFeed()
    return _feed
