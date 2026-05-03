"""Regime-conditional backtest: runs all strategies on historical end-of-bear
sideways windows that PRECEDED bull breakouts. Compares PF/WR/N in each
analog window vs full-history baseline.

Use case: we're currently in (per user observation) a steady sideways market
likely near the end of a bear cycle. This script answers: "in past similar
periods, which strategies actually made money before the breakout?"

Analog windows (post-2021 only — that's when Binance.US data starts):
  1. 2022-Q4 → 2023-Q1: post-FTX bottom + sideways → broke out Mar 2023 (ETF/banking)
  2. 2023-Q3:           sideways $26-30k BTC → broke out Oct-Dec 2023 (ETF approach)
  3. 2024-Q3:           consolidation → broke out Q4 2024

Tokens: BTC, ETH, SOL (only majors with multi-cycle data on Binance.US).
Memecoins excluded — most don't have a single full bear cycle to analog from.

Outputs: stdout table + saves to backtesting/results/regime_analog_<ts>.txt
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime

from backtesting.data import fetch_binance
from backtesting.engine import run_backtest, RiskConfig, DEFAULT_RISK
from backtesting.strategies import STRATEGIES


# ── Analog windows ──────────────────────────────────────────────────────────
# Each window is (label, start_date, end_date, what_came_after).
# Dates are inclusive; chosen to capture the sideways/coiling phase BEFORE the
# breakout (so we measure how strategies behaved while the market was
# range-bound, not the breakout move itself).
ANALOG_WINDOWS = [
    ("post-FTX (2022-Q4→2023-Q1)", "2022-11-15", "2023-03-08",
     "bull breakout Mar 2023 (banking crisis catalyst)"),
    ("mid-2023 sideways (2023-Q3)", "2023-08-01", "2023-10-15",
     "bull rally Oct-Dec 2023 (ETF approach)"),
    ("summer 2024 consolidation",   "2024-07-01", "2024-09-30",
     "bull rally Q4 2024"),
]

TOKENS = ["BTC", "ETH", "SOL"]
TIMEFRAMES = [("1h", "1H"), ("4h", "4H"), ("1d", "1D")]


def run_window(df: pd.DataFrame, start: str, end: str, token: str,
               tf_label: str) -> list[dict]:
    """Run all strategies on a date-filtered slice. Returns list of result dicts."""
    df_window = df[(df.index >= start) & (df.index <= end)]
    if len(df_window) < 50:
        return []

    out = []
    for strat_name, strat_fn in STRATEGIES.items():
        try:
            sigs = strat_fn(df_window, enable_short=False)
            r = run_backtest(df_window, sigs, token, strat_name, tf_label,
                             risk=DEFAULT_RISK)
            out.append({
                "strategy": strat_name,
                "token":    token,
                "tf":       tf_label,
                "bars":     len(df_window),
                "pf":       r.profit_factor,
                "wr":       r.win_rate,
                "n":        r.trade_count,
                "dd":       r.max_drawdown,
                "np_pct":   r.net_profit,
                "passed":   r.passed,
            })
        except Exception as e:
            print(f"  err {strat_name}/{token}/{tf_label}: {e}")
    return out


def main():
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    out_path = f"backtesting/results/regime_analog_{ts}.txt"
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 90)
    emit(f"REGIME-CONDITIONAL BACKTEST — analog windows ({datetime.utcnow():%Y-%m-%d %H:%M UTC})")
    emit("=" * 90)
    emit("Hypothesis: current crypto regime resembles past end-of-bear sideways periods.")
    emit("Test: which strategies performed well in those past periods?")
    emit("")

    # Pre-fetch all token/TF data once
    data: dict = {}
    for token in TOKENS:
        for tf_internal, tf_label in TIMEFRAMES:
            pair = f"{token}USDT"
            print(f"  fetching {token}/{tf_label}...", end=" ", flush=True)
            df = fetch_binance(pair, tf_internal, bars=10000)
            if df is None or len(df) < 100:
                print("SKIP")
                continue
            data[(token, tf_label)] = df
            print(f"{len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")

    # Baseline: full-history PF for each (strategy, token, TF)
    emit("\n=== Baseline: full-history PF per combo ===\n")
    baseline: dict = {}
    for (token, tf_label), df in data.items():
        for strat_name, strat_fn in STRATEGIES.items():
            try:
                sigs = strat_fn(df, enable_short=False)
                r = run_backtest(df, sigs, token, strat_name, tf_label, risk=DEFAULT_RISK)
                baseline[(strat_name, token, tf_label)] = r
            except Exception:
                pass

    emit(f"  {'Strategy':<14} {'Token':<6} {'TF':<3}  {'PF':>6}  {'WR%':>5}  {'N':>5}  {'NP%':>7}")
    emit("  " + "-" * 70)
    for k, r in sorted(baseline.items(), key=lambda kv: -kv[1].profit_factor):
        if r.trade_count >= 30:
            emit(f"  {k[0]:<14} {k[1]:<6} {k[2]:<3}  {r.profit_factor:>6.2f}  {r.win_rate:>5.1f}  "
                 f"{r.trade_count:>5}  {r.net_profit:>+6.1f}")

    # Per-window results
    for window_label, start, end, aftermath in ANALOG_WINDOWS:
        emit("")
        emit("=" * 90)
        emit(f"WINDOW: {window_label}  ({start} → {end})")
        emit(f"What followed: {aftermath}")
        emit("=" * 90)

        rows = []
        for (token, tf_label), df in data.items():
            rows.extend(run_window(df, start, end, token, tf_label))

        if not rows:
            emit("  (no data in this window)")
            continue

        # Sort: passing combos first by PF desc, then non-passing by PF
        rows.sort(key=lambda r: (-(r["n"] >= 15), -r["pf"]))

        emit(f"\n  {'Strategy':<14} {'Token':<6} {'TF':<3}  {'Win-PF':>7} {'Win-WR':>7} {'N':>4}  "
             f"{'Base-PF':>8}  {'Δ PF':>7}  {'Window-NP%':>11}")
        emit("  " + "-" * 90)
        for r in rows:
            base_r = baseline.get((r["strategy"], r["token"], r["tf"]))
            base_pf = base_r.profit_factor if base_r else 0
            delta = r["pf"] - base_pf
            tag = ""
            if r["n"] >= 15 and r["pf"] >= 1.4 and r["pf"] > base_pf * 1.2:
                tag = " ★"  # window-strong AND beats baseline by 20%+
            elif r["n"] >= 15 and r["pf"] >= 1.4:
                tag = " ✓"  # passes window threshold
            emit(f"  {r['strategy']:<14} {r['token']:<6} {r['tf']:<3}  "
                 f"{r['pf']:>7.2f} {r['wr']:>6.1f}% {r['n']:>4}  "
                 f"{base_pf:>8.2f}  {delta:>+7.2f}  {r['np_pct']:>+10.1f}%{tag}")

    emit("")
    emit("=" * 90)
    emit("LEGEND")
    emit("  ★ = passed window (PF≥1.4, N≥15) AND outperformed full-history baseline by 20%+")
    emit("  ✓ = passed window threshold but no significant baseline outperformance")
    emit("  N<15 trades = sample too small to be meaningful")
    emit("")
    emit("INTERPRETATION")
    emit("  ★ rows are strategies that thrived specifically in past sideways/end-of-bear")
    emit("  conditions. If current regime is similar, expect them to outperform their")
    emit("  long-run baseline. Worth weighting tier sizing toward these slots.")
    emit("=" * 90)

    os.makedirs("backtesting/results", exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
