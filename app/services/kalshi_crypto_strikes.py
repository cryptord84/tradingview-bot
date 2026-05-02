"""Probabilistic pricing model for Kalshi daily crypto-strike markets.

Targets KXBTCD (Bitcoin daily strikes). Model: realized-vol GBM + normal CDF.
No drift, no Bayesian updating, no intraday recalibration — minimal MVP.

Inputs: spot price, annualized vol, hours to close, strike
Output: fair_prob that underlying settles >= strike at close

Calibration of vol + formula is the load-bearing part. Everything else
(scan loop, execution, sizing) lives in kalshi_crypto_strikes_bot.py (task 8).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("bot.kalshi.strikes")

SECONDS_PER_YEAR = 365.0 * 24 * 3600
BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"
_SUBTITLE_STRIKE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")


@dataclass
class ScoredMarket:
    ticker: str
    title: str
    strike: float
    yes_ask_cents: int
    yes_bid_cents: int
    hours_to_close: float
    fair_prob: float
    edge_cents: float  # fair_prob*100 - yes_ask_cents, positive = buy YES has edge
    volume: float


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_prob(spot: float, strike: float, hours_to_close: float, annual_vol: float) -> float:
    """P(S_T >= K) under driftless GBM. Returns 0..1.

    For daily horizons the μ - σ²/2 drift term is ~6e-4 — negligible, so we skip it.
    """
    if spot <= 0 or strike <= 0 or annual_vol <= 0:
        return 0.5
    if hours_to_close <= 0:
        return 1.0 if spot >= strike else 0.0
    t_years = hours_to_close / (365.0 * 24.0)
    sigma_t = annual_vol * math.sqrt(t_years)
    if sigma_t <= 0:
        return 1.0 if spot >= strike else 0.0
    d = math.log(strike / spot) / sigma_t
    return 1.0 - _norm_cdf(d)


def realized_vol(daily_closes: list[float]) -> float:
    """Annualized volatility from daily log returns. Returns 0 on insufficient data.

    Retained for tests and fallback. Production uses ewma_realized_vol on hourly data.
    """
    if len(daily_closes) < 2:
        return 0.0
    returns = [
        math.log(daily_closes[i] / daily_closes[i - 1])
        for i in range(1, len(daily_closes))
        if daily_closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(365.0)


def ewma_realized_vol(hourly_closes: list[float], decay: float = 0.97) -> float:
    """EWMA annualized volatility from hourly log returns. Returns 0 on insufficient data.

    σ²_t = λ·σ²_{t-1} + (1-λ)·r²_t  (RiskMetrics-style, zero-mean returns).
    decay=0.97 on hourly data → ~23h half-life, responsive enough for sub-24h strikes
    but smooth enough to avoid whiplash from a single hour's outlier move.
    Added 2026-04-24 to replace 30d daily-close realized_vol, which overpredicted
    OTM YES on short-dated BTCD markets (calibration check 2026-04-23).
    """
    if len(hourly_closes) < 2:
        return 0.0
    returns = [
        math.log(hourly_closes[i] / hourly_closes[i - 1])
        for i in range(1, len(hourly_closes))
        if hourly_closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    # Seed variance from mean squared return, then iterate oldest → newest
    var = sum(r * r for r in returns) / len(returns)
    for r in returns:
        var = decay * var + (1.0 - decay) * r * r
    hourly_std = math.sqrt(var)
    return hourly_std * math.sqrt(365.0 * 24.0)


async def fetch_binance_daily_closes(symbol: str = "BTCUSDT", days: int = 31) -> list[float]:
    """Fetch daily closes from Binance.us. Returns oldest → newest."""
    params = {"symbol": symbol, "interval": "1d", "limit": days}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(BINANCE_KLINES_URL, params=params)
        r.raise_for_status()
        klines = r.json()
    # kline[4] is close price (string)
    return [float(k[4]) for k in klines]


async def fetch_binance_hourly_closes(symbol: str = "BTCUSDT", hours: int = 72) -> list[float]:
    """Fetch 1h closes from Binance.us. Returns oldest → newest."""
    params = {"symbol": symbol, "interval": "1h", "limit": hours}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(BINANCE_KLINES_URL, params=params)
        r.raise_for_status()
        klines = r.json()
    return [float(k[4]) for k in klines]


async def fetch_binance_spot(symbol: str = "BTCUSDT") -> float:
    """Current spot price."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.binance.us/api/v3/ticker/price", params={"symbol": symbol})
        r.raise_for_status()
        return float(r.json()["price"])


def parse_strike_from_subtitle(subtitle: str) -> Optional[float]:
    """Parse strike from Kalshi subtitle like '$86,250 or above'."""
    m = _SUBTITLE_STRIKE_RE.search(subtitle or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def hours_until(close_time_iso: str) -> float:
    """Hours from now to close_time. Negative if past."""
    if not close_time_iso:
        return 0.0
    try:
        close_dt = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    delta = close_dt - datetime.now(timezone.utc)
    return delta.total_seconds() / 3600.0


def score_market(market: dict, spot: float, annual_vol: float) -> Optional[ScoredMarket]:
    """Score a single Kalshi BTCD market. Returns None if unparseable."""
    ticker = market.get("ticker", "")
    subtitle = market.get("subtitle", "")
    title = market.get("title", "")
    strike = parse_strike_from_subtitle(subtitle)
    if strike is None:
        return None

    try:
        yes_ask = int(round(float(market.get("yes_ask_dollars", "0") or "0") * 100))
        yes_bid = int(round(float(market.get("yes_bid_dollars", "0") or "0") * 100))
    except (ValueError, TypeError):
        return None

    hours = hours_until(market.get("close_time", "") or market.get("expected_expiration_time", ""))
    prob = fair_prob(spot, strike, hours, annual_vol)
    edge = prob * 100.0 - yes_ask

    try:
        vol = float(market.get("volume_fp", "0") or "0")
    except (ValueError, TypeError):
        vol = 0.0

    return ScoredMarket(
        ticker=ticker,
        title=title,
        strike=strike,
        yes_ask_cents=yes_ask,
        yes_bid_cents=yes_bid,
        hours_to_close=hours,
        fair_prob=prob,
        edge_cents=edge,
        volume=vol,
    )


def score_markets(markets: list[dict], spot: float, annual_vol: float) -> list[ScoredMarket]:
    """Score a batch of markets. Skips unparseable ones. Sorted by edge descending."""
    scored = [s for m in markets if (s := score_market(m, spot, annual_vol)) is not None]
    scored.sort(key=lambda s: s.edge_cents, reverse=True)
    return scored
