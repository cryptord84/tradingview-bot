#!/usr/bin/env python3
"""Generate deploy-shortlist from latest nightly backtest run.

Lists strategies passing at Lev >= threshold, optionally cross-referenced
against a TradingView alert_list dump to flag deployed vs missing.

Usage:
    venv/bin/python backtesting/deploy_shortlist.py
    venv/bin/python backtesting/deploy_shortlist.py --min-lev 2.0
    venv/bin/python backtesting/deploy_shortlist.py --alerts-json /path/to/alert_list.json
    venv/bin/python backtesting/deploy_shortlist.py --run-id nightly_20260418_0403
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import DB_PATH


# Maps strategy name in backtest DB -> TradingView pine_id of deployed indicator
STRATEGY_TO_PINE_ID = {
    "Donchian":   "USER;6a0a490366d34845bed8071a79198cde",
    "EMA Ribbon": "USER;f060080f798d46efa6ee90ea4356190a",
    "FVG":        "USER;4852215f50f54cbdad7d6ae82fb4ff07",
    "Liq Sweep":  "USER;12e465c59f0941d2a4fef70e58003c45",
    "Stoch RSI":  "USER;fea633ae4e5a488c8ccea5efd448b93a",
    "VWAP Dev":   "USER;53163d00de3843f1a78c67bfc88dbf6d",
    "Mean Rev":   {"1H": "USER;182f490a5e1c445b8c26eb9d65d8d0a6",
                   "4H": "USER;14e672be192545cd86f9d45690a70592"},
    "RSI Div":    {"1H": "USER;12775068023e47aeb862df68f5f005db"},
    # Supertrend and MACD Vol have no deployed Pine indicator
}


def _tf_to_resolution(tf: str) -> str:
    """Map backtest TF label to TV resolution string."""
    return {"15m": "15", "1H": "60", "4H": "240"}.get(tf, tf)


def _symbol_matches(alert_symbol: str, bt_token: str) -> bool:
    """TV uses 'BINANCE:BONKUSDT'; backtest uses 'BONK'. Match on ticker."""
    return bt_token.upper() in alert_symbol.upper().split(":")[-1]


def load_deployed_set(alerts_json_path: str) -> set:
    """Return set of (pine_id, symbol_ticker, resolution) for active alerts."""
    with open(alerts_json_path) as f:
        data = json.load(f)
    deployed = set()
    for a in data.get("alerts", []):
        if not a.get("active"):
            continue
        pid = a["condition"]["series"][0]["pine_id"]
        sym = a["symbol"].split(":")[-1]  # strip exchange prefix
        res = a["resolution"]
        deployed.add((pid, sym.upper(), res))
    return deployed


def is_deployed(strategy: str, token: str, tf: str, deployed_set: set) -> bool:
    """Check if a (strategy, token, tf) combo has an active TV alert."""
    pid_entry = STRATEGY_TO_PINE_ID.get(strategy)
    if not pid_entry:
        return False  # no Pine indicator for this strategy
    pid = pid_entry[tf] if isinstance(pid_entry, dict) else pid_entry
    if pid is None:
        return False
    res = _tf_to_resolution(tf)
    token_upper = token.upper()
    for d_pid, d_sym, d_res in deployed_set:
        if d_pid != pid or d_res != res:
            continue
        if token_upper in d_sym or d_sym.startswith(token_upper):
            return True
    return False


def get_latest_run_id() -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT run_id FROM backtests WHERE run_id LIKE 'nightly_%' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No nightly runs found in DB")
    return row[0]


def fetch_passers(run_id: str, min_lev: float) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT strategy_name, symbol, timeframe, profit_factor, win_rate,
                  total_trades, max_drawdown, sharpe_ratio, net_profit_pct,
                  suggested_leverage
           FROM backtests
           WHERE run_id = ? AND status = 'pass' AND suggested_leverage >= ?
           ORDER BY suggested_leverage DESC, profit_factor DESC""",
        (run_id, min_lev),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Deploy-shortlist from backtest run")
    parser.add_argument("--run-id", help="Run ID (default: latest nightly)")
    parser.add_argument("--min-lev", type=float, default=2.0,
                        help="Minimum suggested leverage (default: 2.0)")
    parser.add_argument("--alerts-json",
                        help="Path to TradingView alert_list JSON dump; "
                             "if provided, flags deployed vs missing")
    args = parser.parse_args()

    run_id = args.run_id or get_latest_run_id()
    passers = fetch_passers(run_id, args.min_lev)

    deployed_set = set()
    if args.alerts_json:
        deployed_set = load_deployed_set(args.alerts_json)

    print(f"\n{'='*90}")
    print(f"  DEPLOY SHORTLIST — {run_id}  |  Lev >= {args.min_lev}  |  {len(passers)} passers")
    if args.alerts_json:
        print(f"  Cross-referenced against {len(deployed_set)} active TV alerts")
    print(f"{'='*90}\n")

    header = f"  {'Strategy':<12} {'Token':<9} {'TF':<4} {'PF':>6} {'WR%':>6} " \
             f"{'Trd':>4} {'DD%':>6} {'NP%':>7} {'Shrp':>5} {'Lev':>5}"
    if args.alerts_json:
        header += f"  {'Status':<10}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    missing = []
    for r in passers:
        line = (f"  {r['strategy_name']:<12} {r['symbol']:<9} {r['timeframe']:<4} "
                f"{r['profit_factor']:>6.2f} {r['win_rate']:>5.1f}% "
                f"{r['total_trades']:>4} {r['max_drawdown']:>5.1f}% "
                f"{r['net_profit_pct']:>6.1f}% {r['sharpe_ratio']:>5.2f} "
                f"{r['suggested_leverage']:>4.1f}x")
        if args.alerts_json:
            if STRATEGY_TO_PINE_ID.get(r["strategy_name"]) is None:
                status = "NO PINE"
            elif is_deployed(r["strategy_name"], r["symbol"], r["timeframe"], deployed_set):
                status = "deployed"
            else:
                status = "🔴 MISSING"
                missing.append(r)
            line += f"  {status:<10}"
        print(line)

    if args.alerts_json and missing:
        print(f"\n{'='*90}")
        print(f"  {len(missing)} MISSING — candidates to deploy as TV alerts:")
        print(f"{'='*90}")
        for r in missing:
            print(f"    {r['strategy_name']:<12} {r['symbol']:<9} {r['timeframe']:<4} "
                  f"PF={r['profit_factor']:.2f} Lev={r['suggested_leverage']:.1f}x "
                  f"NP={r['net_profit_pct']:.1f}%")


if __name__ == "__main__":
    main()
