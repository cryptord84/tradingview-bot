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
from datetime import datetime, timedelta
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
        self.stale_order_hours = tech_cfg.get("stale_order_hours", 4)  # Cancel resting orders older than N hours
        self.min_yes_price = tech_cfg.get("min_yes_price", 15)  # Skip if YES < 15¢ (NO would be 85¢+ near certainty)
        self.max_yes_price = tech_cfg.get("max_yes_price", 85)  # Skip if YES > 85¢
        self.telegram_alerts = tech_cfg.get("telegram_alerts", True)
        self.target_tickers = tech_cfg.get("target_tickers", [])
        self.max_markets_to_scan = tech_cfg.get("max_markets_to_scan", 20)
        self.cfg = tech_cfg  # Store for runtime config access

        # Exit management config (inspired by Polymarket convergence strategy)
        self.take_profit_cents = tech_cfg.get("take_profit_cents", 8)  # Exit at +8c profit per contract
        self.stop_loss_cents = tech_cfg.get("stop_loss_cents", 12)  # Exit at -12c loss per contract
        self.trailing_stop_cents = tech_cfg.get("trailing_stop_cents", 5)  # Trail 5c from peak
        self.time_exit_hours = tech_cfg.get("time_exit_hours", 48)  # Close after 48 hours
        self.partial_exit_at_cents = tech_cfg.get("partial_exit_at_cents", 5)  # Partial exit at +5c
        self.anti_chase_pct = tech_cfg.get("anti_chase_pct", 0.10)  # Skip if >10% move in last 3 candles
        self.exit_check_interval = tech_cfg.get("exit_check_interval_seconds", 120)  # Check exits every 2 min

        # Per-ticker position limit: prevent accumulating in one market
        self.max_contracts_per_ticker = tech_cfg.get("max_contracts_per_ticker", 10)

        # Runtime state
        self._signals: list[TechSignal] = []
        self._positions: list[dict] = []  # Active positions from auto-trade
        self._pending_orders: set = set()  # Track resting orders to avoid duplicates
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._exit_task: Optional[asyncio.Task] = None
        self._scan_count = 0
        self._last_scan: Optional[str] = None
        self._total_trades = 0
        self._total_pnl_cents = 0
        self._exits_triggered = 0

    # ── Lifecycle ──

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._reload_positions_from_db()  # Restore state across restarts
        self._task = asyncio.create_task(self._scan_loop())
        self._exit_task = asyncio.create_task(self._exit_loop())
        asyncio.create_task(self._startup_cleanup())
        logger.info(
            f"Technical bot started: MACD({self.macd_fast}/{self.macd_slow}/{self.macd_signal}) "
            f"CCI({self.cci_period}), interval={self.candle_interval}min, "
            f"auto_trade={self.auto_trade}, "
            f"TP={self.take_profit_cents}c SL={self.stop_loss_cents}c trail={self.trailing_stop_cents}c"
        )
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._exit_task and not self._exit_task.done():
            self._exit_task.cancel()
        logger.info("Technical bot stopped")

    def _reload_positions_from_db(self):
        """Reload open positions from DB so exits work across restarts."""
        from app.database import get_db
        try:
            conn = get_db()
            # Find executed buy trades that have no corresponding sell
            rows = conn.execute("""
                SELECT t.ticker, t.side, SUM(t.count) as total_count,
                       AVG(t.price_cents) as avg_price, MIN(t.timestamp) as first_buy,
                       t.title
                FROM kalshi_trades t
                WHERE t.status = 'executed' AND t.action = 'buy'
                AND NOT EXISTS (
                    SELECT 1 FROM kalshi_trades s
                    WHERE s.ticker = t.ticker AND s.side = t.side
                    AND s.action = 'sell' AND s.status = 'executed'
                )
                GROUP BY t.ticker, t.side
            """).fetchall()
            conn.close()

            self._positions.clear()
            self._pending_orders.clear()
            for row in rows:
                ticker, side, count, avg_price, first_buy, title = row
                self._positions.append({
                    "ticker": ticker,
                    "side": side,
                    "count": int(count),
                    "entry_price": int(round(avg_price)),
                    "entry_time": first_buy,
                    "title": title or ticker,
                })
                self._pending_orders.add(f"{ticker}_{side}")

            if self._positions:
                logger.info(
                    f"Reloaded {len(self._positions)} positions from DB: "
                    + ", ".join(f"{p['ticker']}({p['side']} {p['count']}x@{p['entry_price']}c)" for p in self._positions)
                )
        except Exception as e:
            logger.warning(f"Failed to reload positions from DB: {e}")

    async def _startup_cleanup(self):
        """Cancel any stale resting orders from previous runs on startup."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()
        if not client.enabled:
            return
        await asyncio.sleep(5)  # Let client initialize
        await self._cancel_stale_orders(client)

    async def _cancel_stale_orders(self, client):
        """Cancel resting orders in DB that are older than stale_order_hours."""
        import sqlite3
        from app.database import get_db
        cutoff = (datetime.utcnow() - timedelta(hours=self.stale_order_hours)).isoformat()
        conn = get_db()
        rows = conn.execute(
            "SELECT id, order_id, ticker, side FROM kalshi_trades "
            "WHERE status='resting' AND timestamp < ? AND order_id != ''",
            (cutoff,),
        ).fetchall()
        conn.close()

        if not rows:
            return

        logger.info(f"Tech bot cleanup: cancelling {len(rows)} stale resting orders")
        cancelled = 0
        for row in rows:
            row_id, order_id, ticker, side = row[0], row[1], row[2], row[3]
            try:
                await client.cancel_order(order_id)
                conn = get_db()
                conn.execute(
                    "UPDATE kalshi_trades SET status='cancelled' WHERE id=?", (row_id,)
                )
                conn.commit()
                conn.close()
                cancelled += 1
                await asyncio.sleep(0.2)  # Rate limit
            except Exception as e:
                err = str(e).lower()
                if "not found" in err or "404" in err or "already" in err:
                    # Order already gone from Kalshi — update DB to match
                    conn = get_db()
                    conn.execute(
                        "UPDATE kalshi_trades SET status='cancelled' WHERE id=?", (row_id,)
                    )
                    conn.commit()
                    conn.close()
                    cancelled += 1
                else:
                    logger.warning(f"Failed to cancel {order_id} ({ticker} {side}): {e}")

        logger.info(f"Tech bot cleanup: cancelled {cancelled}/{len(rows)} stale orders")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.scan_all()
            except Exception as e:
                logger.error(f"Tech bot scan error: {e}")
            # Periodically clean up stale orders (every 10 scans)
            if self._scan_count % 10 == 0 and self._scan_count > 0:
                try:
                    from app.services.kalshi_client import get_async_kalshi_client
                    client = get_async_kalshi_client()
                    await self._cancel_stale_orders(client)
                except Exception as e:
                    logger.warning(f"Periodic stale cleanup failed: {e}")
            await asyncio.sleep(self.scan_interval)

    # ── Exit Management Loop ──

    async def _exit_loop(self):
        """Separate loop to check positions for exit conditions."""
        while self._running:
            try:
                if self._positions and self.auto_trade:
                    await self._manage_exits()
            except Exception as e:
                logger.error(f"Exit loop error: {e}")
            await asyncio.sleep(self.exit_check_interval)

    async def _manage_exits(self):
        """Check all open positions for exit conditions.

        Exit logic (from Polymarket convergence strategy):
        1. Take profit: current price moved +N cents from entry
        2. Stop loss: current price moved -N cents from entry
        3. Trailing stop: price retreated N cents from peak since entry
        4. Time exit: position held longer than max hours
        5. Partial exit: sell half at first profit target, let rest ride
        """
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()
        if not client.enabled:
            return

        now = datetime.utcnow()
        to_remove = []

        for i, pos in enumerate(self._positions):
            ticker = pos["ticker"]
            try:
                market = await client.get_market(ticker)
            except Exception:
                continue

            yes_price = int(round(float(market.get("yes_ask_dollars", "0") or "0") * 100))
            no_price = 100 - yes_price
            entry_price = pos["entry_price"]
            side = pos["side"]

            # Current value of our position
            current_price = yes_price if side == "yes" else no_price

            # Track peak price for trailing stop
            if "peak_price" not in pos:
                pos["peak_price"] = current_price
            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price

            pnl_cents = current_price - entry_price
            peak_pullback = pos["peak_price"] - current_price
            hours_held = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 3600

            exit_reason = None
            exit_count = pos["count"]

            # 1. Partial exit: sell half at first profit target
            if (pnl_cents >= self.partial_exit_at_cents
                    and not pos.get("partial_exited")
                    and pos["count"] > 1):
                exit_count = pos["count"] // 2
                pos["count"] -= exit_count
                pos["partial_exited"] = True
                exit_reason = f"PARTIAL_TP (+{pnl_cents}c, selling {exit_count}/{pos['count'] + exit_count})"
                # Don't remove from positions — remainder stays

            # 2. Take profit (full remaining position)
            elif pnl_cents >= self.take_profit_cents:
                exit_reason = f"TAKE_PROFIT (+{pnl_cents}c)"

            # 3. Stop loss
            elif pnl_cents <= -self.stop_loss_cents:
                exit_reason = f"STOP_LOSS ({pnl_cents}c)"

            # 4. Trailing stop: only if we've been profitable at some point
            elif (pos["peak_price"] > entry_price + 2
                    and peak_pullback >= self.trailing_stop_cents):
                exit_reason = f"TRAILING_STOP (peak={pos['peak_price']}c, pullback={peak_pullback}c)"

            # 5. Time exit
            elif hours_held >= self.time_exit_hours:
                exit_reason = f"TIME_EXIT ({hours_held:.1f}h)"

            if exit_reason:
                await self._exit_position(client, pos, exit_count, exit_reason)
                # Mark for removal unless partial exit
                if not exit_reason.startswith("PARTIAL"):
                    to_remove.append(i)

        # Remove fully closed positions (reverse order to preserve indices)
        for i in sorted(to_remove, reverse=True):
            if i < len(self._positions):
                self._positions.pop(i)

    async def _exit_position(self, client, pos: dict, count: int, reason: str):
        """Execute a position exit (sell)."""
        from app.database import insert_kalshi_trade

        ticker = pos["ticker"]
        side = pos["side"]
        entry_price = pos["entry_price"]

        try:
            # Get current market price and sell slightly below bid for quick fill
            try:
                market = await client.get_market(ticker)
                yes_bid = int(round(float(market.get("yes_bid_dollars", "0") or "0") * 100))
                no_bid = int(round(float(market.get("no_bid_dollars", "0") or "0") * 100))
                if side == "yes":
                    sell_price = max(yes_bid - 2, 1)  # 2¢ below bid for quick fill
                else:
                    sell_price = max(no_bid - 2, 1)
            except Exception:
                sell_price = 1  # Fallback to aggressive if market lookup fails

            if side == "yes":
                result = await client.sell_yes(ticker, price=sell_price, count=count)
            else:
                result = await client.sell_no(ticker, price=sell_price, count=count)

            order = result.get("order", {})
            fill_price = order.get("avg_price", 0)
            pnl = (fill_price - entry_price) * count if fill_price else 0
            self._total_pnl_cents += pnl
            self._exits_triggered += 1

            insert_kalshi_trade({
                "order_id": order.get("order_id", ""),
                "ticker": ticker,
                "title": pos.get("title", ticker),
                "side": side,
                "action": "sell",
                "count": count,
                "price_cents": fill_price or 1,
                "total_cost_cents": (fill_price or 1) * count,
                "status": order.get("status", "placed"),
                "notes": f"Tech bot EXIT: {reason} | entry={entry_price}c",
            })

            logger.info(
                f"Tech bot EXIT: {reason} | {side.upper()} {count}x {ticker} "
                f"(entry={entry_price}c, pnl={pnl:+}c)"
            )

            # Telegram alert for exits
            if self.telegram_alerts:
                try:
                    from app.services.telegram_service import TelegramService
                    tg = TelegramService()
                    icon = "🟢" if pnl > 0 else "🔴"
                    await tg.send_message(
                        f"{icon} <b>Tech Bot Exit:</b> {reason}\n"
                        f"   {side.upper()} {count}x {ticker}\n"
                        f"   Entry: {entry_price}c | PnL: {pnl:+}c"
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Exit failed for {ticker}: {e}")

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

        # Auto-select: discover markets across active series (bypasses MVE flood)
        max_days = self.cfg.get("max_days_to_close", 0)
        all_markets = await client.discover_active_markets(min_volume=10, max_days_to_close=max_days)
        candidates = []
        for m in all_markets:
            vol = int(float(m.get("volume_fp", "0") or "0"))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            if vol < 10 or yes_ask < self.min_yes_price or yes_ask > self.max_yes_price:
                continue
            # Skip dead zone — near-zero edge at 48-52¢
            if 48 <= yes_ask <= 52:
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

        # Subscribe to WS feed for liquidity-based sizing
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            for m in markets:
                await ws.subscribe(m.get("ticker", ""))
        except Exception:
            pass

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

        # Anti-chase filter: skip if price moved >N% in last 3 candles
        # (from Polymarket convergence strategy — don't buy the top)
        if len(closes) >= 4:
            recent_move = abs(closes[-1] - closes[-4]) / max(closes[-4], 1)
            if recent_move > self.anti_chase_pct:
                logger.debug(f"Anti-chase: {ticker} moved {recent_move:.1%} in 3 candles, skipping")
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
        if curr_hist > 0 and curr_cci > 25 and prev_hist <= 0:
            return TechSignal(
                ticker=ticker, title=title, side="yes", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=0.5,
            )

        # Moderate bearish: MACD negative + CCI falling below 0
        if curr_hist < 0 and curr_cci < -25 and prev_hist >= 0:
            return TechSignal(
                ticker=ticker, title=title, side="no", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=0.5,
            )

        # Trending bullish: MACD histogram positive and increasing for 3+ bars
        if (len(histogram) >= 3 and curr_hist > 0
                and histogram[-2] > 0 and histogram[-3] > 0
                and curr_hist > histogram[-2] > histogram[-3]
                and curr_cci > 0):
            confidence = min(0.6, 0.35 + abs(curr_hist) * 5)
            return TechSignal(
                ticker=ticker, title=title, side="yes", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=confidence,
            )

        # Trending bearish: MACD histogram negative and decreasing for 3+ bars
        if (len(histogram) >= 3 and curr_hist < 0
                and histogram[-2] < 0 and histogram[-3] < 0
                and curr_hist < histogram[-2] < histogram[-3]
                and curr_cci < 0):
            confidence = min(0.6, 0.35 + abs(curr_hist) * 5)
            return TechSignal(
                ticker=ticker, title=title, side="no", strength="moderate",
                macd_hist=curr_hist, cci_val=curr_cci, price=curr_price,
                confidence=confidence,
            )

        return None

    # ── Execution ──

    async def _execute_signals(self, signals: list[TechSignal], client):
        """Auto-trade strong and moderate signals."""
        from app.database import insert_kalshi_trade

        # Trade strong + moderate signals (strong first)
        tradeable = [s for s in signals if s.strength in ("strong", "moderate")]
        tradeable.sort(key=lambda s: s.confidence, reverse=True)
        if not tradeable:
            return

        if len(self._positions) >= self.max_positions:
            logger.info(f"Max positions ({self.max_positions}) reached, skipping execution")
            return

        for sig in tradeable[:2]:  # Max 2 trades per scan
            # Skip if we already have a resting order on this ticker+side (in-memory guard)
            existing_key = f"{sig.ticker}_{sig.side}"
            if existing_key in self._pending_orders:
                logger.debug(f"Skipping duplicate order for {existing_key} (in-memory)")
                continue

            # DB deduplication: skip if we already have an open position on this ticker+side
            # Check for both resting AND executed buy orders without a corresponding sell
            try:
                from app.database import get_db
                conn = get_db()
                # Count executed buys minus executed sells = net open contracts
                buy_count = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM kalshi_trades "
                    "WHERE ticker=? AND side=? AND action='buy' AND status='executed'",
                    (sig.ticker, sig.side),
                ).fetchone()[0]
                sell_count = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM kalshi_trades "
                    "WHERE ticker=? AND side=? AND action='sell' AND status='executed'",
                    (sig.ticker, sig.side),
                ).fetchone()[0]
                resting_count = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM kalshi_trades "
                    "WHERE ticker=? AND side=? AND status='resting'",
                    (sig.ticker, sig.side),
                ).fetchone()[0]
                conn.close()
                net_open = buy_count - sell_count + resting_count
                if net_open >= self.max_contracts_per_ticker:
                    logger.debug(
                        f"Skipping {sig.ticker} {sig.side}: already {net_open} contracts "
                        f"(max {self.max_contracts_per_ticker})"
                    )
                    self._pending_orders.add(existing_key)
                    continue
            except Exception as e:
                logger.warning(f"DB dedup check failed: {e}")

            # Price range filter: skip near-certainty markets (poor risk/reward)
            # For YES orders: only trade if YES is between min_yes_price and max_yes_price
            # For NO orders: the NO price = 100 - YES price, so same range applies
            if sig.price < self.min_yes_price or sig.price > self.max_yes_price:
                logger.info(
                    f"Skipping {sig.ticker} {sig.side}: YES={sig.price}¢ outside "
                    f"[{self.min_yes_price},{self.max_yes_price}]¢ range"
                )
                continue

            # Compute limit price with aggression premium for fill rate.
            # Strong signals: +3¢, moderate: +1¢
            # CRITICAL: For NO orders, sig.price is the YES candle close.
            # NO contracts trade at (100 - YES_price), so invert before adding aggression.
            aggression = 3 if sig.strength == "strong" else 1
            if sig.side == "yes":
                price = min(sig.price + aggression, 99)
            else:
                # NO price = complement of YES price; pay slightly above NO fair value
                price = min((100 - sig.price) + aggression, 99)
            count = self.contracts_per_trade

            # Progressive drawdown sizing: scale down as daily losses mount
            # -25% of daily limit → 75% size, -50% → 50% size, -75% → 25% size
            try:
                from app.services.kalshi_risk_manager import get_risk_manager
                rm = get_risk_manager()
                current_pnl = rm._get_aggregate_pnl()
                max_loss = rm.max_daily_loss_cents
                if max_loss > 0 and current_pnl < 0:
                    loss_ratio = abs(current_pnl) / max_loss
                    scale = max(0.25, 1.0 - loss_ratio)
                    scaled_count = max(1, int(count * scale))
                    if scaled_count < count:
                        logger.info(f"Drawdown sizing: {count}→{scaled_count} contracts (daily PnL: {current_pnl}c)")
                        count = scaled_count
            except Exception:
                pass

            cost = price * count
            if cost > self.max_cost_per_trade_cents:
                logger.info(f"Trade cost ${cost/100:.2f} exceeds max, skipping")
                continue

            # Risk auditor gate
            try:
                from app.services.kalshi_risk_manager import get_risk_manager
                rm = get_risk_manager()
                if rm.enabled:
                    audit = rm.audit_trade(
                        ticker=sig.ticker, side=sig.side, price_cents=price,
                        count=count, confidence=sig.confidence,
                        bot_name="tech", title=sig.title,
                    )
                    if not audit["approved"]:
                        logger.info(f"Tech trade BLOCKED by auditor: {audit['reason']}")
                        continue
                    if audit.get("adjustments", {}).get("count"):
                        count = audit["adjustments"]["count"]
            except Exception as e:
                logger.warning(f"Risk audit failed (allowing trade): {e}")

            try:
                if sig.side == "yes":
                    result = await client.buy_yes(sig.ticker, price, count)
                else:
                    result = await client.buy_no(sig.ticker, price, count)

                order = result.get("order", {})
                self._total_trades += 1

                # Log to DB
                insert_kalshi_trade({
                    "order_id": order.get("order_id", ""),
                    "ticker": sig.ticker,
                    "title": sig.title,
                    "side": sig.side,
                    "action": "buy",
                    "count": count,
                    "price_cents": price,
                    "total_cost_cents": price * count,
                    "status": order.get("status", "placed"),
                    "notes": f"Tech bot: {sig.strength} {sig.side} | MACD={sig.macd_hist:.4f} CCI={sig.cci_val:.1f}",
                })

                # Track position and pending order
                self._positions.append({
                    "ticker": sig.ticker,
                    "side": sig.side,
                    "count": count,
                    "entry_price": price,
                    "entry_time": datetime.utcnow().isoformat(),
                })
                self._pending_orders.add(existing_key)

                logger.info(
                    f"Tech bot TRADE: {sig.side.upper()} {count}x "
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
            "exits_triggered": self._exits_triggered,
            "total_pnl_cents": self._total_pnl_cents,
            "active_positions": len(self._positions),
            "max_positions": self.max_positions,
            "config": {
                "macd": f"{self.macd_fast}/{self.macd_slow}/{self.macd_signal}",
                "cci_period": self.cci_period,
                "candle_interval_min": self.candle_interval,
                "contracts": self.contracts_per_trade,
                "take_profit_cents": self.take_profit_cents,
                "stop_loss_cents": self.stop_loss_cents,
                "trailing_stop_cents": self.trailing_stop_cents,
                "time_exit_hours": self.time_exit_hours,
            },
        }


# Singleton
_tech_bot: Optional[KalshiTechnicalBot] = None


def get_technical_bot() -> KalshiTechnicalBot:
    global _tech_bot
    if _tech_bot is None:
        _tech_bot = KalshiTechnicalBot()
    return _tech_bot
