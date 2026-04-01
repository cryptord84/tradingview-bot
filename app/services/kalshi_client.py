"""Kalshi API client for binary event contract trading.

Uses the official kalshi-python SDK (v2.1.4) with RSA key authentication.
Supports both demo (sandbox) and production environments.
"""

import logging
from typing import Optional

from kalshi_python import (
    ApiClient,
    Configuration,
    CreateOrderRequest,
    EventsApi,
    KalshiClient,
    MarketsApi,
    PortfolioApi,
)

from app.config import get

logger = logging.getLogger("bot.kalshi")

# API base URLs
KALSHI_HOSTS = {
    "demo": "https://api.demo.kalshi.co/trade-api/v2",
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
        self.max_cost_per_trade = cfg.get("max_cost_per_trade_cents", 500)  # $5 default
        self.max_open_positions = cfg.get("max_open_positions", 10)
        self.default_count = cfg.get("default_contract_count", 10)

        self._client: Optional[ApiClient] = None
        self._portfolio: Optional[PortfolioApi] = None
        self._markets: Optional[MarketsApi] = None
        self._events: Optional[EventsApi] = None

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

    def get_balance(self) -> dict:
        """Get account balance in cents."""
        self._ensure_client()
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
        resp = self._events.get_events(status=status, limit=limit)
        events = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return events.get("events", [])

    def get_event(self, event_ticker: str) -> dict:
        """Get details for a specific event."""
        self._ensure_client()
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
        kwargs = {"status": status, "limit": limit}
        if event_ticker:
            kwargs["event_ticker"] = event_ticker
        resp = self._markets.get_markets(**kwargs)
        markets = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return markets.get("markets", [])

    def get_market(self, ticker: str) -> dict:
        """Get details for a specific market."""
        self._ensure_client()
        resp = self._markets.get_market(ticker=ticker)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("market", data)

    def get_orderbook(self, ticker: str) -> dict:
        """Get order book for a market."""
        self._ensure_client()
        resp = self._markets.get_market_orderbook(ticker=ticker)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("orderbook", data)

    def get_market_trades(self, ticker: str, limit: int = 20) -> list:
        """Get recent trades for a market."""
        self._ensure_client()
        resp = self._markets.get_trades(ticker=ticker, limit=limit)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("trades", [])

    def get_candlesticks(
        self, ticker: str, period_interval: int = 60, limit: int = 100
    ) -> list:
        """Get candlestick data. period_interval: 1 (1min), 60 (1hr), 1440 (1day)."""
        self._ensure_client()
        resp = self._markets.get_market_candlesticks(
            ticker=ticker, period_interval=period_interval
        )
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("candlesticks", [])

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

        # Safety check: max cost
        price = yes_price if side == "yes" else no_price
        if price and (price * count) > self.max_cost_per_trade:
            raise ValueError(
                f"Order cost ({price * count}c) exceeds max_cost_per_trade "
                f"({self.max_cost_per_trade}c / ${self.max_cost_per_trade/100:.2f})"
            )

        order = CreateOrderRequest(**order_kwargs)
        logger.info(f"Placing Kalshi order: {action} {count}x {side} @ {price}c on {ticker}")

        resp = self._portfolio.create_order(create_order_request=order)
        result = resp.to_dict() if hasattr(resp, "to_dict") else resp
        logger.info(f"Kalshi order placed: {result}")
        return result

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
        resp = self._portfolio.cancel_order(order_id=order_id)
        result = resp.to_dict() if hasattr(resp, "to_dict") else resp
        logger.info(f"Kalshi order cancelled: {order_id}")
        return result

    # =========================================================================
    # POSITIONS
    # =========================================================================

    def get_positions(self, settlement_status: str = "unsettled") -> list:
        """Get current positions. settlement_status: unsettled, settled, all."""
        self._ensure_client()
        resp = self._portfolio.get_positions(settlement_status=settlement_status)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("market_positions", [])

    def get_open_orders(self) -> list:
        """Get all open/resting orders."""
        self._ensure_client()
        resp = self._portfolio.get_orders(status="resting")
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("orders", [])

    def get_fills(self, limit: int = 50) -> list:
        """Get recent fills (executed trades)."""
        self._ensure_client()
        resp = self._portfolio.get_fills(limit=limit)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data.get("fills", [])

    def get_settlements(self, limit: int = 50) -> list:
        """Get settled positions and payouts."""
        self._ensure_client()
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
