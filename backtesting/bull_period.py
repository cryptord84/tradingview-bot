"""Bull-period backtest: runs all strategies on past sustained-uptrend windows.

Mirror of regime_analog.py but flipped: instead of testing END-OF-BEAR sideways
windows that PRECEDED breakouts, this tests the BULL WINDOWS THEMSELVES.
Tells us which strategy × token combos crushed during sustained uptrends so we
can position sizing/alerts ahead of the next bull cycle.

Bull windows (since Binance.US data start in 2021):
  1. 2023-Q4 → 2024-Q1: ETF approach, BTC $26k → $73k ATH (Mar 2024)
  2. 2024-Q4 → 2025-Q1: post-halving + election, BTC $60k → ~$108k ATH (early 2025)

Tokens: full FOCUS_TOKENS universe (multi-source). Tokens with insufficient
history in a window are auto-skipped.
Timeframes: 4H, 1H. Skips 15m (data depth too shallow on most sources).

Output:
  - stdout summary
  - backtesting/results/bull_period_<ts>.txt (full ranked tables)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime

from backtesting.data import fetch_all, TIMEFRAMES
from backtesting.engine import run_backtest, DEFAULT_RISK
from backtesting.strategies import STRATEGIES


# ── Bull windows ─────────────────────────────────────────────────────────────
# (label, start_date, end_date, narrative)
BULL_WINDOWS = [
    ("2023-Q4 → 2024-Q1 bull", "2023-10-15", "2024-03-31",
     "BTC $26k→$73k ATH on ETF approach + post-FTX recovery"),
    ("2024-Q4 → 2025-Q1 bull", "2024-10-01", "2025-02-28",
     "BTC $60k→~$108k on post-halving + election rally"),
]

# Just 4H and 1H — most strategies are designed for these. 15m data depth is
# also thin pre-2024 on most sources.
FOCUS_TFS = ["1H", "4H"]


def run_window(df: pd.DataFrame, start: str, end: str, token: str,
               tf_label: str) -> list[dict]:
    """Run all strategies on a date-filtered slice. Returns list of result dicts."""
    df_window = df[(df.index >= start) & (df.index <= end)]
    if len(df_window) < 100:
        return []

    out = []
    for strat_name, strat_fn in STRATEGIES.items():
        try:
            sigs = strat_fn(df_window, enable_short=False)
            r = run_backtest(df_window, sigs, token, strat_name, tf_label,
                             risk=DEFAULT_RISK)
            out.append({
                "strategy": strat_name,
                "token": token,
                "tf": tf_label,
                "bars": len(df_window),
                "pf": r.profit_factor,
                "wr": r.win_rate,
                "n": r.trade_count,
                "dd": r.max_drawdown,
                "np_pct": r.net_profit,
            })
        except Exception as e:
            print(f"  err {strat_name}/{token}/{tf_label}: {e}")
    return out


def main():
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    out_path = f"backtesting/results/bull_period_{ts}.txt"
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 100)
    emit(f"BULL-PERIOD BACKTEST  ({datetime.utcnow():%Y-%m-%d %H:%M UTC})")
    emit("=" * 100)
    emit("Question: which strategy × token combos crushed during sustained bull windows?")
    emit("Method: run full strategy matrix on each bull window; compare to full-period baseline.")
    emit("Use: sizing overrides + alert deploy decisions for the next bull cycle.")
    emit("")

    # ── Fetch data once per TF ───────────────────────────────────────────────
    # Need ~3 years of history to cover 2023-Q4 window. fetch_all defaults to
    # 2000 bars; bump aggressively. Binance/Coinbase paginate; OKX caps at
    # ~2000 bars total (most historical).
    BARS_PER_TF = {
        "1H": 8000,   # ~333 days — covers 2024-Q4 window cleanly
        "4H": 8000,   # ~1333 days = ~3.65 years — covers both windows
    }

    all_data: dict = {}
    for tf in FOCUS_TFS:
        emit(f"\n--- Fetching {tf} data ({BARS_PER_TF[tf]} bars target) ---")
        data = fetch_all(timeframe=tf, bars=BARS_PER_TF[tf])
        for token, df in data.items():
            if df is None or len(df) < 200:
                continue
            all_data[(token, tf)] = df
        emit(f"  Loaded: {sum(1 for k in all_data if k[1] == tf)} tokens on {tf}")

    # ── Baseline: full-history results ───────────────────────────────────────
    emit("\n=== Baseline: full-period PF per combo (>=30 trades) ===\n")
    baseline: dict = {}
    for (token, tf_label), df in all_data.items():
        for strat_name, strat_fn in STRATEGIES.items():
            try:
                sigs = strat_fn(df, enable_short=False)
                r = run_backtest(df, sigs, token, strat_name, tf_label, risk=DEFAULT_RISK)
                baseline[(strat_name, token, tf_label)] = r
            except Exception:
                pass

    # ── Per-window analysis ──────────────────────────────────────────────────
    all_rows_by_window: dict = {}

    for window_label, start, end, narrative in BULL_WINDOWS:
        emit("")
        emit("=" * 100)
        emit(f"WINDOW: {window_label}  ({start} → {end})")
        emit(f"Narrative: {narrative}")
        emit("=" * 100)

        rows = []
        for (token, tf_label), df in all_data.items():
            rows.extend(run_window(df, start, end, token, tf_label))

        if not rows:
            emit("  (no data in this window)")
            continue

        # Annotate each row with baseline PF and the regime delta
        for r in rows:
            br = baseline.get((r["strategy"], r["token"], r["tf"]))
            r["base_pf"] = br.profit_factor if br else 0.0
            r["delta"] = r["pf"] - r["base_pf"]
            # ★ = thrived in window AND beat baseline by 30%+ (regime-conditional edge)
            # ✓ = thrived in window (PF≥1.4, N≥15) but no big baseline gap
            if r["n"] >= 15 and r["pf"] >= 1.4 and r["base_pf"] > 0 and r["pf"] > r["base_pf"] * 1.3:
                r["tag"] = "★"
            elif r["n"] >= 15 and r["pf"] >= 1.4:
                r["tag"] = "✓"
            else:
                r["tag"] = ""

        # Top 30 by window PF, with N>=15 floor
        rows_sorted = sorted([r for r in rows if r["n"] >= 15],
                             key=lambda r: -r["pf"])[:30]
        all_rows_by_window[window_label] = rows

        emit(f"\nTop 30 in window (PF desc, n>=15):")
        emit(f"  {'Strategy':<14} {'Token':<10} {'TF':<3}  {'Win-PF':>6} {'WR%':>5} {'N':>4}  "
             f"{'Base-PF':>7}  {'ΔPF':>6}  {'Win-NP%':>8}  Tag")
        emit("  " + "-" * 95)
        for r in rows_sorted:
            emit(f"  {r['strategy']:<14} {r['token']:<10} {r['tf']:<3}  "
                 f"{r['pf']:>6.2f} {r['wr']:>5.1f} {r['n']:>4}  "
                 f"{r['base_pf']:>7.2f}  {r['delta']:>+6.2f}  {r['np_pct']:>+7.1f}%  {r['tag']}")

    # ── Cross-window: combos that performed well in BOTH windows ─────────────
    emit("")
    emit("=" * 100)
    emit("CROSS-WINDOW WINNERS — combos that thrived in BOTH bull periods")
    emit("=" * 100)
    emit("(PF >= 1.4 AND n >= 15 in both windows)")
    emit("")

    if len(BULL_WINDOWS) >= 2:
        w1, w2 = BULL_WINDOWS[0][0], BULL_WINDOWS[1][0]
        rows1 = {(r["strategy"], r["token"], r["tf"]): r for r in all_rows_by_window.get(w1, [])
                 if r["n"] >= 15 and r["pf"] >= 1.4}
        rows2 = {(r["strategy"], r["token"], r["tf"]): r for r in all_rows_by_window.get(w2, [])
                 if r["n"] >= 15 and r["pf"] >= 1.4}

        cross_keys = sorted(rows1.keys() & rows2.keys(),
                            key=lambda k: -((rows1[k]["pf"] + rows2[k]["pf"]) / 2))

        if cross_keys:
            emit(f"  {'Strategy':<14} {'Token':<10} {'TF':<3}  {'W1-PF':>5} {'W2-PF':>5}  {'AvgPF':>6}  "
                 f"{'Base-PF':>7}  {'AvgΔ':>6}")
            emit("  " + "-" * 80)
            for k in cross_keys:
                r1, r2 = rows1[k], rows2[k]
                avg_pf = (r1["pf"] + r2["pf"]) / 2
                avg_delta = ((r1["pf"] - r1["base_pf"]) + (r2["pf"] - r2["base_pf"])) / 2
                emit(f"  {k[0]:<14} {k[1]:<10} {k[2]:<3}  "
                     f"{r1['pf']:>5.2f} {r2['pf']:>5.2f}  {avg_pf:>6.2f}  "
                     f"{r1['base_pf']:>7.2f}  {avg_delta:>+6.2f}")
        else:
            emit("  (no combos cleared the bar in both windows)")

    # ── Strategy-level rollup: which INDICATORS work best in bull markets? ───
    emit("")
    emit("=" * 100)
    emit("STRATEGY ROLLUP — bull-window edge by indicator family")
    emit("=" * 100)
    emit("")

    strat_stats: dict = {}
    for window_label, rows in all_rows_by_window.items():
        for r in rows:
            if r["n"] < 15:
                continue
            s = r["strategy"]
            strat_stats.setdefault(s, {"pfs": [], "deltas": [], "wins": 0, "tested": 0})
            strat_stats[s]["pfs"].append(r["pf"])
            strat_stats[s]["deltas"].append(r["delta"])
            strat_stats[s]["tested"] += 1
            if r["pf"] >= 1.4:
                strat_stats[s]["wins"] += 1

    emit(f"  {'Strategy':<14} {'Tested':>7} {'Win-rate':>9} {'AvgPF':>7} {'AvgΔ':>7} {'MaxPF':>7}")
    emit("  " + "-" * 60)
    for s, st in sorted(strat_stats.items(),
                        key=lambda kv: -(sum(kv[1]["pfs"]) / max(1, len(kv[1]["pfs"])))):
        n = len(st["pfs"])
        if n == 0:
            continue
        avg_pf = sum(st["pfs"]) / n
        avg_d = sum(st["deltas"]) / n
        max_pf = max(st["pfs"])
        win_rate = st["wins"] / max(1, st["tested"]) * 100
        emit(f"  {s:<14} {st['tested']:>7} {win_rate:>8.0f}% {avg_pf:>7.2f} {avg_d:>+7.2f} {max_pf:>7.2f}")

    emit("")
    emit("=" * 100)
    emit("LEGEND")
    emit("  ★ = window PF≥1.4 with n≥15 AND beat full-period baseline by 30%+")
    emit("  ✓ = window PF≥1.4 with n≥15 (good in window but not regime-dependent)")
    emit("  ΔPF = window_PF - baseline_PF (positive = bull edge above baseline)")
    emit("")
    emit("INTERPRETATION")
    emit("  CROSS-WINDOW WINNERS = combos to deploy first when next bull confirms.")
    emit("  STRATEGY ROLLUP = which indicator FAMILIES carry edge across bull regimes.")
    emit("  High AvgΔ = regime-dependent edge (latent in current sideways data).")
    emit("=" * 100)

    os.makedirs("backtesting/results", exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
