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
from app.database import insert_kalshi_trade, get_whale_flow_by_ticker
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
    """Binary-market Kelly for YES side: f* = (p - q) / (1 - q) where q = price as decimal.

    Returns 0 if no edge (p <= q) or if q >= 1 (degenerate market).
    """
    q = yes_ask_cents / 100.0
    if q <= 0 or q >= 1:
        return 0.0
    if fair_prob <= q:
        return 0.0
    return (fair_prob - q) / (1.0 - q)


def _kelly_fraction_no(fair_prob_yes: float, no_price_cents: int) -> float:
    """Binary-market Kelly for NO side: f* = (p_no - q_no) / (1 - q_no).

    p_no = 1 - fair_prob_yes (true probability event does NOT happen)
    q_no = no_price_cents / 100 (cost of NO contract as decimal)
    """
    p_no = 1.0 - fair_prob_yes
    q_no = no_price_cents / 100.0
    if q_no <= 0 or q_no >= 1:
        return 0.0
    if p_no <= q_no:
        return 0.0
    return (p_no - q_no) / (1.0 - q_no)


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
        # "yes" = original strategy (buy cheap OTM YES hoping for big moves)
        # "no" = flipped strategy (buy NO on OTM strikes, high win rate, small profit per win)
        self.side = strikes_cfg.get("side", "no")
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
        # Vol ceiling (2026-06-25): the 72h-EWMA realized vol overshoots after a
        # violent selloff (spiked 0.4→0.9-1.1 in the 06-22→25 BTC drop) and lags
        # the market's forward-looking implied vol. That inflated vol pushed the
        # model's fair_prob ABOVE market prices on every strike → every NO edge
        # went negative → 0 trades for 3 days. Capping the pricing vol keeps a
        # transient realized spike from erasing all edges. 0/None disables the cap.
        self.vol_cap = strikes_cfg.get("vol_cap", 0.60) or float("inf")
        self.max_cost_per_trade_cents = strikes_cfg.get("max_cost_per_trade_cents", 500)
        self.max_contracts_per_ticker = strikes_cfg.get("max_contracts_per_ticker", 10)
        self.max_open_positions = strikes_cfg.get("max_open_positions", 8)
        self.min_yes_ask_cents = strikes_cfg.get("min_yes_ask_cents", 3)
        self.max_yes_ask_cents = strikes_cfg.get("max_yes_ask_cents", 97)
        # NO-side filters: target YES priced at 20-50¢ (NO costs 50-80¢)
        self.no_min_yes_bid_cents = strikes_cfg.get("no_min_yes_bid_cents", 15)
        self.no_max_yes_bid_cents = strikes_cfg.get("no_max_yes_bid_cents", 50)
        self.max_days_to_close = strikes_cfg.get("max_days_to_close", 2)
        # Empirical calibration (2026-05-11): isotonic-regressed pred→actual mapping
        # on top of the parametric model. Fixes the bidirectional overdispersion
        # documented in project_btcd_audit_20260511.md. Disable to use raw parametric
        # probs (for debugging or A/B).
        self.use_calibrator = strikes_cfg.get("use_calibrator", True)

        wf = strikes_cfg.get("whale_follow", {})
        self.whale_follow_enabled = wf.get("enabled", False)
        self.whale_follow_lookback_min = wf.get("lookback_minutes", 120)
        self.whale_follow_min_yes_contracts = wf.get("min_yes_contracts", 100)
        self.whale_follow_max_price_cents = wf.get("max_price_cents", 40)
        self.whale_follow_min_yes_ratio = wf.get("min_yes_ratio", 0.70)
        self.whale_follow_contracts = wf.get("contracts_per_follow", 1)
        self._whale_followed_tickers: set[str] = set()

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
        # Tickers the order API has declared 410 Gone (market closed for orders —
        # these crypto dailies are can_close_early and Kalshi stops accepting
        # orders while the markets-list still reports them "active"). Without a
        # skip memory the bot re-selects the same dead strike every 5-min scan
        # and hammers it (64 failed 410 orders, 0 fills, 2026-06-20→22). Session-
        # scoped: cleared on restart, which is fine — a genuinely re-opened market
        # is rare and will simply be retried then.
        self._gone_tickers: set[str] = set()
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
        wf_status = f"whale_follow={'ON' if self.whale_follow_enabled and self.side != 'no' else 'OFF'}"
        logger.info(
            f"Kalshi crypto strikes bot started [{mode}] side={self.side.upper()} "
            f"series={self.series} interval={self.scan_interval}s "
            f"min_edge={self.min_edge_cents}c min_fair_prob={self.min_fair_prob:.2f} "
            f"kelly={self.kelly_fraction:.2f} {wf_status}"
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
        vol = min(max(raw, self.vol_floor), self.vol_cap)
        self._vol_cache[underlying] = (now, vol)
        capped = " CAPPED" if raw > self.vol_cap else ""
        logger.info(
            f"Vol refresh {underlying}: ewma_hourly={raw:.3f} used={vol:.3f}{capped} "
            f"(lookback={self.vol_lookback_hours}h decay={self.vol_ewma_decay} cap={self.vol_cap})"
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
                    "fair_prob": s.fair_prob, "raw_fair_prob": s.raw_fair_prob,
                    "yes_ask": s.yes_ask_cents,
                    "yes_bid": s.yes_bid_cents, "edge": s.edge_cents, "volume": s.volume,
                }) + "\n")

    def _size_contracts(self, scored: ScoredMarket, available_cents: int) -> int:
        """Returns number of contracts to buy. 0 means don't trade."""
        if self.side == "no":
            f_star = _kelly_fraction_no(scored.fair_prob, scored.no_price_cents)
            price = scored.no_price_cents
        else:
            f_star = _kelly_fraction(scored.fair_prob, scored.yes_ask_cents)
            price = scored.yes_ask_cents
        if f_star <= 0:
            return 0
        stake_cents = int(available_cents * f_star * self.kelly_fraction)
        stake_cents = min(stake_cents, self.max_cost_per_trade_cents)
        if stake_cents < price:
            return 0
        count = stake_cents // price
        return min(count, self.max_contracts_per_ticker)

    async def _scan_series(self, client: AsyncKalshiClient, series: str, held_tickers: set[str]):
        annual_vol = await self._get_annual_vol(self._underlying_from_series(series))
        spot = await fetch_binance_spot(self._binance_symbol_for(series))
        markets = await client.get_markets_full(
            status="open", limit=200, series_ticker=series,
        )
        all_tickers = {m.get("ticker", "") for m in markets if m.get("ticker")}
        scored = score_markets(markets, spot, annual_vol, use_calibrator=self.use_calibrator)

        if scored:
            self._log_calibration(series, spot, annual_vol, scored)

        if self.side == "no":
            eligible = [
                s for s in scored
                if s.no_edge_cents >= self.min_edge_cents
                and s.fair_prob <= (1.0 - self.min_fair_prob)  # YES prob low enough that NO has confidence
                and self.no_min_yes_bid_cents <= s.yes_bid_cents <= self.no_max_yes_bid_cents
                and 0 < s.hours_to_close <= self.max_days_to_close * 24
                and s.ticker not in held_tickers
                and s.ticker not in self._gone_tickers
            ]
            eligible.sort(key=lambda s: s.no_edge_cents, reverse=True)
        else:
            eligible = [
                s for s in scored
                if s.edge_cents >= self.min_edge_cents
                and s.fair_prob >= self.min_fair_prob
                and self.min_yes_ask_cents <= s.yes_ask_cents <= self.max_yes_ask_cents
                and 0 < s.hours_to_close <= self.max_days_to_close * 24
                and s.ticker not in held_tickers
                and s.ticker not in self._gone_tickers
            ]
        logger.info(
            f"{series}: spot=${spot:,.2f} vol={annual_vol:.2f} "
            f"scored={len(scored)} eligible={len(eligible)} side={self.side}"
        )
        if not eligible:
            return [], all_tickers
        return eligible, all_tickers

    async def _execute_signal(self, client: AsyncKalshiClient, s: ScoredMarket, count: int):
        if self.side == "no":
            price = s.no_price_cents
            edge = s.no_edge_cents
            side_label = "NO"
        else:
            price = s.yes_ask_cents
            edge = s.edge_cents
            side_label = "YES"

        if self.dry_run:
            self._dry_run_signals += 1
            logger.info(
                f"[DRY-RUN] WOULD BUY {side_label} {count}× {s.ticker} @ {price}c "
                f"(fair_yes={s.fair_prob:.3f} edge=+{edge:.1f}c)"
            )
            return
        try:
            if self.side == "no":
                result = await client.buy_no(s.ticker, price, count)
            else:
                result = await client.buy_yes(s.ticker, price, count)
            order = result.get("order", {}) if isinstance(result, dict) else {}
            self._trades_placed += 1
            insert_kalshi_trade({
                "order_id": order.get("order_id", ""),
                "ticker": s.ticker,
                "title": s.title,
                "side": "no" if self.side == "no" else "yes",
                "action": "buy",
                "count": count,
                "price_cents": price,
                "total_cost_cents": price * count,
                "status": order.get("status", "placed"),
                "notes": (
                    f"Strikes bot ({side_label}): fair_yes={s.fair_prob:.3f} "
                    f"edge=+{edge:.1f}c hours={s.hours_to_close:.1f}"
                ),
            })
            logger.info(
                f"LIVE BUY {side_label} {count}× {s.ticker} @ {price}c "
                f"(fair_yes={s.fair_prob:.3f} edge=+{edge:.1f}c)"
            )
        except Exception as e:
            # 410 Gone = market closed for orders (can_close_early) though still
            # listed "active". Remember it so we stop re-selecting + re-hammering
            # the same dead strike every scan; other live markets stay tradeable.
            if "410" in str(e) or "Gone" in str(e):
                self._gone_tickers.add(s.ticker)
                logger.warning(
                    f"Order 410 Gone for {s.ticker} — market closed for orders; "
                    f"skipping it this session ({len(self._gone_tickers)} gone)"
                )
            else:
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
        open_market_tickers: set[str] = set()
        for series in self.series:
            if slots_left <= 0:
                break
            try:
                eligible, all_tickers = await self._scan_series(client, series, held)
                open_market_tickers.update(all_tickers)
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

        await self._whale_follow_scan(client, held, balance_cents, open_market_tickers)

    async def _whale_follow_scan(self, client: AsyncKalshiClient, held_tickers: set[str], balance_cents: int, open_market_tickers: set[str] | None = None):
        """Check DB for recent heavy whale YES flow on cheap strikes and follow with 1 contract."""
        if not self.whale_follow_enabled:
            return
        # Whale-follow is a YES-momentum signal — skip when running NO-side strategy
        if self.side == "no":
            return

        series_prefixes = [s.replace("KX", "") for s in self.series]
        prefixes = [f"KX{p}" for p in series_prefixes]
        flows = get_whale_flow_by_ticker(prefixes, self.whale_follow_lookback_min)
        if not flows:
            return

        for f in flows:
            ticker = f["ticker"]
            if ticker in held_tickers or ticker in self._whale_followed_tickers:
                continue
            if open_market_tickers and ticker not in open_market_tickers:
                continue

            yes_c = f["yes_contracts"] or 0
            no_c = f["no_contracts"] or 0
            total = yes_c + no_c
            if total == 0 or yes_c < self.whale_follow_min_yes_contracts:
                continue
            yes_ratio = yes_c / total
            if yes_ratio < self.whale_follow_min_yes_ratio:
                continue
            avg_price = int(f["avg_yes_price_cents"] or 99)
            if avg_price > self.whale_follow_max_price_cents:
                continue

            cost = avg_price * self.whale_follow_contracts
            if not self.dry_run and cost > balance_cents:
                continue

            logger.info(
                f"WHALE-FOLLOW: {ticker} — {yes_c} YES contracts ({yes_ratio:.0%}) "
                f"avg {avg_price}c in last {self.whale_follow_lookback_min}min"
            )

            if self.dry_run:
                self._dry_run_signals += 1
                logger.info(f"[DRY-RUN] WOULD WHALE-FOLLOW BUY {self.whale_follow_contracts}× {ticker} @ ~{avg_price}c")
                continue

            try:
                result = await client.buy_yes(ticker, avg_price, self.whale_follow_contracts)
                order = result.get("order", {}) if isinstance(result, dict) else {}
                self._trades_placed += 1
                self._bot_held_tickers.add(ticker)
                self._whale_followed_tickers.add(ticker)
                insert_kalshi_trade({
                    "order_id": order.get("order_id", ""),
                    "ticker": ticker,
                    "title": ticker,
                    "side": "yes",
                    "action": "buy",
                    "count": self.whale_follow_contracts,
                    "price_cents": avg_price,
                    "total_cost_cents": cost,
                    "status": order.get("status", "placed"),
                    "notes": (
                        f"Whale-follow: {yes_c} YES/{no_c} NO contracts, "
                        f"ratio={yes_ratio:.2f}, avg_price={avg_price}c"
                    ),
                })
                balance_cents -= cost
            except Exception as e:
                logger.error(f"Whale-follow order failed for {ticker}: {e}")

        stale = self._whale_followed_tickers - held_tickers
        if stale:
            self._whale_followed_tickers -= stale

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
