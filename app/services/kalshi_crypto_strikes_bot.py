"""Scanning bot for Kalshi daily crypto-strike markets.

Pricing model lives in kalshi_crypto_strikes.py (pure functions).
This file is the scan loop, edge gate, Kelly sizer, order router, and
calibration logger. Paper-trade mode (dry_run) logs predicted fair_prob
without placing orders — used for the 48h calibration burn-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get
from app.database import insert_kalshi_trade
from app.services.kalshi_client import AsyncKalshiClient, get_async_kalshi_client
from app.services.kalshi_crypto_strikes import (
    ScoredMarket, ewma_realized_vol, fetch_binance_hourly_closes,
    fetch_binance_spot, score_markets,
)

logger = logging.getLogger("bot.kalshi.strikes")

CALIBRATION_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "kalshi_strikes_calibration.jsonl"
# Shortened 2026-04-24 from 6h → 1h: EWMA on hourly data is the point — want it
# to respond to regime shifts within an hour, not lag a quarter-day behind.
VOL_CACHE_SECONDS = 3600


def _kelly_fraction(fair_prob: float, yes_ask_cents: int) -> float:
    """Binary-market Kelly: f* = (p - q) / (1 - q) where q = price as decimal.

    Returns 0 if no edge (p <= q) or if q >= 1 (degenerate market).
    """
    q = yes_ask_cents / 100.0
    if q <= 0 or q >= 1:
        return 0.0
    if fair_prob <= q:
        return 0.0
    return (fair_prob - q) / (1.0 - q)


class KalshiCryptoStrikesBot:
    """Scans daily crypto-strike markets, sizes by fractional Kelly, trades on edge."""

    def __init__(self):
        cfg = get("kalshi") or {}
        strikes_cfg = cfg.get("crypto_strikes", {})
        self.enabled = strikes_cfg.get("enabled", False)
        self.dry_run = strikes_cfg.get("dry_run", True)
        self.scan_interval = strikes_cfg.get("scan_interval_seconds", 300)
        self.series = strikes_cfg.get("series", ["KXBTCD"])
        self.min_edge_cents = strikes_cfg.get("min_edge_cents", 5)
        # Hotfix 2026-04-24: 48h paper-trade showed systematic OTM YES overprediction
        # in 10-50% fair_prob range (actual hit rate 4-22%, sim ROI -46% to -68%).
        # Gate at 0.50 until vol model (EWMA short-window + skew/recalibration) ships.
        self.min_fair_prob = strikes_cfg.get("min_fair_prob", 0.50)
        self.kelly_fraction = strikes_cfg.get("kelly_fraction", 0.25)
        # Vol model revision 2026-04-24 (option A): switched from 30d daily realized vol
        # to EWMA on 72h of 1h candles. Daily vol was over-smoothed and regime-stale for
        # sub-24h strikes — kept OTM fair_prob inflated in the 10-50% zone.
        self.vol_lookback_hours = strikes_cfg.get("vol_lookback_hours", 72)
        self.vol_ewma_decay = strikes_cfg.get("vol_ewma_decay", 0.97)
        self.vol_floor = strikes_cfg.get("vol_floor", 0.35)
        self.max_cost_per_trade_cents = strikes_cfg.get("max_cost_per_trade_cents", 500)
        self.max_contracts_per_ticker = strikes_cfg.get("max_contracts_per_ticker", 10)
        self.max_open_positions = strikes_cfg.get("max_open_positions", 8)
        self.min_yes_ask_cents = strikes_cfg.get("min_yes_ask_cents", 3)
        self.max_yes_ask_cents = strikes_cfg.get("max_yes_ask_cents", 97)
        self.max_days_to_close = strikes_cfg.get("max_days_to_close", 2)

        self._scan_task: Optional[asyncio.Task] = None
        self._running = False
        self._scan_count = 0
        self._last_scan_iso: Optional[str] = None
        self._trades_placed = 0
        self._dry_run_signals = 0

        self._vol_cache: dict[str, tuple[float, float]] = {}
        # Tickers this bot has opened (live mode only). Kalshi has no bot attribution,
        # so we count bot-owned positions locally rather than against the account-wide total.
        self._bot_held_tickers: set[str] = set()
        CALIBRATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> asyncio.Task:
        if self._scan_task and not self._scan_task.done():
            logger.warning("Crypto strikes bot already running")
            return self._scan_task
        # Pre-warm the Kalshi client singleton to avoid _ensure_client() race with
        # other bots starting simultaneously (sets _client before _portfolio — thread B
        # sees _client as non-None and returns early before _portfolio is assigned).
        try:
            get_async_kalshi_client()._sync._ensure_client()
        except Exception as e:
            logger.warning(f"Kalshi client pre-warm failed (scan loop will retry): {e}")
        self._running = True
        self._scan_task = asyncio.create_task(self._scan_loop())
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            f"Kalshi crypto strikes bot started [{mode}] "
            f"series={self.series} interval={self.scan_interval}s "
            f"min_edge={self.min_edge_cents}c min_fair_prob={self.min_fair_prob:.2f} "
            f"kelly={self.kelly_fraction:.2f}"
        )
        return self._scan_task

    def stop(self):
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        logger.info("Kalshi crypto strikes bot stopped")

    async def _get_annual_vol(self, underlying: str) -> float:
        now = time.monotonic()
        cached = self._vol_cache.get(underlying)
        if cached and (now - cached[0]) < VOL_CACHE_SECONDS:
            return cached[1]
        symbol = {"BTCD": "BTCUSDT", "ETHD": "ETHUSDT", "SOLD": "SOLUSDT"}.get(underlying, "BTCUSDT")
        closes = await fetch_binance_hourly_closes(symbol, self.vol_lookback_hours)
        raw = ewma_realized_vol(closes, decay=self.vol_ewma_decay)
        vol = max(raw, self.vol_floor)
        self._vol_cache[underlying] = (now, vol)
        logger.info(
            f"Vol refresh {underlying}: ewma_hourly={raw:.3f} used={vol:.3f} "
            f"(lookback={self.vol_lookback_hours}h decay={self.vol_ewma_decay})"
        )
        return vol

    def _underlying_from_series(self, series: str) -> str:
        return series.replace("KX", "")

    def _binance_symbol_for(self, series: str) -> str:
        und = self._underlying_from_series(series)
        return {"BTCD": "BTCUSDT", "ETHD": "ETHUSDT", "SOLD": "SOLUSDT"}.get(und, "BTCUSDT")

    def _log_calibration(self, series: str, spot: float, annual_vol: float, scored: list[ScoredMarket]):
        ts = datetime.now(timezone.utc).isoformat()
        with CALIBRATION_LOG_PATH.open("a") as f:
            for s in scored:
                f.write(json.dumps({
                    "ts": ts, "series": series, "spot": spot, "annual_vol": annual_vol,
                    "ticker": s.ticker, "strike": s.strike, "hours": s.hours_to_close,
                    "fair_prob": s.fair_prob, "yes_ask": s.yes_ask_cents,
                    "yes_bid": s.yes_bid_cents, "edge": s.edge_cents, "volume": s.volume,
                }) + "\n")

    def _size_contracts(self, scored: ScoredMarket, available_cents: int) -> int:
        """Returns number of contracts to buy. 0 means don't trade."""
        f_star = _kelly_fraction(scored.fair_prob, scored.yes_ask_cents)
        if f_star <= 0:
            return 0
        stake_cents = int(available_cents * f_star * self.kelly_fraction)
        stake_cents = min(stake_cents, self.max_cost_per_trade_cents)
        if stake_cents < scored.yes_ask_cents:
            return 0
        count = stake_cents // scored.yes_ask_cents
        return min(count, self.max_contracts_per_ticker)

    async def _scan_series(self, client: AsyncKalshiClient, series: str, held_tickers: set[str]):
        annual_vol = await self._get_annual_vol(self._underlying_from_series(series))
        spot = await fetch_binance_spot(self._binance_symbol_for(series))
        markets = await client.get_markets_full(
            status="open", limit=200, series_ticker=series,
        )
        scored = score_markets(markets, spot, annual_vol)

        if scored:
            self._log_calibration(series, spot, annual_vol, scored)

        eligible = [
            s for s in scored
            if s.edge_cents >= self.min_edge_cents
            and s.fair_prob >= self.min_fair_prob
            and self.min_yes_ask_cents <= s.yes_ask_cents <= self.max_yes_ask_cents
            and 0 < s.hours_to_close <= self.max_days_to_close * 24
            and s.ticker not in held_tickers
        ]
        logger.info(
            f"{series}: spot=${spot:,.2f} vol={annual_vol:.2f} "
            f"scored={len(scored)} eligible={len(eligible)}"
        )
        if not eligible:
            return []
        return eligible

    async def _execute_signal(self, client: AsyncKalshiClient, s: ScoredMarket, count: int):
        if self.dry_run:
            self._dry_run_signals += 1
            logger.info(
                f"[DRY-RUN] WOULD BUY {count}× {s.ticker} @ {s.yes_ask_cents}c "
                f"(fair={s.fair_prob:.3f} edge=+{s.edge_cents:.1f}c)"
            )
            return
        try:
            result = await client.buy_yes(s.ticker, s.yes_ask_cents, count)
            order = result.get("order", {}) if isinstance(result, dict) else {}
            self._trades_placed += 1
            insert_kalshi_trade({
                "order_id": order.get("order_id", ""),
                "ticker": s.ticker,
                "title": s.title,
                "side": "yes",
                "action": "buy",
                "count": count,
                "price_cents": s.yes_ask_cents,
                "total_cost_cents": s.yes_ask_cents * count,
                "status": order.get("status", "placed"),
                "notes": (
                    f"Strikes bot: fair={s.fair_prob:.3f} edge=+{s.edge_cents:.1f}c "
                    f"hours={s.hours_to_close:.1f}"
                ),
            })
            logger.info(
                f"LIVE BUY {count}× {s.ticker} @ {s.yes_ask_cents}c "
                f"(fair={s.fair_prob:.3f} edge=+{s.edge_cents:.1f}c)"
            )
        except Exception as e:
            logger.error(f"Order failed for {s.ticker}: {e}")

    async def _scan_once(self):
        client = get_async_kalshi_client()
        # Account-wide tickers — used ONLY for dedup (don't buy a ticker already owned).
        # NOT used for the bot's position cap — that counts bot-owned positions only.
        try:
            positions = await client.get_positions()
        except Exception as e:
            logger.warning(f"Could not fetch positions, aborting scan: {e}")
            return
        account_held = {p.get("ticker", "") for p in positions if p.get("position", 0) != 0}

        stale = self._bot_held_tickers - account_held
        if stale:
            logger.info(f"Pruning {len(stale)} settled ticker(s) from bot cap: {sorted(stale)}")
            self._bot_held_tickers -= stale

        bot_open = len(self._bot_held_tickers)
        if not self.dry_run and bot_open >= self.max_open_positions:
            logger.info(f"Bot at position cap ({bot_open}/{self.max_open_positions}), idle")
            return
        slots_left = (
            self.max_open_positions  # dry_run: no real cap, just a per-scan fan-out limit
            if self.dry_run
            else (self.max_open_positions - bot_open)
        )

        balance_cents = 0
        try:
            bal = await client.get_balance()
            balance_cents = bal.get("balance", 0) if isinstance(bal, dict) else int(bal or 0)
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e}")
            return
        if not self.dry_run and balance_cents < self.min_yes_ask_cents:
            logger.info("Insufficient balance for any order")
            return

        # Dedup against account-wide held tickers (avoid doubling up if another bot owns it)
        held = set(account_held)
        for series in self.series:
            if slots_left <= 0:
                break
            try:
                eligible = await self._scan_series(client, series, held)
            except Exception as e:
                logger.warning(f"Scan failed for {series}: {e}")
                continue
            for s in eligible:
                if slots_left <= 0:
                    break
                count = self._size_contracts(s, balance_cents)
                if count <= 0:
                    continue
                await self._execute_signal(client, s, count)
                held.add(s.ticker)
                if not self.dry_run:
                    self._bot_held_tickers.add(s.ticker)
                slots_left -= 1

    async def _scan_loop(self):
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Scan loop error: {e}")
            self._scan_count += 1
            self._last_scan_iso = datetime.now(timezone.utc).isoformat()
            try:
                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                break


_singleton: Optional[KalshiCryptoStrikesBot] = None


def get_crypto_strikes_bot() -> KalshiCryptoStrikesBot:
    global _singleton
    if _singleton is None:
        _singleton = KalshiCryptoStrikesBot()
    return _singleton
