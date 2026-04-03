"""Kalshi WebSocket feed — real-time orderbook and trade streaming.

Connects to Kalshi's WebSocket API for live orderbook deltas and trade
notifications. Maintains an in-memory orderbook cache that the market
maker and spread bot can read instead of polling the REST API.

Also emits trade events to a shared log for the dashboard live feed.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import get

logger = logging.getLogger("bot.kalshi.ws")

KALSHI_WS_HOSTS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "live": "wss://api.elections.kalshi.com/trade-api/ws/v2",
}


class OrderbookLevel:
    """Single price level in the orderbook."""
    __slots__ = ("price", "quantity")

    def __init__(self, price: float, quantity: float):
        self.price = price
        self.quantity = quantity


class LiveOrderbook:
    """In-memory orderbook for a single market, built from WS snapshots + deltas."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes_levels: dict[str, float] = {}  # price_str -> quantity
        self.no_levels: dict[str, float] = {}
        self.last_updated: Optional[str] = None
        self.seq: int = 0

    def apply_snapshot(self, msg: dict):
        """Reset orderbook from a full snapshot."""
        self.yes_levels.clear()
        self.no_levels.clear()
        for price_str, qty_str in msg.get("yes_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                self.yes_levels[price_str] = qty
        for price_str, qty_str in msg.get("no_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                self.no_levels[price_str] = qty
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def apply_delta(self, msg: dict):
        """Apply an incremental delta to the orderbook."""
        side = msg.get("side", "yes")
        price_str = msg.get("price_dollars", "0")
        delta = float(msg.get("delta_fp", "0"))
        levels = self.yes_levels if side == "yes" else self.no_levels

        current = levels.get(price_str, 0.0)
        new_qty = current + delta
        if new_qty <= 0:
            levels.pop(price_str, None)
        else:
            levels[price_str] = new_qty
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def best_yes_bid(self) -> int:
        """Highest YES bid price in cents."""
        if not self.yes_levels:
            return 0
        return int(round(max(float(p) for p in self.yes_levels) * 100))

    def best_yes_ask(self) -> int:
        """Lowest YES ask (derived from NO bids): 100 - highest NO bid."""
        if not self.no_levels:
            return 0
        best_no = max(float(p) for p in self.no_levels)
        return int(round((1.0 - best_no) * 100))

    def mid_price(self) -> float:
        """Mid price in cents."""
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return 50.0

    def spread(self) -> int:
        """Spread in cents."""
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid > 0 and ask > 0:
            return ask - bid
        return 0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "yes_bid": self.best_yes_bid(),
            "yes_ask": self.best_yes_ask(),
            "mid": self.mid_price(),
            "spread": self.spread(),
            "yes_levels": len(self.yes_levels),
            "no_levels": len(self.no_levels),
            "last_updated": self.last_updated,
        }


class KalshiWSFeed:
    """WebSocket feed manager for Kalshi real-time data."""

    def __init__(self):
        cfg = get("kalshi") or {}
        ws_cfg = cfg.get("websocket", {})

        self.enabled = ws_cfg.get("enabled", True) and cfg.get("enabled", False)
        self.mode = cfg.get("mode", "demo")
        self.api_key_id = cfg.get("api_key_id", "")
        self.private_key_path = cfg.get("private_key_path", "")
        self.max_reconnect_delay = ws_cfg.get("max_reconnect_delay", 60)

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = False
        self._subscribed_tickers: set[str] = set()
        self._orderbooks: dict[str, LiveOrderbook] = {}
        self._trade_log: deque = deque(maxlen=200)
        self._connect_count = 0
        self._message_count = 0
        self._last_message: Optional[str] = None
        self._private_key = None

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Kalshi WS feed starting ({self.mode} mode)")
        return self._task

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._connected = False

    def _load_key(self):
        """Load RSA private key for WS auth."""
        if self._private_key:
            return
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, text: str) -> str:
        """RSA-PSS signature for WS auth headers."""
        message = text.encode("utf-8")
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _ws_headers(self) -> dict:
        """Build authenticated headers for WS handshake."""
        self._load_key()
        ts = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        msg_string = ts + "GET" + path
        signature = self._sign(msg_string)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _run_loop(self):
        """Reconnecting WS loop with exponential backoff."""
        backoff = 5
        while self._running:
            try:
                await self._ws_connect()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Kalshi WS error: {e} — reconnecting in {backoff}s")
                self._connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_reconnect_delay)

    async def _ws_connect(self):
        """Single WebSocket session."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed — Kalshi WS feed disabled")
            self._running = False
            return

        url = KALSHI_WS_HOSTS.get(self.mode, KALSHI_WS_HOSTS["demo"])
        headers = self._ws_headers()

        async with websockets.connect(url, extra_headers=headers, ping_interval=20, ping_timeout=10) as ws:
            self._connected = True
            self._connect_count += 1
            logger.info(f"Kalshi WS connected (#{self._connect_count})")

            # Subscribe to any tickers already requested
            for ticker in list(self._subscribed_tickers):
                await self._send_subscribe(ws, ticker)

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue

                self._message_count += 1
                self._last_message = datetime.now(timezone.utc).isoformat()

                try:
                    data = json.loads(raw)
                    await self._handle_message(data)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug(f"WS parse error: {e}")

    async def _send_subscribe(self, ws, ticker: str):
        """Subscribe to orderbook_delta and trade channels for a ticker."""
        sub_id = hash(ticker) & 0xFFFF
        await ws.send(json.dumps({
            "id": sub_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_ticker": ticker,
            },
        }))
        await ws.send(json.dumps({
            "id": sub_id + 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["trade"],
                "market_ticker": ticker,
            },
        }))
        logger.debug(f"WS subscribed: {ticker}")

    async def subscribe(self, ticker: str):
        """Add a ticker to the subscription set. If connected, subscribes immediately."""
        self._subscribed_tickers.add(ticker)
        if ticker not in self._orderbooks:
            self._orderbooks[ticker] = LiveOrderbook(ticker)

    async def unsubscribe(self, ticker: str):
        """Remove a ticker from the subscription set."""
        self._subscribed_tickers.discard(ticker)
        self._orderbooks.pop(ticker, None)

    async def _handle_message(self, data: dict):
        """Route incoming WS messages."""
        msg_type = data.get("type")
        msg = data.get("msg", {})
        seq = data.get("seq", 0)
        ticker = msg.get("market_ticker", "")

        if msg_type == "orderbook_snapshot":
            if ticker in self._orderbooks:
                self._orderbooks[ticker].apply_snapshot(msg)
                self._orderbooks[ticker].seq = seq
                logger.debug(f"WS snapshot: {ticker}")

        elif msg_type == "orderbook_delta":
            if ticker in self._orderbooks:
                book = self._orderbooks[ticker]
                if seq > book.seq:
                    book.apply_delta(msg)
                    book.seq = seq

        elif msg_type == "trade":
            trade = {
                "ticker": ticker,
                "side": msg.get("side", "?"),
                "count": msg.get("count", 0),
                "price_cents": int(round(float(msg.get("yes_price", "0") or "0") * 100)),
                "ts": msg.get("ts") or datetime.now(timezone.utc).isoformat(),
                "taker_side": msg.get("taker_side", ""),
            }
            self._trade_log.appendleft(trade)

        elif msg_type == "error":
            code = msg.get("code", "?")
            error_msg = msg.get("msg", "unknown")
            logger.error(f"WS error {code}: {error_msg}")

    # ── Public API ──

    def get_orderbook(self, ticker: str) -> Optional[LiveOrderbook]:
        """Get the live orderbook for a ticker (or None if not subscribed)."""
        return self._orderbooks.get(ticker)

    def get_trade_log(self, limit: int = 50) -> list[dict]:
        """Get recent trades from the WS feed."""
        return list(self._trade_log)[:limit]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "connected": self._connected,
            "connect_count": self._connect_count,
            "message_count": self._message_count,
            "last_message": self._last_message,
            "subscribed_tickers": list(self._subscribed_tickers),
            "orderbooks": {
                t: book.to_dict() for t, book in self._orderbooks.items()
            },
            "trade_log_size": len(self._trade_log),
        }


# Singleton
_ws_feed: Optional[KalshiWSFeed] = None


def get_kalshi_ws_feed() -> KalshiWSFeed:
    global _ws_feed
    if _ws_feed is None:
        _ws_feed = KalshiWSFeed()
    return _ws_feed
