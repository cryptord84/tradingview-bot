"""TP/SL audit — measure money left on the table from TPs hit too tight,
trail stops hit on noise, etc.

For each closed position in the last N days, fetch the 4H OHLC for the
holding window and compute:

  MFE (max favorable excursion) = highest price reached during holding period
                                  (long-only, per current strategy roster)
  realized exit = actual exit_price
  efficiency_pct = (exit - entry) / (MFE - entry)  — 100% means caught the peak
  $ left on table = (MFE - exit) / entry * amount_usdc

Aggregate by strategy + status to identify which combos systematically clip
their winners early. Suggest TP multiplier adjustments where impact > $5/trade.
"""
from __future__ import annotations

import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional

from backtesting.data import fetch_binance


DB_PATH = "data/trades.db"
LOOKBACK_DAYS = 14


def fetch_ohlc_for_position(symbol: str, opened_iso: str, closed_iso: str):
    """Fetch 4H OHLC bars covering the position's holding window.

    Returns a list of bars (each with high/low/close/timestamp) within the
    [opened, closed+1bar] window, or None if data fetch failed.
    """
    try:
        opened = datetime.fromisoformat(opened_iso.replace("Z", "")).replace(tzinfo=timezone.utc)
        closed = datetime.fromisoformat(closed_iso.replace("Z", "")).replace(tzinfo=timezone.utc)
    except Exception:
        return None

    # Strip exchange prefix and .P suffix to get pair
    pair = symbol.replace("USDT", "USDT").replace(".P", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    df = fetch_binance(pair, "4h", bars=2000)
    if df is None or len(df) == 0:
        return None
    # df.index is timestamp. Filter to holding window with 1-bar pad on each side.
    pad = timedelta(hours=4)
    mask = (df.index >= opened - pad) & (df.index <= closed + pad)
    return df[mask]


def audit():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = conn.execute("""
        SELECT * FROM positions
        WHERE created_at >= ? AND status LIKE 'closed%'
          AND direction = 'long'
        ORDER BY created_at DESC
    """, (cutoff,)).fetchall()
    conn.close()

    print(f"\n{'='*100}")
    print(f"  TP/SL AUDIT — {LOOKBACK_DAYS}-day window  ({datetime.utcnow():%Y-%m-%d %H:%M UTC})")
    print(f"  {len(rows)} closed long positions analyzed")
    print('='*100)

    by_status = defaultdict(list)
    by_strat_status = defaultdict(list)

    for r in rows:
        if not (r["created_at"] and r["closed_at"] and r["entry_price"] and r["exit_price"]):
            continue

        bars = fetch_ohlc_for_position(r["symbol"], r["created_at"], r["closed_at"])
        if bars is None or len(bars) == 0:
            continue

        entry = r["entry_price"]
        exit_p = r["exit_price"]
        amount = r["amount_usdc"] or 0
        mfe = float(bars["high"].max())  # long-only
        mae = float(bars["low"].min())   # max adverse excursion (lowest dip)

        # Calculations
        mfe_pct = (mfe - entry) / entry * 100
        realized_pct = (exit_p - entry) / entry * 100
        # Efficiency: how much of the favorable move did we capture?
        # Negative if exit was below entry (a loss, regardless of MFE).
        if mfe > entry:
            efficiency = (exit_p - entry) / (mfe - entry) * 100
        else:
            efficiency = 0  # never went above entry

        money_left = max(0, (mfe - exit_p) / entry * amount) if mfe > exit_p else 0

        record = {
            "id": r["id"], "symbol": r["symbol"], "strategy": r["strategy"] or "?",
            "status": r["status"], "amount": amount,
            "entry": entry, "exit": exit_p, "mfe": mfe, "mae": mae,
            "mfe_pct": mfe_pct, "realized_pct": realized_pct,
            "mae_pct": (mae - entry) / entry * 100,
            "efficiency": efficiency, "money_left": money_left,
            "tp": r["tp_price"], "sl": r["sl_price"],
            "atr": r["atr"], "bars": len(bars),
        }
        by_status[r["status"]].append(record)
        by_strat_status[(r["strategy"] or "?", r["status"])].append(record)

    # ── Per-status summary ─────────────────────────────────────────────────
    print(f"\n{'Status':<16} {'N':>3} {'Avg MFE%':>9} {'Avg Real%':>10} {'Avg Eff%':>9} "
          f"{'Total $left':>12} {'Avg $left':>10}")
    print("  " + "-" * 80)
    for status, lst in sorted(by_status.items(), key=lambda kv: -len(kv[1])):
        n = len(lst)
        if n == 0: continue
        avg_mfe = sum(p["mfe_pct"] for p in lst) / n
        avg_real = sum(p["realized_pct"] for p in lst) / n
        avg_eff = sum(p["efficiency"] for p in lst) / n
        total_left = sum(p["money_left"] for p in lst)
        avg_left = total_left / n
        print(f"  {status:<16} {n:>3} {avg_mfe:>+8.2f}% {avg_real:>+9.2f}% "
              f"{avg_eff:>+8.0f}% {total_left:>+11.2f} {avg_left:>+9.2f}")

    # ── Per-strategy × status ──────────────────────────────────────────────
    print(f"\n{'='*100}")
    print("  By strategy × status (n>=2)")
    print('='*100)
    print(f"  {'Strategy':<28} {'Status':<14} {'N':>3} {'AvgMFE%':>8} {'Real%':>7} "
          f"{'Eff%':>6} {'$left':>9}")
    print("  " + "-" * 95)
    for (strat, status), lst in sorted(by_strat_status.items(),
                                        key=lambda kv: -sum(p["money_left"] for p in kv[1])):
        n = len(lst)
        if n < 2: continue
        avg_mfe = sum(p["mfe_pct"] for p in lst) / n
        avg_real = sum(p["realized_pct"] for p in lst) / n
        avg_eff = sum(p["efficiency"] for p in lst) / n
        total_left = sum(p["money_left"] for p in lst)
        print(f"  {strat[:27]:<28} {status:<14} {n:>3} {avg_mfe:>+7.2f}% {avg_real:>+6.2f}% "
              f"{avg_eff:>+5.0f}% ${total_left:>+8.2f}")

    # ── Worst offenders (top 10 most $ left on table) ──────────────────────
    print(f"\n{'='*100}")
    print("  Top 10 individual positions — most $ left on table")
    print('='*100)
    all_positions = [p for lst in by_status.values() for p in lst]
    worst = sorted(all_positions, key=lambda p: -p["money_left"])[:10]
    print(f"  {'ID':<5} {'Symbol':<14} {'Strategy':<22} {'Status':<14} "
          f"{'Entry':<10} {'Exit':<10} {'MFE':<10} {'$Left':>8} {'Eff%':>6}")
    print("  " + "-" * 110)
    for p in worst:
        print(f"  {p['id']:<5} {p['symbol']:<14} {p['strategy'][:21]:<22} {p['status']:<14} "
              f"{p['entry']:<10.6f} {p['exit']:<10.6f} {p['mfe']:<10.6f} ${p['money_left']:>+7.2f} {p['efficiency']:>+5.0f}%")

    # ── Interpretation ─────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print("  INTERPRETATION")
    print('='*100)
    print("""
  Efficiency = % of the favorable move captured before exit.
    100% = caught the peak    50% = exited at half the move      < 50% = TP/trail too tight
  $ left on table = (MFE - exit) × position size / entry. The "could have been" gain.

  ACTIONABLE PATTERNS:
    • If closed_tp efficiency averages < 60% → TPs are too tight; widen TP multiplier.
    • If closed_trail efficiency < 70% → trailing stop too aggressive (clipping winners).
    • If closed_sl mae_pct close to sl_pct → SL is set fine, just stops getting hit.
    • If closed_sl mae much smaller than sl distance → SL too wide, or signals are weak.
""")


if __name__ == "__main__":
    audit()
