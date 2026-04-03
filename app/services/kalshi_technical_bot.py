"""Kalshi MACD/CCI Technical Bot — trades prediction markets using indicator signals.

Applies MACD (12/26/9) and CCI (20) to Kalshi contract price candlesticks.
When both indicators agree on direction, places a trade on the corresponding side.

Signal logic:
- BUY YES: MACD histogram > 0 AND MACD crossing above signal AND CCI > 100
- BUY NO:  MACD histogram < 0 AND MACD crossing below signal AND CCI < -100
- EXIT:    Opposite signal, or CCI returns to neutral zone (-50 to +50)

Uses 1-hour candlesticks by default (5-min available but noisy on Kalshi).
"""

import asyncio
import logging
import math
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.tech")


# ─── Indicator Calculations ──────────────────────────────────────────────────

def ema(data: list[float], period: int) -> list[float]:
    """Exponential Moving Average."""
    if len(data) < period:
        return [0.0] * len(data)
    result = [0.0] * len(data)
    k = 2 / (period + 1)
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = data[i] * k + result[i - 1] * (1 - k)
    return result


def sma(data: list[float], period: int) -> list[float]:
    """Simple Moving Average."""
    result = [0.0] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1:i + 1]) / period
    return result


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Calculate MACD line, signal line, and histogram."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def cci(highs: list[float], lows: list[float], closes: list[float], period: int = 20):
    """Commodity Channel Index."""
    if len(closes) < period:
        return [0.0] * len(closes)

    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    tp_sma = sma(tp, period)

    result = [0.0] * len(closes)
    for i in range(period - 1, len(closes)):
        window = tp[i - period + 1:i + 1]
        mean = tp_sma[i]
        mean_dev = sum(abs(x - mean) for x in window) / period
        if mean_dev > 0:
            result[i] = (tp[i] - mean) / (0.015 * mean_dev)

    return result


# ─── Signal Types ────────────────────────────────────────────────────────────

class TechSignal:
    """A technical indicator signal on a Kalshi market."""

    def __init__(self, ticker: str, title: str, side: str, strength: str,
                 macd_hist: float, cci_val: float, price: int, confidence: float):
        self.ticker = ticker
        self.title = title
        self.side = side  # "yes" or "no"
        self.strength = strength  # "strong", "moderate", "weak"
        self.macd_hist = macd_hist
        self.cci_val = cci_val
        self.price = price  # current price in cents
        self.confidence = confidence  # 0-1
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "side": self.side,
            "strength": self.strength,
            "macd_hist": round(self.macd_hist, 4),
            "cci": round(self.cci_val, 1),
            "price_cents": self.price,
            "confidence": round(self.confidence, 2),
            "timestamp": self.timestamp,
        }


# ─── Main Bot ────────────────────────────────────────────────────────────────

class KalshiTechnicalBot:
    """MACD/CCI twin indicator bot for Kalshi markets."""

    def __init__(self):
        cfg = get("kalshi") or {}
        tech_cfg = cfg.get("technical_bot", {})

        self.enabled = tech_cfg.get("enabled", False)
        self.scan_interval = tech_cfg.get("scan_interval_seconds", 300)
        self.candle_interval = tech_cfg.get("candle_interval_minutes", 60)
        self.candle_count = tech_cfg.get("candle_count", 100)
        self.macd_fast = tech_cfg.get("macd_fast", 12)
        self.macd_slow = tech_cfg.get("macd_slow", 26)
        self.macd_signal = tech_cfg.get("macd_signal", 9)
        self.cci_period = tech_cfg.get("cci_period", 20)
        self.cci_overbought = tech_cfg.get("cci_overbought", 100)
        self.cci_oversold = tech_cfg.get("cci_oversold", -100)
        self.auto_trade = tech_cfg.get("auto_trade", False)
        self.contracts_per_trade = tech_cfg.get("contracts_per_trade", 5)
        self.max_positions = tech_cfg.get("max_positions", 5)
        self.max_cost_per_trade_cents = tech_cfg.get("max_cost_per_trade_cents", 500)
        self.telegram_alerts = tech_cfg.get("telegram_alerts", True)
        self.target_tickers = tech_cfg.get("target_tickers", [])
        self.max_markets_to_scan = tech_cfg.get("max_markets_to_scan", 20)

        # Runtime state
        self._signals: list[TechSignal] = []
        self._positions: list[dict] = []  # Active positions from auto-trade
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._scan_count = 0
        self._last_scan: Optional[str] = None
        self._total_trades = 0
        self._total_pnl_cents = 0

    # ── Lifecycle ──

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(
            f"Technical bot started: MACD({self.macd_fast}/{self.macd_slow}/{self.macd_signal}) "
            f"CCI({self.cci_period}), interval={self.candle_interval}min, "
            f"auto_trade={self.auto_trade}"
        )
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Technical bot stopped")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.scan_all()
            except Exception as e:
                logger.error(f"Tech bot scan error: {e}")
            await asyncio.sleep(self.scan_interval)

    # ── Market Selection ──

    # Finance/economics markets are nearly efficient (0.17pp maker-taker gap)
    # Focus on high-bias categories where technical signals have edge
    LOW_EDGE_KEYWORDS = ["finance", "fed ", "interest rate", "gdp", "cpi",
                         "inflation", "treasury", "earnings"]

    async def _get_target_markets(self, client) -> list[dict]:
        """Get markets to analyze — either configured tickers or auto-selected.

        Research-informed filtering:
        - Skip finance/economics (near-efficient, no technical edge)
        - Skip 40-60¢ range (no directional edge near fair value)
        - Prefer tail prices where behavioral bias creates alpha
        """
        if self.target_tickers:
            markets = []
            for ticker in self.target_tickers:
                try:
                    m = await client.get_market(ticker)
                    markets.append(m)
                except Exception:
                    pass
            return markets

        # Auto-select: high volume, tail prices, high-bias categories
        all_markets = await client.get_markets_full(status="open", limit=100)
        candidates = []
        for m in all_markets:
            vol = int(float(m.get("volume_fp", "0") or "0"))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            if vol < 25 or yes_ask < 5 or yes_ask > 95:
                continue
            # Skip dead zone — near-zero edge at 40-60¢
            if 40 <= yes_ask <= 60:
                continue
            # Skip low-edge finance/economics markets
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            if any(kw in title for kw in self.LOW_EDGE_KEYWORDS):
                continue
            candidates.append(m)

        candidates.sort(key=lambda m: int(float(m.get("volume_fp", "0") or "0")), reverse=True)
        return candidates[:self.max_markets_to_scan]

    # ── Analysis ──

    async def scan_all(self) -> list[dict]:
        """Scan all target markets and generate signals."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()
        if not client.enabled:
            return []

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        new_signals = []

        markets = await self._get_target_markets(client)
        logger.info(f"Tech scan #{self._scan_count}: analyzing {len(markets)} markets")

        for m in markets:
            ticker = m.get("ticker", "")
            title = m.get("title", ticker)

            try:
                candles = await client.get_candlesticks(
                    ticker, period_interval=self.candle_interval, limit=self.candle_count
                )
                if len(candles) < self.macd_slow + self.macd_signal:
                    continue

                signal = self._analyze(ticker, title, candles, m)
                if signal:
                    new_signals.append(signal)
                    self._signals.insert(0, signal)

            except Exception as e:
                logger.debug(f"Skipping {ticker}: {e}")

        # Trim signal history
        if len(self._signals) > 200:
            self._signals = self._signals[:200]

        if new_signals:
            logger.info(f"Tech bot: {len(new_signals)} signals generated")
            if self.telegram_alerts:
                await self._send_alerts(new_signals)
            if self.auto_trade:
                await self._execute_signals(new_signals, client)

        return [s.to_dict() for s in new_signals]

    def _analyze(self, ticker: str, title: str, candles: list, market: dict) -> Optional[TechSignal]:
        """Run MACD + CCI on candlestick data and return signal if conditions met."""
        # Extract OHLC — Kalshi candles: {open, high, low, close, volume, period_end_ts, ...}
        opens = [c.get("open", 0) or c.get("price", 50) for c in candles]
        highs = [c.get("high", 0) or c.get("open", 50) for c in candles]
        lows = [c.get("low", 0) or c.get("open", 50) for c in candles]
        closes = [c.get("close", 0) or c.get("price", 50) for c in candles]

        # Convert to floats
        closes = [float(x) for x in closes]
        highs = [float(x) for x in highs]
        lows = [float(x) for x in lows]

        if not closes or max(closes) == 0:
            return None

        # Calculate indicators
        macd_line, signal_line, histogram = macd(
            closes, self.macd_fast, self.macd_slow, self.macd_signal
        )
        cci_values = cci(highs, lows, closes, self.cci_period)

        # Get current values (last candle)
        curr_hist = histogram[-1]
        prev_hist = histogram[-2] if len(histogram) > 1 else 0
        curr_cci = cci_values[-1]
        curr_macd = macd_line[-1]
        curr_signal = signal_line[-1]
        curr_price = int(closes[-1])

        # ── Signal Logic ──

        # MACD crossover detection
        macd_bull_cross = prev_hist <= 0 and curr_hist > 0  # histogram crosses above 0
        macd_bear_cross = prev_hist >= 0 and curr_hist < 0  # histogram crosses below 0

        # Strong bullish: MACD bull cross + CCI overbought territory
        if macd_bull_cross and curr_cci > self.cci_overbought:
            confidence = min(1.0, 0.6 + abs(curr_cci) / 500 + abs(curr_hist) * 10)
            return TechSignal(
                ticker=ticker, title=title, side="yes", strength="strong",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=confidence,
            )

        # Strong bearish: MACD bear cross + CCI oversold
        if macd_bear_cross and curr_cci < self.cci_oversold:
            confidence = min(1.0, 0.6 + abs(curr_cci) / 500 + abs(curr_hist) * 10)
            return TechSignal(
                ticker=ticker, title=title, side="no", strength="strong",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=confidence,
            )

        # Moderate bullish: MACD positive + CCI rising above 0
        if curr_hist > 0 and curr_cci > 50 and prev_hist <= 0:
            return TechSignal(
                ticker=ticker, title=title, side="yes", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=0.5,
            )

        # Moderate bearish: MACD negative + CCI falling below 0
        if curr_hist < 0 and curr_cci < -50 and prev_hist >= 0:
            return TechSignal(
                ticker=ticker, title=title, side="no", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=0.5,
            )

        return None

    # ── Execution ──

    async def _execute_signals(self, signals: list[TechSignal], client):
        """Auto-trade strong signals."""
        from app.database import insert_kalshi_trade

        # Only trade strong signals
        strong = [s for s in signals if s.strength == "strong"]
        if not strong:
            return

        if len(self._positions) >= self.max_positions:
            logger.info(f"Max positions ({self.max_positions}) reached, skipping execution")
            return

        for sig in strong[:2]:  # Max 2 trades per scan
            price = sig.price
            cost = price * self.contracts_per_trade
            if cost > self.max_cost_per_trade_cents:
                logger.info(f"Trade cost ${cost/100:.2f} exceeds max, skipping")
                continue

            try:
                if sig.side == "yes":
                    result = await client.buy_yes(sig.ticker, price, self.contracts_per_trade)
                else:
                    result = await client.buy_no(sig.ticker, price, self.contracts_per_trade)

                order = result.get("order", {})
                self._total_trades += 1

                # Log to DB
                insert_kalshi_trade({
                    "order_id": order.get("order_id", ""),
                    "ticker": sig.ticker,
                    "title": sig.title,
                    "side": sig.side,
                    "action": "buy",
                    "count": self.contracts_per_trade,
                    "price_cents": price,
                    "total_cost_cents": cost,
                    "status": order.get("status", "placed"),
                    "notes": f"Tech bot: {sig.strength} {sig.side} | MACD={sig.macd_hist:.4f} CCI={sig.cci_val:.1f}",
                })

                # Track position
                self._positions.append({
                    "ticker": sig.ticker,
                    "side": sig.side,
                    "count": self.contracts_per_trade,
                    "entry_price": price,
                    "entry_time": datetime.utcnow().isoformat(),
                })

                logger.info(
                    f"Tech bot TRADE: {sig.side.upper()} {self.contracts_per_trade}x "
                    f"@{price}¢ on {sig.ticker} (MACD={sig.macd_hist:.4f}, CCI={sig.cci_val:.1f})"
                )

            except Exception as e:
                logger.error(f"Tech bot trade failed on {sig.ticker}: {e}")

    # ── Alerts ──

    async def _send_alerts(self, signals: list[TechSignal]):
        from app.services.telegram_service import TelegramService
        tg = TelegramService()

        lines = [f"<b>📊 Kalshi Tech Signal ({len(signals)} markets)</b>\n"]
        for s in signals[:5]:
            icon = "🟢" if s.side == "yes" else "🔴"
            strength_label = {"strong": "⚡", "moderate": "📈", "weak": "📉"}.get(s.strength, "")
            lines.append(
                f"{icon} {strength_label} <b>{s.side.upper()}</b> @{s.price}¢ "
                f"({s.confidence:.0%})\n"
                f"   {s.title[:45]}\n"
                f"   MACD: {s.macd_hist:.4f} | CCI: {s.cci_val:.0f}\n"
            )

        if self.auto_trade:
            lines.append(f"\n<i>Auto-trade: ON | Positions: {len(self._positions)}/{self.max_positions}</i>")
        else:
            lines.append(f"\n<i>Auto-trade: OFF (signals only)</i>")

        await tg.send_message("\n".join(lines))

    # ── Status ──

    def get_signals(self, limit: int = 50) -> list[dict]:
        return [s.to_dict() for s in self._signals[:limit]]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "auto_trade": self.auto_trade,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan,
            "total_signals": len(self._signals),
            "total_trades": self._total_trades,
            "active_positions": len(self._positions),
            "max_positions": self.max_positions,
            "config": {
                "macd": f"{self.macd_fast}/{self.macd_slow}/{self.macd_signal}",
                "cci_period": self.cci_period,
                "candle_interval_min": self.candle_interval,
                "contracts": self.contracts_per_trade,
            },
        }


# Singleton
_tech_bot: Optional[KalshiTechnicalBot] = None


def get_technical_bot() -> KalshiTechnicalBot:
    global _tech_bot
    if _tech_bot is None:
        _tech_bot = KalshiTechnicalBot()
    return _tech_bot
