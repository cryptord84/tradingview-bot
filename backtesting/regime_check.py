"""Quantitative regime check: does today's BTC/ETH/SOL state actually match
the post-FTX / mid-2023 / summer-2024 analog windows where Stoch RSI/ETH/4H
ran PF 2.03?

Computes 5 metrics per token:
  1. Volatility:    14-day ATR / price (% per day)
  2. Trend slope:   30-day slope of 50-day MA (% per day)
  3. MA distance:   % above/below 200-day MA
  4. Drawdown:      % below 1-year high
  5. BB width:      Bollinger bandwidth (compression proxy)

Then computes "now" (last 30 days) vs each analog window (mean during it).
Similarity score = fraction of metrics within ±25% of analog values.

Decision rule:
  - ≥0.6 (3 of 5 metrics match) → CLEAR ANALOG: deploy with confidence
  - 0.4-0.6 (2 metrics)         → PARTIAL ANALOG: deploy with smaller size
  - <0.4                        → NOT AN ANALOG: don't deploy yet

Output to stdout + saves to backtesting/results/regime_check_<ts>.txt
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime

from backtesting.data import fetch_binance


ANALOG_WINDOWS = [
    ("post-FTX",         "2022-11-15", "2023-03-08"),
    ("mid-2023 sideways","2023-08-01", "2023-10-15"),
    ("summer 2024",      "2024-07-01", "2024-09-30"),
]

TOKENS = ["BTC", "ETH", "SOL"]

# How close metrics need to be to count as "matching" (% of analog value)
MATCH_TOLERANCE = 0.25


def compute_metrics(df: pd.DataFrame) -> dict:
    """Compute 5 regime metrics from a price dataframe. Uses last bar's view."""
    if len(df) < 200:
        return {}

    high = df["high"]
    low  = df["low"]
    close = df["close"]

    # 1. Volatility: 14-period ATR / close (rolling daily-equiv % vol)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    vol_pct = (atr14 / close).iloc[-1] * 100  # % per bar

    # 2. Trend slope: linregress of 50-bar MA over last 30 bars (% per bar)
    ma50 = close.rolling(50).mean()
    recent = ma50.iloc[-30:].values
    if len(recent) < 30 or np.isnan(recent).any():
        trend_slope_pct = 0.0
    else:
        x = np.arange(len(recent))
        slope, _ = np.polyfit(x, recent, 1)
        trend_slope_pct = (slope / recent[-1]) * 100  # % per bar

    # 3. Distance from 200-bar MA (%)
    ma200 = close.rolling(200).mean()
    ma_dist_pct = ((close.iloc[-1] / ma200.iloc[-1]) - 1) * 100

    # 4. Drawdown from rolling high (~1 year for 4H = 2190 bars; clamp to available)
    lookback = min(len(close), 2190)
    high_lookback = close.rolling(lookback).max()
    dd_pct = ((close.iloc[-1] / high_lookback.iloc[-1]) - 1) * 100  # negative = down

    # 5. Bollinger bandwidth: (upper - lower) / middle (compression proxy)
    period = 20
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    bb_width_pct = ((sma + 2*std) - (sma - 2*std)) / sma * 100
    bb_w = bb_width_pct.iloc[-1]

    return {
        "vol_pct":     float(vol_pct),
        "trend_slope": float(trend_slope_pct),
        "ma_dist":     float(ma_dist_pct),
        "drawdown":    float(dd_pct),
        "bb_width":    float(bb_w),
    }


def metrics_in_window(df: pd.DataFrame, start: str, end: str) -> dict:
    """Compute metrics on each bar in the window, return the mean."""
    df_w = df[(df.index >= start) & (df.index <= end)]
    if len(df_w) < 50:
        return {}

    # Compute metrics at each bar (using df up to that bar for rolling values)
    samples = []
    for end_idx in range(50, len(df_w), 5):  # every 5 bars to keep it fast
        cutoff = df_w.index[end_idx]
        df_up_to = df[df.index <= cutoff]
        if len(df_up_to) < 200:
            continue
        m = compute_metrics(df_up_to)
        if m:
            samples.append(m)
    if not samples:
        return {}
    return {k: float(np.mean([s[k] for s in samples])) for k in samples[0]}


def similarity(now: dict, window: dict) -> tuple[float, dict]:
    """Compute similarity score (0-1) between two metric dicts. Returns
    (score, per_metric_match_dict)."""
    if not now or not window:
        return 0.0, {}
    matches = {}
    for k in now:
        n_val = now[k]
        w_val = window.get(k)
        if w_val is None:
            matches[k] = False
            continue
        # Tolerance is relative to the analog value (or absolute for near-zero)
        denom = abs(w_val) if abs(w_val) > 0.5 else 1.0
        rel_diff = abs(n_val - w_val) / denom
        matches[k] = rel_diff <= MATCH_TOLERANCE
    score = sum(matches.values()) / len(matches) if matches else 0.0
    return score, matches


def main():
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    out_path = f"backtesting/results/regime_check_{ts}.txt"
    lines = []
    def emit(s=""):
        print(s); lines.append(s)

    emit("=" * 88)
    emit(f"REGIME CHECK — current vs analog windows ({datetime.utcnow():%Y-%m-%d %H:%M UTC})")
    emit("=" * 88)
    emit("Hypothesis: current crypto sideways state resembles past end-of-bear regimes")
    emit("where Stoch RSI / ETH / 4H ran PF 2.03 vs 0.61 baseline.")
    emit("Decision rule:  ≥0.6 similarity (3+ of 5 metrics) → DEPLOY WITH CONFIDENCE")
    emit("                0.4-0.6                            → PARTIAL ANALOG (smaller size)")
    emit("                <0.4                               → NOT AN ANALOG (skip)")
    emit("")

    # Fetch data once
    data = {}
    for t in TOKENS:
        df = fetch_binance(f"{t}USDT", "4h", bars=10000)
        if df is None:
            continue
        data[t] = df

    for t, df in data.items():
        emit(f"\n{'─' * 88}")
        emit(f"{t}/4H  ({len(df)} bars, {df.index[0].date()} → {df.index[-1].date()})")
        emit(f"{'─' * 88}")

        # NOW = mean metrics over last ~30 days (180 bars at 4H)
        recent_start = df.index[-180].strftime("%Y-%m-%d")
        recent_end   = df.index[-1].strftime("%Y-%m-%d")
        now_metrics = metrics_in_window(df, recent_start, recent_end)
        if not now_metrics:
            emit("  (insufficient recent data)")
            continue

        emit(f"\n  Current ({recent_start} → {recent_end}, last ~30 days):")
        for k, v in now_metrics.items():
            emit(f"    {k:<12}: {v:>+8.3f}")

        # Compare to each analog window
        scores = []
        for label, start, end in ANALOG_WINDOWS:
            win_metrics = metrics_in_window(df, start, end)
            if not win_metrics:
                emit(f"\n  vs {label} ({start} → {end}): (no data)")
                continue
            score, matches = similarity(now_metrics, win_metrics)
            scores.append((label, score))
            emit(f"\n  vs {label} ({start} → {end})  similarity: {score:.0%}")
            for k in now_metrics:
                n = now_metrics[k]
                w = win_metrics.get(k, 0)
                m = matches.get(k, False)
                tag = "✓" if m else "✗"
                emit(f"    {tag} {k:<12}: now {n:>+8.3f} vs window {w:>+8.3f}  "
                     f"(Δ {abs(n-w):.3f})")

        # Verdict
        if scores:
            best = max(scores, key=lambda x: x[1])
            emit(f"\n  → Best analog: {best[0]} (similarity {best[1]:.0%})")
            if best[1] >= 0.6:
                emit(f"    VERDICT: CLEAR ANALOG — current regime resembles {best[0]}")
            elif best[1] >= 0.4:
                emit(f"    VERDICT: PARTIAL ANALOG — some resemblance to {best[0]}")
            else:
                emit(f"    VERDICT: NOT AN ANALOG — current regime differs from all 3 windows")

    emit("")
    emit("=" * 88)
    emit("APPLY")
    emit("  ETH high similarity → deploy Stoch RSI / ETH / 4H (PF 2.03 in analog)")
    emit("  SOL high similarity → consider Liq Sweep / SOL / 4H (showed up in 2 of 3 windows)")
    emit("  No matches → hold current 5 WF passers, don't add regime-bet alerts yet")
    emit("=" * 88)

    os.makedirs("backtesting/results", exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
