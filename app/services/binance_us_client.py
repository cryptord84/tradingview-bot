"""Binance.US REST API client — read-only Phase 1.

Authenticated requests via HMAC-SHA256 signing. No external library — uses
httpx like the existing data layer. Keeps dependency surface minimal.

Phase 1 scope: account info / balance reads, server time + ping for health.
Trading methods come in Phase 2.

Security:
  - API key + secret loaded from env vars (.env). Never log or echo them.
  - Withdrawal permissions MUST be disabled on the API key.
  - IP whitelist recommended (handle ISP changes via periodic health check).

Config (config.yaml):
    binance_us:
      enabled: true
      base_url: https://api.binance.us
      timeout_seconds: 15
      health_check_interval_minutes: 30  # how often to ping for connectivity

.env:
    BINANCE_US_API_KEY=<your_api_key>
    BINANCE_US_API_SECRET=<your_api_secret>
"""
import os
import time
import hmac
import hashlib
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import get

logger = logging.getLogger("bot.binance_us")


class BinanceUSClient:
    """Phase-1 read-only client. Phase 2 will add place_order / cancel / get_fills."""

    def __init__(self):
        cfg = get("binance_us") or {}
        self.enabled = cfg.get("enabled", False)
        self.base_url = cfg.get("base_url", "https://api.binance.us")
        timeout = cfg.get("timeout_seconds", 15)

        # Secrets from env — NEVER log/echo
        self.api_key = os.getenv("BINANCE_US_API_KEY", "")
        self.api_secret = os.getenv("BINANCE_US_API_SECRET", "")

        if self.enabled and (not self.api_key or not self.api_secret):
            logger.warning(
                "Binance.US enabled in config but BINANCE_US_API_KEY / "
                "BINANCE_US_API_SECRET missing from env. Disabling client."
            )
            self.enabled = False

        self._client = httpx.AsyncClient(timeout=timeout)

    # ── HMAC signing ──────────────────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        """Return signed query string with HMAC-SHA256 signature appended."""
        # Add timestamp + recv window
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={sig}"

    async def _signed_get(self, path: str, params: Optional[dict] = None) -> dict:
        """Authenticated GET with signed query string."""
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled or unconfigured"}
        try:
            url = f"{self.base_url}{path}?{self._sign(params or {})}"
            r = await self._client.get(url, headers={"X-MBX-APIKEY": self.api_key})
            if r.status_code != 200:
                return {
                    "success": False,
                    "status_code": r.status_code,
                    "error": r.text[:300],
                }
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    async def _public_get(self, path: str, params: Optional[dict] = None) -> dict:
        """Unauthenticated GET (ping, server time, etc.)."""
        try:
            url = f"{self.base_url}{path}"
            r = await self._client.get(url, params=params)
            if r.status_code != 200:
                return {"success": False, "status_code": r.status_code, "error": r.text[:300]}
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    # ── Health checks ─────────────────────────────────────────────────────
    async def ping(self) -> bool:
        r = await self._public_get("/api/v3/ping")
        return r.get("success", False)

    async def get_server_time_skew_ms(self) -> Optional[int]:
        """Returns local-vs-server clock skew in ms. >2000ms causes signed reqs to fail."""
        r = await self._public_get("/api/v3/time")
        if not r.get("success"):
            return None
        server_ms = r["data"].get("serverTime", 0)
        local_ms = int(time.time() * 1000)
        return server_ms - local_ms

    async def health_check(self) -> dict:
        """Combined: ping + time + auth probe (account balance read).
        Returns dict with each check's pass/fail. Used by the periodic
        cron to detect IP whitelist breaks (ISP changes), key revocation,
        or server outages.
        """
        out = {"success": True}
        out["ping_ok"] = await self.ping()
        out["time_skew_ms"] = await self.get_server_time_skew_ms()

        if not self.enabled or not self.api_key:
            out["auth_ok"] = False
            out["auth_error"] = "client disabled (no API key configured)"
            out["success"] = False
            return out

        # Auth probe — if this fails, key is revoked OR IP not whitelisted
        acct = await self.get_account_info()
        out["auth_ok"] = acct.get("success", False)
        if not out["auth_ok"]:
            out["auth_error"] = acct.get("error", "unknown")
            out["success"] = False
        return out

    # ── Account / balances ────────────────────────────────────────────────
    async def get_account_info(self) -> dict:
        """Full account info — permissions, balances, fees."""
        return await self._signed_get("/api/v3/account")

    async def get_balances(self) -> dict:
        """Returns {asset: {free, locked}} for all non-zero balances."""
        r = await self.get_account_info()
        if not r.get("success"):
            return r
        out = {}
        for b in r["data"].get("balances", []):
            free = float(b.get("free", 0))
            locked = float(b.get("locked", 0))
            if free > 0 or locked > 0:
                out[b["asset"]] = {"free": free, "locked": locked}
        return {"success": True, "data": out}

    async def get_balance(self, asset: str) -> float:
        """Convenience: free balance for a single asset (e.g. 'USDT')."""
        r = await self.get_balances()
        if not r.get("success"):
            return 0.0
        return r["data"].get(asset, {}).get("free", 0.0)

    async def get_total_usdt_value(self) -> float:
        """Sum of free USDT across stable assets (USDT, USD, USDC).

        Useful for inclusion in tradeable_usd. Treats USD/USDC as 1:1 with USDT.
        """
        r = await self.get_balances()
        if not r.get("success"):
            return 0.0
        bal = r["data"]
        total = 0.0
        for asset in ("USDT", "USD", "USDC"):
            total += bal.get(asset, {}).get("free", 0.0)
        return total

    # ── Symbol metadata (filters) ─────────────────────────────────────────
    _exchange_info_cache: Optional[dict] = None

    async def get_symbol_info(self, symbol: str) -> dict:
        """Returns symbol filters (lot size, min notional, step size).

        Cached on first call — exchangeInfo is large but stable across hours.
        """
        if BinanceUSClient._exchange_info_cache is None:
            r = await self._public_get("/api/v3/exchangeInfo")
            if not r.get("success"):
                return {"success": False, "error": r.get("error")}
            BinanceUSClient._exchange_info_cache = r["data"]
        for s in BinanceUSClient._exchange_info_cache.get("symbols", []):
            if s.get("symbol") == symbol:
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                return {
                    "success": True,
                    "status": s.get("status"),
                    "base_asset": s.get("baseAsset"),
                    "quote_asset": s.get("quoteAsset"),
                    "base_precision": s.get("baseAssetPrecision"),
                    "quote_precision": s.get("quoteAssetPrecision"),
                    "min_qty": float(filters.get("LOT_SIZE", {}).get("minQty", 0)),
                    "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", 0)),
                    "min_notional": float(
                        filters.get("MIN_NOTIONAL", {}).get("minNotional",
                            filters.get("NOTIONAL", {}).get("minNotional", 0))
                    ),
                    "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0)),
                }
        return {"success": False, "error": f"symbol {symbol} not in exchangeInfo"}

    async def get_ticker_price(self, symbol: str) -> Optional[float]:
        """Latest ticker price (no auth needed)."""
        r = await self._public_get("/api/v3/ticker/price", {"symbol": symbol})
        if not r.get("success"):
            return None
        try:
            return float(r["data"].get("price", 0))
        except Exception:
            return None

    async def get_book_ticker(self, symbol: str) -> dict:
        """Best bid/ask — limit-order pricing needs the touch, not last trade."""
        r = await self._public_get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        if not r.get("success"):
            return {"success": False, "error": r.get("error")}
        try:
            d = r["data"]
            return {
                "success": True,
                "bid": float(d["bidPrice"]), "ask": float(d["askPrice"]),
                "bid_qty": float(d["bidQty"]), "ask_qty": float(d["askQty"]),
            }
        except Exception as e:
            return {"success": False, "error": f"bookTicker parse: {e}"}

    # ── Trading (Phase 2) ─────────────────────────────────────────────────
    async def place_market_buy_quote(self, symbol: str, quote_quantity: float) -> dict:
        """Market BUY using a USDT-denominated quote amount.

        Example: place_market_buy_quote('INJUSDT', 15.0) buys $15 worth of INJ.
        Uses `quoteOrderQty` parameter — Binance figures out the base quantity.
        Min notional varies per symbol (typically $5–10).
        """
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled"}
        # Round to 2 decimals (USDT has 2-cent precision in practice)
        quote_quantity = round(quote_quantity, 2)
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": quote_quantity,
        }
        return await self._signed_post("/api/v3/order", params)

    async def place_market_sell_base(self, symbol: str, base_quantity: float) -> dict:
        """Market SELL using base asset quantity (e.g. sell 0.5 INJ).

        Quantity must be aligned to the symbol's stepSize filter; caller is
        responsible for rounding via `quantize_qty()` below.
        """
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled"}
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": base_quantity,
        }
        return await self._signed_post("/api/v3/order", params)

    async def get_order(self, symbol: str, order_id: int) -> dict:
        """Look up an order by id — used for fill confirmation."""
        return await self._signed_get("/api/v3/order", {"symbol": symbol, "orderId": order_id})

    async def get_open_orders(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return await self._signed_get("/api/v3/openOrders", params)

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self._signed_delete("/api/v3/order", {"symbol": symbol, "orderId": order_id})

    async def get_my_trades(self, symbol: str, limit: int = 50) -> dict:
        """Recent fills for a symbol — used for P&L computation + dashboard."""
        return await self._signed_get(
            "/api/v3/myTrades",
            {"symbol": symbol, "limit": limit},
        )

    async def place_limit_order(self, symbol: str, side: str, base_quantity: float,
                                price: float, time_in_force: str = "GTC",
                                test: bool = False) -> dict:
        """LIMIT order with base quantity. Caller must pre-quantize qty (stepSize)
        and price (tickSize). `test=True` hits /api/v3/order/test — full filter +
        signature validation server-side, nothing placed."""
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled"}
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": base_quantity,
            "price": f"{price:.8f}".rstrip("0").rstrip("."),
        }
        endpoint = "/api/v3/order/test" if test else "/api/v3/order"
        return await self._signed_post(endpoint, params)

    @staticmethod
    def quantize_qty(qty: float, step_size: float) -> float:
        """Round a base-asset quantity DOWN to the symbol's step size.

        Binance rejects orders whose quantity isn't aligned to LOT_SIZE.stepSize.
        e.g. step=0.01 → qty 0.123 → 0.12.
        """
        if step_size <= 0:
            return qty
        # Floor-divide to nearest step
        steps = int(qty / step_size)
        return round(steps * step_size, 8)

    @staticmethod
    def quantize_price(price: float, tick_size: float, up: bool = False) -> float:
        """Align a price to the symbol's tickSize (floor unless `up`)."""
        if tick_size <= 0:
            return price
        import math
        ticks = (math.ceil if up else math.floor)(price / tick_size)
        return round(ticks * tick_size, 8)

    # ── Internal helpers for signed POST/DELETE ───────────────────────────
    async def _signed_post(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled or unconfigured"}
        try:
            url = f"{self.base_url}{path}?{self._sign(params or {})}"
            r = await self._client.post(url, headers={"X-MBX-APIKEY": self.api_key})
            if r.status_code != 200:
                return {"success": False, "status_code": r.status_code, "error": r.text[:300]}
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    async def _signed_delete(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.enabled:
            return {"success": False, "error": "binance_us disabled or unconfigured"}
        try:
            url = f"{self.base_url}{path}?{self._sign(params or {})}"
            r = await self._client.delete(url, headers={"X-MBX-APIKEY": self.api_key})
            if r.status_code != 200:
                return {"success": False, "status_code": r.status_code, "error": r.text[:300]}
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    async def close(self):
        await self._client.aclose()
