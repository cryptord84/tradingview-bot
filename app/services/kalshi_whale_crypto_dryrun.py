"""Whale-confirmed crypto strikes — DRY-RUN measurement module (2026-06-29).

Purpose: validate the backtested edge LIVE without risk before committing real
capital. The 2026-06-29 whale backtest (447 settled markets) found that following
net whale conviction on SINGLE-OUTCOME crypto strikes, in the mid price band
(50-88c), wins ~84-91% with a +11..+17c/contract edge — strongest on BTC15M
(+17c) and BTCD (+16c). Blind whale-following is -1c (favorite bias + parlay
poison), so the edge is specifically: single-outcome crypto + whale confirmation
+ mid-band entry.

This module does NOT place orders. Every scan it:
  1. pulls fresh net whale flow per crypto strike (BTC/ETH daily + 15-min),
  2. when one side has strong conviction AND the price we could ACTUALLY enter
     at (live book ask) sits in the mid-band, logs a paper signal capturing the
     whale's price, our achievable entry price, and the slippage between them,
  3. on settlement, fills in the outcome and realized P&L at the achievable
     entry price.

That measures the one thing a backtest cannot: real entry slippage (you enter
AFTER the whale moved the price) and whether the markets are even orderable at
signal time. If dry-run edge survives slippage, the live version is justified.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import get
from app.services.kalshi_client import get_async_kalshi_client
from app.database import (
    get_whale_flow_by_ticker,
    insert_whale_dryrun,
    get_open_whale_dryruns,
    settle_whale_dryrun,
)

logger = logging.getLogger("bot.kalshi.whale_dryrun")

_DEFAULTS = {
    "enabled": False,
    "scan_interval_seconds": 120,   # 2026-06-29: 300→120 so 15-min markets (15min life) get caught
    "series": ["KXBTCD", "KXBTC15M", "KXETHD", "KXETH15M"],
    # Lookback must not outlive the market. 2026-06-29: a single 30m window
    # outlived the 15-min markets entirely (they settled before conviction formed,
    # killing the strongest-edge lane), so it's split by market life:
    "whale_lookback_minutes": 30,   # daily strikes (BTCD/ETHD, ~24h life)
    "lookback_15m_minutes": 8,      # 15-min strikes (BTC15M/ETH15M) — must be < their life
    "min_15m_hours_to_close": 0.05, # 15M: need >=3 min left to "enter" before settle
    "min_whale_contracts": 100,     # ignore tiny flow
    # 2026-06-29: 0.6→0.4. The backtest followed NET whale side at any lean; 0.6
    # logged ~1/609 tickers (too strict + a different subset than validated). 0.4
    # (~60/40) is a meaningful lean with workable signal volume; conviction is
    # recorded per signal so edge-by-conviction can be analyzed from the data.
    "min_conviction": 0.4,
    "entry_band_lo_cents": 50,      # mid-band: the backtested edge zone
    "entry_band_hi_cents": 88,
}


def _cfg() -> dict:
    c = dict(_DEFAULTS)
    c.update((get("kalshi") or {}).get("whale_crypto_dryrun") or {})
    return c


class WhaleCryptoDryRun:
    def __init__(self):
        c = _cfg()
        self.enabled = bool(c["enabled"])
        self.interval = float(c["scan_interval_seconds"])
        self.series = list(c["series"])
        self.lookback_min = int(c["whale_lookback_minutes"])
        self.lookback_15m = int(c["lookback_15m_minutes"])
        self.min_15m_hours = float(c["min_15m_hours_to_close"])
        self.min_contracts = int(c["min_whale_contracts"])
        self.min_conviction = float(c["min_conviction"])
        self.band_lo = int(c["entry_band_lo_cents"])
        self.band_hi = int(c["entry_band_hi_cents"])
        # Partition series by market life: 15-min strikes need a short lookback.
        self.series_15m = [s for s in self.series if "15M" in s]
        self.series_daily = [s for s in self.series if "15M" not in s]
        self._task = None
        self._running = False
        self._scans = 0
        self._signals_logged = 0

    def start(self) -> asyncio.Task:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"Whale-crypto DRY-RUN started: series={self.series} "
            f"band={self.band_lo}-{self.band_hi}c conviction>={self.min_conviction} "
            f"lookback={self.lookback_min}m (NO real orders)"
        )
        return self._task

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        while self._running:
            try:
                await self._scan()
                await self._settle_pass()
            except Exception as e:
                logger.error(f"Whale-crypto dry-run scan error: {e}")
            await asyncio.sleep(self.interval)

    async def _scan(self):
        self._scans += 1
        # Net whale flow per ticker, with a lookback matched to the market's life:
        # daily strikes use a 30m window; 15-min strikes use a short window (a 30m
        # window outlives them). Tag each row so the 15M time-left guard applies.
        flow = []
        if self.series_daily:
            flow += [(f, False) for f in
                     get_whale_flow_by_ticker(self.series_daily, lookback_minutes=self.lookback_min)]
        if self.series_15m:
            flow += [(f, True) for f in
                     get_whale_flow_by_ticker(self.series_15m, lookback_minutes=self.lookback_15m)]
        if not flow:
            return
        client = get_async_kalshi_client()
        already = {(d["ticker"], d["whale_side"]) for d in get_open_whale_dryruns()}

        for f, is_15m in flow:
            yes_c = f.get("yes_contracts", 0) or 0
            no_c = f.get("no_contracts", 0) or 0
            total = yes_c + no_c
            if total < self.min_contracts:
                continue
            # Net conviction side + strength
            side = "yes" if yes_c >= no_c else "no"
            conviction = abs(yes_c - no_c) / total
            if conviction < self.min_conviction:
                continue
            ticker = f["ticker"]
            if (ticker, side) in already:
                continue  # one paper signal per (ticker, side)

            # whale's cost-weighted avg price on the conviction side
            cost = (f.get("yes_cost_cents") if side == "yes" else f.get("no_cost_cents")) or 0
            cnt = yes_c if side == "yes" else no_c
            whale_price = round(cost / cnt) if cnt else 0

            # Live book: the price we could ACTUALLY enter at right now.
            try:
                m = await client.get_market(ticker)
            except Exception as e:
                logger.debug(f"dry-run get_market {ticker} failed: {e}")
                continue
            if not m or (m.get("status") not in (None, "active", "open")):
                continue
            close_time = m.get("close_time") or ""
            hours = self._hours_to_close(close_time)
            # 15M guard: need enough time left to realistically enter before settle.
            if is_15m and (hours is None or hours < self.min_15m_hours):
                continue
            # buying YES → pay yes_ask; buying NO → pay no_ask (≈100-yes_bid)
            entry = m.get("yes_ask") if side == "yes" else m.get("no_ask")
            book_present = entry is not None and entry > 0

            # Mid-band gate is on the ACHIEVABLE entry price (what we'd pay), not
            # the whale's price — that's the realistic test.
            in_band = book_present and self.band_lo <= entry <= self.band_hi
            if not in_band:
                continue

            insert_whale_dryrun({
                "ticker": ticker,
                "series": self._series_of(ticker),
                "whale_side": side,
                "whale_price_cents": whale_price,
                "entry_price_cents": entry,
                "slippage_cents": entry - whale_price,  # +ve = we enter worse than whale
                "conviction": round(conviction, 3),
                "whale_contracts": total,
                "hours_to_close": round(hours, 3) if hours is not None else None,
                "close_time": close_time,
            })
            self._signals_logged += 1
            logger.info(
                f"[DRY-RUN] whale-confirmed {side.upper()} {ticker}: "
                f"whale@{whale_price}c entry@{entry}c (slip {entry-whale_price:+d}c) "
                f"conviction={conviction:.2f} contracts={total} hours={hours}"
            )

    async def _settle_pass(self):
        """Resolve outcomes for paper signals whose market has settled."""
        client = get_async_kalshi_client()
        for d in get_open_whale_dryruns():
            try:
                m = await client.get_market(d["ticker"])
            except Exception:
                continue
            result = (m or {}).get("result", "")
            if result not in ("yes", "no"):
                continue
            won = (result == d["whale_side"])
            entry = d["entry_price_cents"]
            # paper P&L per contract at the achievable entry price
            pnl = (100 - entry) if won else -entry
            settle_whale_dryrun(d["id"], result=result, won=int(won), realized_pnl_cents=pnl)
            logger.info(
                f"[DRY-RUN] settled {d['ticker']} {d['whale_side']}: result={result} "
                f"{'WON' if won else 'lost'} entry@{entry}c paper_pnl={pnl:+d}c"
            )

    @staticmethod
    def _hours_to_close(close_iso: str):
        if not close_iso:
            return None
        try:
            dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except Exception:
            return None

    def _series_of(self, ticker: str) -> str:
        for s in self.series:
            if ticker.startswith(s + "-"):
                return s
        return ticker.split("-")[0]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled, "running": self._running,
            "scans": self._scans, "signals_logged": self._signals_logged,
        }


_bot = None


def get_whale_crypto_dryrun() -> "WhaleCryptoDryRun":
    global _bot
    if _bot is None:
        _bot = WhaleCryptoDryRun()
    return _bot
