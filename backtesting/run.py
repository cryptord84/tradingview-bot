#!/usr/bin/env python3
"""
Backtest runner — runs all strategies across all tokens and timeframes.

Usage:
    venv/bin/python backtesting/run.py
    venv/bin/python backtesting/run.py --tf 1H
    venv/bin/python backtesting/run.py --tf 4H --strategy Supertrend
    venv/bin/python backtesting/run.py --bars 2000
"""

import argparse
import csv
import os
import sys
from datetime import datetime

# Allow running from project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtesting.data import fetch_all, TIMEFRAMES
from backtesting.engine import run_backtest, BacktestResult, THRESHOLDS, RiskConfig, DEFAULT_RISK, LEGACY_RISK
from backtesting.strategies import STRATEGIES

# ── Column widths for pretty-print ───────────────────────────────────────────
COLS = [
    ("Token",    6),
    ("PF",       5),
    ("WR%",      6),
    ("Trades",   7),
    ("MaxDD%",   7),
    ("NetPft%",  8),
    ("Sharpe",   6),
    ("AvgRR",    5),
    ("Status",   28),
]


def _row(result: BacktestResult) -> str:
    d = result.summary_row()
    parts = [d["Token"].ljust(6), d["PF"].rjust(5), d["WR%"].rjust(6),
             d["Trades"].rjust(7), d["MaxDD%"].rjust(7), d["NetPft%"].rjust(8),
             d["Sharpe"].rjust(6), d["AvgRR"].rjust(5),
             "  " + d["Status"]]
    return "  ".join(parts)


def _header() -> str:
    heads = [c.ljust(w) for c, w in COLS]
    return "  ".join(heads)


def _sep() -> str:
    return "─" * 82


def save_csv(results: list[BacktestResult], path: str):
    fieldnames = ["Token", "Strategy", "TF", "PF", "WR%", "Trades", "MaxDD%", "NetPft%", "Sharpe", "AvgRR", "Status"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r.summary_row())


def run(timeframes: list[str], strategy_names: list[str], bars: int,
        risk: RiskConfig = DEFAULT_RISK):
    all_results: list[BacktestResult] = []

    for tf in timeframes:
        print(f"\nFetching {tf} data…")
        ohlcv_map = fetch_all(tf, bars=bars)

        if not ohlcv_map:
            print(f"  No data fetched for {tf}, skipping.")
            continue

        for strat_name in strategy_names:
            strat_fn = STRATEGIES[strat_name]
            print(f"\n{'═'*82}")
            print(f"  {strat_name} — {tf}")
            print(f"{'═'*82}")
            print(f"  {_header()}")
            print(f"  {_sep()}")

            strat_results = []
            for token, df in ohlcv_map.items():
                try:
                    signals = strat_fn(df)
                    result = run_backtest(df, signals, token, strat_name, tf, risk=risk)
                except Exception as e:
                    print(f"  {token:<6}  ERROR: {e}")
                    continue

                flag = " ✓" if result.passed else ""
                print(f"  {_row(result)}{flag}")
                strat_results.append(result)
                all_results.append(result)

            # Summary for this strategy/timeframe
            passed = [r for r in strat_results if r.passed]
            print(f"  {_sep()}")
            print(f"  {len(passed)}/{len(strat_results)} tokens passed thresholds")
            if passed:
                print(f"  Passing: {', '.join(r.token for r in passed)}")

    # Save combined CSV
    if all_results:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out_dir = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, f"backtest_{ts}.csv")
        save_csv(all_results, csv_path)
        print(f"\nResults saved to: {csv_path}")

    # Final pass/fail summary across everything
    print(f"\n{'═'*82}")
    print("  FINAL PASS SUMMARY")
    print(f"{'═'*82}")
    passing = [r for r in all_results if r.passed]
    if passing:
        for r in sorted(passing, key=lambda x: x.profit_factor, reverse=True):
            print(f"  {r.strategy:<14} {r.token:<6} {r.timeframe}  "
                  f"PF={r.profit_factor}  WR={r.win_rate}%  "
                  f"Trades={r.trade_count}  DD={r.max_drawdown}%  NP={r.net_profit}%  "
                  f"Sharpe={r.sharpe_ratio}  RR={r.avg_rr}")
    else:
        print("  No strategies passed all thresholds.")

    print(f"\nThresholds: PF>{THRESHOLDS['profit_factor']}  "
          f"WR>{THRESHOLDS['win_rate']}%  "
          f"Trades≥{THRESHOLDS['trade_count']}  "
          f"MaxDD<{THRESHOLDS['max_drawdown']}%  "
          f"NP>{THRESHOLDS['net_profit']}%")

    return all_results


def main():
    parser = argparse.ArgumentParser(description="Run strategy backtests")
    parser.add_argument("--tf", choices=list(TIMEFRAMES.keys()) + ["ALL"], default="ALL",
                        help="Timeframe to test (default: ALL)")
    parser.add_argument("--strategy", choices=list(STRATEGIES.keys()) + ["ALL"], default="ALL",
                        help="Strategy to run (default: ALL)")
    parser.add_argument("--bars", type=int, default=1000,
                        help="Number of bars to fetch per symbol (default: 1000)")
    parser.add_argument("--legacy", action="store_true",
                        help="Use legacy mode (100%% equity, no SL/TP) for comparison")
    parser.add_argument("--risk-pct", type=float, default=2.0,
                        help="Risk per trade as %% of equity (default: 2.0)")
    parser.add_argument("--sl-mult", type=float, default=1.5,
                        help="ATR stop-loss multiplier (default: 1.5)")
    parser.add_argument("--tp-mult", type=float, default=3.0,
                        help="ATR take-profit multiplier (default: 3.0)")
    args = parser.parse_args()

    tfs = list(TIMEFRAMES.keys()) if args.tf == "ALL" else [args.tf]
    strats = list(STRATEGIES.keys()) if args.strategy == "ALL" else [args.strategy]

    if args.legacy:
        risk = LEGACY_RISK
        mode = "LEGACY (100% equity, no SL/TP)"
    else:
        risk = RiskConfig(
            risk_per_trade_pct=args.risk_pct,
            atr_sl_mult=args.sl_mult,
            atr_tp_mult=args.tp_mult,
        )
        mode = f"Risk: {risk.risk_per_trade_pct}%/trade, SL: {risk.atr_sl_mult}x ATR, TP: {risk.atr_tp_mult}x ATR, Trail: ON"

    print(f"Running: {strats} on {tfs} with {args.bars} bars per symbol")
    print(f"Mode: {mode}")
    run(tfs, strats, args.bars, risk=risk)


if __name__ == "__main__":
    main()
