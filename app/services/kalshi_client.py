"""Kalshi API client for binary event contract trading.

Uses the official kalshi-python SDK (v2.1.4) with RSA key authentication.
Supports both demo (sandbox) and production environments.

Rate limiting: built-in token-bucket limiter prevents API throttling when
multiple bots share the same client.  Default 10 req/s (configurable).

Async wrapper: ``AsyncKalshiClient`` offloads every sync SDK call to a
thread via ``asyncio.to_thread`` so async bots never block the event loop.
"""

import asyncio
import base64
import collections
import functools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_python import (
    ApiClient,
    Configuration,
    EventsApi,
    KalshiClient,
    MarketsApi,
    PortfolioApi,
)

from app.config import get

logger = logging.getLogger("bot.kalshi")


# ─── Rate Limiter ────────────────────────────────────────────────────────────

class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter.

    Allows `rate` requests per second with a burst capacity of `burst`.
    Callers block (sleep) until a token is available.
    """

    def __init__(self, rate: float = 10.0, burst: int = 15):
        self._rate = rate          # tokens added per second
        self._burst = burst        # max tokens in bucket
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)

# API base URLs
KALSHI_HOSTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "live": "https://api.elections.kalshi.com/trade-api/v2",
}


class KalshiTradingClient:
    """Client for Kalshi event contract trading."""

    def __init__(self):
        cfg = get("kalshi") or {}
        self.enabled = cfg.get("enabled", False)
        self.mode = cfg.get("mode", "demo")  # "demo" or "live"
        self.api_key_id = cfg.get("api_key_id", "")
        self.private_key_path = cfg.get("private_key_path", "")
        self.max_cost_per_trade = cfg.get("max_cost_per_trade_cents", 100)  # $1 default
        self.max_total_exposure = cfg.get("max_total_exposure_cents", 500)  # $5 default
        self.max_open_positions = cfg.get("max_open_positions", 10)
        self.default_count = cfg.get("default_contract_count", 5)

        self._client: Optional[ApiClient] = None
        self._portfolio: Optional[PortfolioApi] = None
        self._markets: Optional[MarketsApi] = None
        self._events: Optional[EventsApi] = None

        # Rate limiter — shared across all bots using this singleton
        rate_limit = cfg.get("rate_limit_per_second", 10)
        burst = cfg.get("rate_limit_burst", 15)
        self._limiter = TokenBucketRateLimiter(rate=rate_limit, burst=burst)

        # Order failure tracker — ring buffer of last 200 failures
        self._order_failures: collections.deque = collections.deque(maxlen=200)
        self._order_success_count = 0
        self._order_failure_count = 0

    def _ensure_client(self):
        """Lazily initialize the API client with RSA auth."""
        if self._client is not None:
            return

        if not self.api_key_id or not self.private_key_path:
            raise RuntimeError(
                "Kalshi API key ID and private key path must be set in config.yaml"
            )

        host = KALSHI_HOSTS.get(self.mode, KALSHI_HOSTS["demo"])
        config = Configuration(host=host)
        self._client = ApiClient(configuration=config)

        # Set RSA key authentication
        kalshi_client = KalshiClient(configuration=config)
        kalshi_client.set_kalshi_auth(
            key_id=self.api_key_id,
            private_key_path=self.private_key_path,
        )
        self._client = kalshi_client

        self._portfolio = PortfolioApi(self._client)
        self._markets = MarketsApi(self._client)
        self._events = EventsApi(self._client)

        logger.info(f"Kalshi client initialized ({self.mode} mode)")

    # =========================================================================
    # ACCOUNT
    # =========================================================================

    def _rate_limit(self):
        """Acquire a rate-limit token before making an API call."""
        self._limiter.acquire()

    def get_balance(self) -> dict:
        """Get account balance in cents."""
        self._ensure_client()
        self._rate_limit()
        resp = self._portfolio.get_balance()
        balance_data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        logger.info(f"Kalshi balance: {balance_data}")
        return balance_data

    # =========================================================================
    # MARKETS / EVENTS
    # =========================================================================

    def get_events(self, status: str = "open", limit: int = 20) -> list:
        """Get available events. Status: open, closed, settled."""
        self._ensure_client()
        self._rate_limit()
        resp = self._events.get_events(status=status, limit=limit)
        events = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return events.get("events", [])

    def get_event(self, event_ticker: str) -> dict:
        """Get details for a specific event."""
        self._ensure_client()
        self._rate_limit()
        resp = self._events.get_event(event_ticker=event_ticker)
        return resp.to_dict() if hasattr(resp, "to_dict") else resp

    def get_markets(
        self,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 50,
    ) -> list:
        """Get markets, optionally filtered by event."""
        self._ensure_client()
        self._rate_limit()
        kwargs = {"status": status, "limit": limit}
        if event_ticker:
            kwargs["event_ticker"] = event_ticker
        resp = self._markets.get_markets(**kwargs)
        markets = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return markets.get("markets", [])

    def get_market(self, ticker: str) -> dict:
        """Get details for a specific market."""
        self._ensure_client()
        self._rate_limit()
        resp = self._markets.get_market(ticker=ticker)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("market", data)

    def get_orderbook(self, ticker: str) -> dict:
        """Get order book for a market."""
        self._ensure_client()
        self._rate_limit()
        resp = self._markets.get_market_orderbook(ticker=ticker)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("orderbook", data)

    def get_market_trades(self, ticker: str, limit: int = 20) -> list:
        """Get recent trades for a market."""
        self._ensure_client()
        self._rate_limit()
        resp = self._markets.get_trades(ticker=ticker, limit=limit)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("trades", [])

    def _api_get(self, path: str) -> dict:
        """Direct authenticated GET to the Kalshi API (bypasses SDK)."""
        self._rate_limit()
        host = KALSHI_HOSTS.get(self.mode, KALSHI_HOSTS["demo"]).replace("/trade-api/v2", "")
        full_path = "/trade-api/v2" + path

        with open(self.private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)

        ts = str(int(time.time() * 1000))
        msg = (ts + "GET" + full_path).encode()
        sig = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
        resp = requests.get(host + full_path, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_recent_trades(self, limit: int = 100, ticker: Optional[str] = None) -> list:
        """Get recent trades with full data (count, price) via direct API.

        The SDK strips count/price fields, so this bypasses it.
        """
        self._ensure_client()
        path = f"/markets/trades?limit={limit}"
        if ticker:
            path += f"&ticker={ticker}"
        data = self._api_get(path)
        return data.get("trades", [])

    def get_markets_full(self, status: str = "open", limit: int = 200,
                         event_ticker: Optional[str] = None,
                         series_ticker: Optional[str] = None) -> list:
        """Get markets with full pricing data via direct API.

        The SDK strips yes_ask, no_ask, volume, and other pricing fields.
        Use series_ticker (e.g. "KXNBA") to target specific market series.
        """
        self._ensure_client()
        path = f"/markets?status={status}&limit={limit}"
        if event_ticker:
            path += f"&event_ticker={event_ticker}"
        if series_ticker:
            path += f"&series_ticker={series_ticker}"
        data = self._api_get(path)
        return data.get("markets", [])

    # Known active series with real liquidity (updated Apr 2026)
    ACTIVE_SERIES = [
        "KXNBA", "KXNHL", "KXMLB",  # Sports — highest volume
        "KXFED", "KXCPI", "KXGDP",  # Economics
        "KXBTC", "KXBTCD", "KXETH", "KXETHD",  # Crypto daily
        "KXINX", "KXINXD",  # S&P 500
    ]

    def discover_active_markets(self, min_volume: int = 10,
                                 limit_per_series: int = 50,
                                 max_days_to_close: int = 0) -> list:
        """Discover markets with real liquidity across known active series.

        The generic /markets endpoint is flooded with zero-volume MVE parlays.
        This queries each active series individually to find tradeable markets.

        Args:
            min_volume: Minimum volume to include a market.
            limit_per_series: Max markets to fetch per series.
            max_days_to_close: If > 0, exclude markets closing more than this many
                               days from now (e.g., 7 = only markets closing within a week).
        """
        self._ensure_client()
        from datetime import datetime, timezone, timedelta
        cutoff = None
        if max_days_to_close > 0:
            cutoff = datetime.now(timezone.utc) + timedelta(days=max_days_to_close)

        all_markets = []
        for series in self.ACTIVE_SERIES:
            try:
                markets = self.get_markets_full(
                    status="open", limit=limit_per_series,
                    series_ticker=series,
                )
                for m in markets:
                    vol = float(m.get("volume_fp", "0") or "0")
                    if vol < min_volume:
                        continue
                    # Filter by close date if configured
                    if cutoff:
                        close_time = m.get("close_time") or m.get("expected_expiration_time") or ""
                        if close_time:
                            try:
                                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                                if close_dt > cutoff:
                                    continue
                            except (ValueError, TypeError):
                                pass
                    m["_series"] = series
                    all_markets.append(m)
            except Exception as e:
                logger.warning(f"Failed to fetch series {series}: {e}")
        all_markets.sort(
            key=lambda m: float(m.get("volume_fp", "0") or "0"), reverse=True
        )
        logger.info(
            f"Market discovery: {len(all_markets)} active markets across "
            f"{len(self.ACTIVE_SERIES)} series"
            + (f" (max {max_days_to_close}d to close)" if max_days_to_close else "")
        )
        return all_markets

    def get_market_full(self, ticker: str) -> dict:
        """Get a single market with full pricing data via direct API."""
        self._ensure_client()
        data = self._api_get(f"/markets/{ticker}")
        return data.get("market", {})

    @staticmethod
    def _extract_series(ticker: str) -> str:
        """Extract series ticker from a market ticker (e.g. KXNBA-26-SAS -> KXNBA)."""
        # Series is typically the first segment before the first dash+digit
        parts = ticker.split("-")
        return parts[0] if parts else ticker

    def get_candlesticks(
        self, ticker: str, period_interval: int = 60, limit: int = 100
    ) -> list:
        """Get candlestick data. period_interval: 1 (1min), 60 (1hr), 1440 (1day).

        Returns normalized candles with flat {open, high, low, close, volume} in cents.
        """
        self._ensure_client()
        series = self._extract_series(ticker)
        end_ts = int(time.time())
        # Estimate start_ts from limit and period
        start_ts = end_ts - (limit * period_interval * 60)
        path = (
            f"/series/{series}/markets/{ticker}/candlesticks"
            f"?start_ts={start_ts}&end_ts={end_ts}&period_interval={period_interval}"
        )
        data = self._api_get(path)
        raw = data.get("candlesticks", [])
        # Normalize from new API format (nested dicts with _dollars strings)
        # to flat format expected by bots ({open, high, low, close, volume} in cents)
        normalized = []
        for c in raw:
            price = c.get("price", {})
            if isinstance(price, dict):
                normalized.append({
                    "open": int(round(float(price.get("open_dollars", "0") or "0") * 100)),
                    "high": int(round(float(price.get("high_dollars", "0") or "0") * 100)),
                    "low": int(round(float(price.get("low_dollars", "0") or "0") * 100)),
                    "close": int(round(float(price.get("close_dollars", "0") or "0") * 100)),
                    "volume": int(float(c.get("volume_fp", "0") or "0")),
                    "ts": c.get("end_period_ts", 0),
                })
            else:
                # Already flat format (legacy)
                normalized.append(c)
        return normalized

    def search_markets(self, query: str, limit: int = 20) -> list:
        """Search for markets by keyword in title/description."""
        self._ensure_client()
        all_markets = self.get_markets(status="open", limit=200)
        query_lower = query.lower()
        matches = [
            m for m in all_markets
            if query_lower in (m.get("title", "") + " " + m.get("subtitle", "")).lower()
        ]
        return matches[:limit]

    # =========================================================================
    # ORDERS
    # =========================================================================

    def place_order(
        self,
        ticker: str,
        side: str,
        action: str = "buy",
        count: Optional[int] = None,
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
        expiration_ts: Optional[int] = None,
    ) -> dict:
        """Place an order on a market.

        Args:
            ticker: Market ticker (e.g., "KXBTC-25MAR31-T100000")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts (default from config)
            yes_price: Price in cents (1-99) for yes side
            no_price: Price in cents (1-99) for no side
            order_type: "limit" or "market" (IOC)
            client_order_id: Optional idempotency key
            expiration_ts: Optional order expiration (unix timestamp)
        """
        self._ensure_client()
        count = count or self.default_count

        # Build order request
        order_kwargs = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            order_kwargs["yes_price"] = yes_price
        if no_price is not None:
            order_kwargs["no_price"] = no_price
        if client_order_id:
            order_kwargs["client_order_id"] = client_order_id
        if expiration_ts:
            order_kwargs["expiration_ts"] = expiration_ts

        # Safety checks
        price = yes_price if side == "yes" else no_price
        order_cost = (price * count) if price else 0

        # 0. Global risk gate — circuit breaker + category limit check
        if action == "buy" and order_cost > 0:
            try:
                from app.services.kalshi_risk_manager import get_risk_manager
                rm = get_risk_manager()
                if rm.enabled:
                    gate = rm.check_order(
                        order_cost,
                        bot_name=client_order_id or "unknown",
                        ticker=ticker,
                    )
                    if not gate["allowed"]:
                        raise ValueError(f"Risk gate blocked: {gate['reason']}")

                    # Liquidity-based size cap
                    max_liq_size = rm.get_max_size(
                        ticker, price_cents=price or 0, side=side,
                    )
                    if max_liq_size is not None and count > max_liq_size:
                        logger.info(
                            f"Liquidity cap: reducing {ticker} order from {count} to {max_liq_size} contracts"
                        )
                        count = max_liq_size
                        order_kwargs["count"] = count
                        order_cost = (price * count) if price else 0
            except ValueError:
                raise
            except Exception as e:
                logger.warning(f"Risk gate check failed (allowing order): {e}")

        # 1. Per-trade limit
        if order_cost > self.max_cost_per_trade:
            raise ValueError(
                f"Order cost ({order_cost}c / ${order_cost/100:.2f}) exceeds "
                f"max_cost_per_trade ({self.max_cost_per_trade}c / ${self.max_cost_per_trade/100:.2f})"
            )

        # 2. Balance check — ensure account has enough funds
        if action == "buy" and order_cost > 0:
            try:
                balance = self.get_balance().get("balance", 0)
                if order_cost > balance:
                    raise ValueError(
                        f"Insufficient balance: order costs {order_cost}c / ${order_cost/100:.2f} "
                        f"but account only has {balance}c / ${balance/100:.2f}"
                    )
            except ValueError:
                raise
            except Exception as e:
                logger.warning(f"Could not verify balance before order: {e}")

        # 3. Total exposure check — cap total open risk
        if action == "buy":
            try:
                positions = self.get_positions()
                total_exposure = sum(
                    abs(p.get("total_cost", 0) or 0) for p in positions
                )
                if (total_exposure + order_cost) > self.max_total_exposure:
                    raise ValueError(
                        f"Total exposure ({total_exposure + order_cost}c / "
                        f"${(total_exposure + order_cost)/100:.2f}) would exceed "
                        f"max_total_exposure ({self.max_total_exposure}c / "
                        f"${self.max_total_exposure/100:.2f})"
                    )
            except ValueError:
                raise
            except Exception as e:
                logger.warning(f"Could not verify exposure before order: {e}")

        logger.info(f"Placing Kalshi order: {action} {count}x {side} @ {price}c on {ticker}")

        self._rate_limit()
        try:
            resp = self._portfolio.create_order(**order_kwargs)
        except Exception as e:
            self._order_failure_count += 1
            self._order_failures.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker,
                "side": side,
                "action": action,
                "count": count,
                "price": price,
                "bot": client_order_id.split("-")[0] if client_order_id else "unknown",
                "error": str(e)[:300],
            })
            raise

        result = resp.to_dict() if hasattr(resp, "to_dict") else resp
        self._order_success_count += 1
        logger.info(f"Kalshi order placed: {result}")

        # Record against category budget
        if action == "buy" and order_cost > 0:
            try:
                from app.services.kalshi_risk_manager import get_risk_manager
                get_risk_manager().record_order(ticker, order_cost)
            except Exception:
                pass

        return result

    def get_order_health(self, limit: int = 50) -> dict:
        """Return order success/failure stats and recent failures."""
        return {
            "success_count": self._order_success_count,
            "failure_count": self._order_failure_count,
            "recent_failures": list(self._order_failures)[-limit:],
        }

    def buy_yes(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        """Buy YES contracts at a given price (cents)."""
        return self.place_order(ticker, side="yes", action="buy", yes_price=price, count=count)

    def buy_no(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        """Buy NO contracts at a given price (cents)."""
        return self.place_order(ticker, side="no", action="buy", no_price=price, count=count)

    def sell_yes(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        """Sell YES contracts at a given price (cents)."""
        return self.place_order(ticker, side="yes", action="sell", yes_price=price, count=count)

    def sell_no(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        """Sell NO contracts at a given price (cents)."""
        return self.place_order(ticker, side="no", action="sell", no_price=price, count=count)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        self._ensure_client()
        self._rate_limit()
        resp = self._portfolio.cancel_order(order_id=order_id)
        result = resp.to_dict() if hasattr(resp, "to_dict") else resp
        logger.info(f"Kalshi order cancelled: {order_id}")
        return result

    # =========================================================================
    # POSITIONS
    # =========================================================================

    def get_positions(self, ticker: Optional[str] = None) -> list:
        """Get current positions, optionally filtered by ticker."""
        self._ensure_client()
        self._rate_limit()
        kwargs = {}
        if ticker:
            kwargs["ticker"] = ticker
        resp = self._portfolio.get_positions(**kwargs)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("market_positions", [])

    def get_open_orders(self) -> list:
        """Get all open/resting orders."""
        self._ensure_client()
        self._rate_limit()
        resp = self._portfolio.get_orders(status="resting")
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("orders", [])

    def get_fills(self, limit: int = 50) -> list:
        """Get recent fills (executed trades)."""
        self._ensure_client()
        self._rate_limit()
        resp = self._portfolio.get_fills(limit=limit)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("fills", [])

    def get_settlements(self, limit: int = 50) -> list:
        """Get settled positions and payouts."""
        self._ensure_client()
        self._rate_limit()
        resp = self._portfolio.get_settlements(limit=limit)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("settlements", [])

    # =========================================================================
    # PORTFOLIO SUMMARY
    # =========================================================================

    def get_portfolio_summary(self) -> dict:
        """Get a complete portfolio overview: balance, positions, P&L."""
        self._ensure_client()

        balance = self.get_balance()
        positions = self.get_positions()

        # Calculate unrealized P&L from current positions
        total_invested = 0
        total_market_value = 0
        position_details = []

        for pos in positions:
            ticker = pos.get("ticker", "")
            count = pos.get("position", 0)
            if count == 0:
                continue

            try:
                market = self.get_market(ticker)
                yes_price = market.get("yes_ask", 0) or market.get("last_price", 50)
                no_price = 100 - yes_price

                side = "yes" if count > 0 else "no"
                abs_count = abs(count)
                avg_cost = pos.get("market_exposure", 0) / abs_count if abs_count > 0 else 0
                current_value = (yes_price if side == "yes" else no_price) * abs_count
                invested = abs(pos.get("market_exposure", 0))
                unrealized_pnl = current_value - invested

                position_details.append({
                    "ticker": ticker,
                    "title": market.get("title", ticker),
                    "side": side,
                    "count": abs_count,
                    "avg_cost_cents": round(avg_cost),
                    "current_price_cents": yes_price if side == "yes" else no_price,
                    "invested_cents": invested,
                    "market_value_cents": current_value,
                    "unrealized_pnl_cents": unrealized_pnl,
                    "close_date": market.get("close_time", ""),
                    "status": market.get("status", ""),
                })

                total_invested += invested
                total_market_value += current_value
            except Exception as e:
                logger.warning(f"Error fetching market {ticker}: {e}")
                position_details.append({
                    "ticker": ticker,
                    "side": "yes" if count > 0 else "no",
                    "count": abs(count),
                    "error": str(e),
                })

        return {
            "balance": balance,
            "positions": position_details,
            "total_positions": len(position_details),
            "total_invested_cents": total_invested,
            "total_market_value_cents": total_market_value,
            "unrealized_pnl_cents": total_market_value - total_invested,
            "mode": self.mode,
        }

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def close(self):
        """Clean up API client."""
        if self._client and hasattr(self._client, "close"):
            self._client.close()
        self._client = None
        self._portfolio = None
        self._markets = None
        self._events = None


# Singleton
_kalshi_client: Optional[KalshiTradingClient] = None


def get_kalshi_client() -> KalshiTradingClient:
    global _kalshi_client
    if _kalshi_client is None:
        _kalshi_client = KalshiTradingClient()
    return _kalshi_client


# ─── Async Wrapper ────────────────────────────────────────────────────────────

class AsyncKalshiClient:
    """Async facade over KalshiTradingClient.

    Every method delegates to the underlying sync client via
    ``asyncio.to_thread`` so callers never block the event loop.
    Shares the same singleton (and therefore the same rate limiter).
    """

    def __init__(self, sync_client: KalshiTradingClient):
        self._sync = sync_client

    # Expose config attributes directly
    @property
    def enabled(self):
        return self._sync.enabled

    @property
    def mode(self):
        return self._sync.mode

    @property
    def api_key_id(self):
        return self._sync.api_key_id

    @property
    def private_key_path(self):
        return self._sync.private_key_path

    @property
    def max_cost_per_trade(self):
        return self._sync.max_cost_per_trade

    @property
    def max_total_exposure(self):
        return self._sync.max_total_exposure

    @property
    def default_count(self):
        return self._sync.default_count

    # --- Account ---
    async def get_balance(self) -> dict:
        return await asyncio.to_thread(self._sync.get_balance)

    # --- Markets / Events ---
    async def get_events(self, status: str = "open", limit: int = 20) -> list:
        return await asyncio.to_thread(self._sync.get_events, status, limit)

    async def get_event(self, event_ticker: str) -> dict:
        return await asyncio.to_thread(self._sync.get_event, event_ticker)

    async def get_markets(self, event_ticker: Optional[str] = None,
                          status: str = "open", limit: int = 50) -> list:
        return await asyncio.to_thread(self._sync.get_markets, event_ticker, status, limit)

    async def get_market(self, ticker: str) -> dict:
        return await asyncio.to_thread(self._sync.get_market, ticker)

    async def get_orderbook(self, ticker: str) -> dict:
        return await asyncio.to_thread(self._sync.get_orderbook, ticker)

    async def get_market_trades(self, ticker: str, limit: int = 20) -> list:
        return await asyncio.to_thread(self._sync.get_market_trades, ticker, limit)

    async def get_recent_trades(self, limit: int = 100, ticker: Optional[str] = None) -> list:
        return await asyncio.to_thread(self._sync.get_recent_trades, limit, ticker)

    async def get_markets_full(self, status: str = "open", limit: int = 200,
                               event_ticker: Optional[str] = None,
                               series_ticker: Optional[str] = None) -> list:
        return await asyncio.to_thread(
            self._sync.get_markets_full, status, limit, event_ticker, series_ticker
        )

    async def get_market_full(self, ticker: str) -> dict:
        return await asyncio.to_thread(self._sync.get_market_full, ticker)

    async def discover_active_markets(self, min_volume: int = 10,
                                       limit_per_series: int = 50,
                                       max_days_to_close: int = 0) -> list:
        return await asyncio.to_thread(
            self._sync.discover_active_markets, min_volume, limit_per_series,
            max_days_to_close
        )

    async def get_candlesticks(self, ticker: str, period_interval: int = 60,
                               limit: int = 100) -> list:
        return await asyncio.to_thread(self._sync.get_candlesticks, ticker, period_interval, limit)

    async def search_markets(self, query: str, limit: int = 20) -> list:
        return await asyncio.to_thread(self._sync.search_markets, query, limit)

    # --- Orders ---
    async def place_order(self, ticker: str, side: str, action: str = "buy",
                          count: Optional[int] = None, yes_price: Optional[int] = None,
                          no_price: Optional[int] = None, order_type: str = "limit",
                          client_order_id: Optional[str] = None,
                          expiration_ts: Optional[int] = None) -> dict:
        return await asyncio.to_thread(
            self._sync.place_order, ticker, side, action, count,
            yes_price, no_price, order_type, client_order_id, expiration_ts,
        )

    def get_order_health(self, limit: int = 50) -> dict:
        return self._sync.get_order_health(limit)

    async def buy_yes(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        return await asyncio.to_thread(self._sync.buy_yes, ticker, price, count)

    async def buy_no(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        return await asyncio.to_thread(self._sync.buy_no, ticker, price, count)

    async def sell_yes(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        return await asyncio.to_thread(self._sync.sell_yes, ticker, price, count)

    async def sell_no(self, ticker: str, price: int, count: Optional[int] = None) -> dict:
        return await asyncio.to_thread(self._sync.sell_no, ticker, price, count)

    async def cancel_order(self, order_id: str) -> dict:
        return await asyncio.to_thread(self._sync.cancel_order, order_id)

    # --- Positions ---
    async def get_positions(self, ticker: Optional[str] = None) -> list:
        return await asyncio.to_thread(self._sync.get_positions, ticker)

    async def get_open_orders(self) -> list:
        return await asyncio.to_thread(self._sync.get_open_orders)

    async def get_fills(self, limit: int = 50) -> list:
        return await asyncio.to_thread(self._sync.get_fills, limit)

    async def get_settlements(self, limit: int = 50) -> list:
        return await asyncio.to_thread(self._sync.get_settlements, limit)

    async def get_portfolio_summary(self) -> dict:
        return await asyncio.to_thread(self._sync.get_portfolio_summary)

    def close(self):
        self._sync.close()


# Async singleton
_async_kalshi_client: Optional[AsyncKalshiClient] = None


def get_async_kalshi_client() -> AsyncKalshiClient:
    global _async_kalshi_client
    if _async_kalshi_client is None:
        _async_kalshi_client = AsyncKalshiClient(get_kalshi_client())
    return _async_kalshi_client
