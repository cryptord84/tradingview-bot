"""Kalshi Whale Tracker — monitors large trades and alerts on whale activity.

Scans recent fills across active markets, flags trades above a configurable
threshold, and sends Telegram alerts with trade details.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.whale")


class WhaleTrade:
    """A detected whale-sized trade."""

    def __init__(self, ticker: str, title: str, side: str, count: int,
                 price_cents: int, cost_cents: int, trade_id: str, ts: str):
        self.ticker = ticker
        self.title = title
        self.side = side
        self.count = count
        self.price_cents = price_cents
        self.cost_cents = cost_cents
        self.trade_id = trade_id
        self.ts = ts
        self.detected_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "side": self.side,
            "count": self.count,
            "price_cents": self.price_cents,
            "cost_cents": self.cost_cents,
            "cost_usd": round(self.cost_cents / 100, 2),
            "trade_id": self.trade_id,
            "ts": self.ts,
            "detected_at": self.detected_at,
        }


class KalshiWhaleTracker:
    """Tracks large trades on Kalshi markets."""

    def __init__(self):
        cfg = get("kalshi") or {}
        whale_cfg = cfg.get("whale_tracker", {})

        self.enabled = whale_cfg.get("enabled", False)
        self.scan_interval = whale_cfg.get("scan_interval_seconds", 60)
        self.min_count = whale_cfg.get("min_contract_count", 50)
        self.min_cost_cents = whale_cfg.get("min_cost_cents", 2500)  # $25
        self.max_markets_to_scan = whale_cfg.get("max_markets_to_scan", 30)
        self.telegram_alerts = whale_cfg.get("telegram_alerts", True)
        self.history_limit = whale_cfg.get("history_limit", 100)

        self._whales: list[WhaleTrade] = []
        self._seen_trade_ids: set[str] = set()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._scan_count = 0
        self._last_scan: Optional[str] = None

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(f"Whale tracker started (min {self.min_count} contracts / ${self.min_cost_cents/100:.0f})")
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Whale tracker stopped")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.scan()
            except Exception as e:
                logger.error(f"Whale scan error: {e}")
            await asyncio.sleep(self.scan_interval)

    async def scan(self) -> list[dict]:
        """Scan recent trades across all markets for whale activity."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return []

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        new_whales = []

        try:
            # Fetch recent trades across all markets (direct API for full data)
            trades = await client.get_recent_trades(limit=100)

            for t in trades:
                trade_id = t.get("trade_id", "")
                if not trade_id or trade_id in self._seen_trade_ids:
                    continue

                # API returns dollar-denominated strings
                count = int(float(t.get("count_fp", "0") or "0"))
                yes_price_dollars = float(t.get("yes_price_dollars", "0") or "0")
                no_price_dollars = float(t.get("no_price_dollars", "0") or "0")
                side = t.get("taker_side", "yes")
                price_dollars = yes_price_dollars if side == "yes" else no_price_dollars
                price_cents = int(round(price_dollars * 100))
                cost_cents = count * price_cents

                is_whale = count >= self.min_count or cost_cents >= self.min_cost_cents
                if is_whale:
                    ticker = t.get("ticker", "")
                    whale = WhaleTrade(
                        ticker=ticker,
                        title=ticker,
                        side=side,
                        count=count,
                        price_cents=price_cents,
                        cost_cents=cost_cents,
                        trade_id=str(trade_id),
                        ts=str(t.get("created_time", "")),
                    )
                    new_whales.append(whale)
                    self._whales.insert(0, whale)
                    logger.info(
                        f"WHALE: {count}x {side.upper()} @{price_cents}c = ${cost_cents/100:.2f} on {ticker}"
                    )

                self._seen_trade_ids.add(trade_id)

            # Trim history
            if len(self._whales) > self.history_limit:
                self._whales = self._whales[:self.history_limit]
            if len(self._seen_trade_ids) > 5000:
                self._seen_trade_ids = set(list(self._seen_trade_ids)[-2000:])

            # Alert on new whales
            if new_whales and self.telegram_alerts:
                await self._send_alert(new_whales)

        except Exception as e:
            logger.error(f"Whale scan error: {e}")

        return [w.to_dict() for w in new_whales]

    async def _send_alert(self, whales: list[WhaleTrade]):
        from app.services.telegram_service import TelegramService
        tg = TelegramService()

        lines = [f"<b>🐋 Kalshi Whale Alert ({len(whales)} trades)</b>\n"]
        for w in whales[:5]:
            direction = "🟢 YES" if w.side == "yes" else "🔴 NO"
            lines.append(
                f"{direction} <b>{w.count}x @{w.price_cents}¢</b> = ${w.cost_cents/100:.2f}\n"
                f"   {w.title[:50]}\n"
            )
        await tg.send_message("\n".join(lines))

    def get_whales(self, limit: int = 50) -> list[dict]:
        return [w.to_dict() for w in self._whales[:limit]]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan,
            "total_whales_detected": len(self._whales),
            "min_count": self.min_count,
            "min_cost_usd": round(self.min_cost_cents / 100, 2),
        }


_tracker: Optional[KalshiWhaleTracker] = None


def get_whale_tracker() -> KalshiWhaleTracker:
    global _tracker
    if _tracker is None:
        _tracker = KalshiWhaleTracker()
    return _tracker
