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
BINANCE_TOKENS = {
    "SOL": "solusdt",
    "JTO": "jtousdt",
    "BONK": "bonkusdt",
    "ETH": "ethusdt",
    "ORCA": "orcausdt",
}

# Reverse lookup: Binance uppercase symbol -> our token symbol
_BINANCE_SYMBOL_MAP = {v.upper(): k for k, v in BINANCE_TOKENS.items()}

# CoinGecko-only tokens (not on Binance.us or delisted)
COINGECKO_ONLY = {
    "PYTH": "pyth-network",
    "RAY": "raydium",
    "WIF": "dogwifcoin",
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

    def _handle_ticker(self, data: dict):
        """Parse a Binance 24hrTicker event and update in-memory prices."""
        binance_sym = data.get("s", "")  # e.g. "SOLUSDT"
        token = _BINANCE_SYMBOL_MAP.get(binance_sym)
        if not token:
            return

        self._prices[token] = PriceData(
            price=float(data.get("c", 0)),          # last price
            change_24h=float(data.get("P", 0)),      # 24h change %
            high_24h=float(data.get("h", 0)),        # 24h high
            low_24h=float(data.get("l", 0)),         # 24h low
            volume_24h=float(data.get("q", 0)),      # 24h quote volume (USDT)
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
        """Fetch prices from CoinGecko for non-Binance tokens."""
        if not self._http or not COINGECKO_ONLY:
            return

        cg_ids = ",".join(COINGECKO_ONLY.values())
        resp = await self._http.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": cg_ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
        )
        resp.raise_for_status()
        cg_data = resp.json()

        id_to_sym = {v: k for k, v in COINGECKO_ONLY.items()}
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

        # Also back-fill Binance tokens via CoinGecko if WS hasn't delivered yet
        all_cg_ids = {
            "SOL": "solana", "JTO": "jito-governance-token",
            "WIF": "dogwifcoin", "BONK": "bonk",
            "ETH": "ethereum", "ORCA": "orca",
        }
        stale_threshold = time.time() - 120  # backfill if no update in 2 min
        missing = [
            sym for sym in all_cg_ids
            if sym not in self._prices or self._prices[sym].updated_at < stale_threshold
        ]
        if missing:
            ids_str = ",".join(all_cg_ids[s] for s in missing)
            try:
                resp2 = await self._http.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={
                        "ids": ids_str,
                        "vs_currencies": "usd",
                        "include_24hr_change": "true",
                    },
                )
                resp2.raise_for_status()
                cg2 = resp2.json()
                id_to_sym2 = {v: k for k, v in all_cg_ids.items()}
                for cg_id, vals in cg2.items():
                    sym = id_to_sym2.get(cg_id)
                    if sym and sym not in self._prices:
                        self._prices[sym] = PriceData(
                            price=vals.get("usd", 0),
                            change_24h=vals.get("usd_24h_change", 0),
                            updated_at=time.time(),
                        )
            except Exception as e:
                logger.debug("CoinGecko backfill for Binance tokens failed: %s", e)


# ------------------------------------------------------------------ #
# Singleton
# ------------------------------------------------------------------ #
_feed: Optional[PriceFeed] = None


def get_price_feed() -> PriceFeed:
    global _feed
    if _feed is None:
        _feed = PriceFeed()
    return _feed
