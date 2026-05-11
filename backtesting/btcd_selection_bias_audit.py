"""Selection-bias audit: do our actual BTCD trades hit the bucket-wide win rate?

The 2026-05-11 audit showed the 50-80% bucket has actual YES rate 76% (raw model
predicts 66%, underpredicts by 10pts). In theory, betting YES in that bucket
should be +EV. But our 92 BTCD trades lost ~$70 over 3 weeks. Possible causes:

  A. Selection bias inside the bucket — we systematically picked the worst
     tickers (e.g., always the cheapest YES on the most-OTM strike). Our actual
     win rate < bucket-wide actual.
  B. Bucket coverage — our trades didn't cluster where the bias was; bulk landed
     in a bucket with neutral or unfavorable actuals.
  C. Cost structure — slippage, half-spreads, settlement fees ate the edge.
  D. Sample-size noise — 92 trades is thin; realized P&L variance dominates.

This script answers it by joining our trades to per-ticker outcomes and
computing realized P&L bucket-by-bucket.
"""
from __future__ import annotations

import json
import re
import sqlite3
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "trades.db")
JSONL_PATH = os.path.join(ROOT, "data", "kalshi_strikes_calibration.jsonl")

SETTLEMENT_HOURS = 0.15  # match build_btcd_calibrator
FEES_PER_CONTRACT_CENTS = 0  # Kalshi: no per-contract trading fee; settlement-only


def bucket(p: float) -> str:
    if p < 0.05: return "0-5%"
    if p < 0.20: return "5-20%"
    if p < 0.50: return "20-50%"
    if p < 0.80: return "50-80%"
    if p < 0.95: return "80-95%"
    return "95-100%"


def derive_outcomes(path: str) -> dict[str, dict]:
    """For each ticker, find snapshot closest to settlement. Returns mapping
    ticker -> {outcome: 0|1, settle_spot, strike, hours_at_close}.
    """
    closest: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("ticker")
            if not t:
                continue
            cur = closest.get(t)
            if cur is None or e["hours"] < cur["hours"]:
                closest[t] = e
    out: dict[str, dict] = {}
    for t, e in closest.items():
        if e["hours"] <= SETTLEMENT_HOURS:
            out[t] = {
                "outcome": 1 if e["spot"] >= e["strike"] else 0,
                "settle_spot": e["spot"],
                "strike": e["strike"],
                "hours_at_close": e["hours"],
            }
    return out


_NOTE_FAIR_RE = re.compile(r"fair=([\d.]+)")
_NOTE_EDGE_RE = re.compile(r"edge=\+?(-?[\d.]+)c")


def load_trades(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT timestamp, ticker, action, count, price_cents, total_cost_cents, notes
           FROM kalshi_trades
           WHERE ticker LIKE 'KXBTCD%'
             AND action = 'buy'
           ORDER BY timestamp"""
    ).fetchall()
    conn.close()
    trades = []
    for r in rows:
        d = dict(r)
        m = _NOTE_FAIR_RE.search(d.get("notes") or "")
        d["fair_prob"] = float(m.group(1)) if m else None
        m2 = _NOTE_EDGE_RE.search(d.get("notes") or "")
        d["edge_cents"] = float(m2.group(1)) if m2 else None
        trades.append(d)
    return trades


def main():
    print("Loading per-ticker outcomes from calibration jsonl…")
    outcomes = derive_outcomes(JSONL_PATH)
    print(f"  {len(outcomes)} tickers with derived outcomes")
    print()

    print("Loading our BTCD trades from kalshi_trades…")
    trades = load_trades(DB_PATH)
    print(f"  {len(trades)} BTCD BUY trades total")
    print()

    # Join: each trade needs ticker outcome to compute realized P&L
    joined = []
    missing_outcome = 0
    missing_fair = 0
    for t in trades:
        out = outcomes.get(t["ticker"])
        if not out:
            missing_outcome += 1
            continue
        if t["fair_prob"] is None:
            missing_fair += 1
            continue
        # YES contract: pays $1 if outcome=1, else $0. Cost was price_cents per contract.
        payout_cents = 100 * t["count"] if out["outcome"] == 1 else 0
        realized_cents = payout_cents - (t["total_cost_cents"] or 0)
        joined.append({**t, **out, "payout_cents": payout_cents, "realized_cents": realized_cents})

    print(f"Joined: {len(joined)} trades (skipped {missing_outcome} unresolved tickers, "
          f"{missing_fair} missing fair_prob)")
    print()

    if not joined:
        print("ERROR: nothing to analyze.", file=sys.stderr)
        return 1

    # Aggregate by bucket of LOGGED fair_prob (the bot's prediction at trade time)
    agg = defaultdict(lambda: {
        "n": 0,
        "sum_pred": 0.0,
        "wins": 0,
        "sum_cost": 0,
        "sum_payout": 0,
        "sum_realized": 0,
    })
    for j in joined:
        bk = bucket(j["fair_prob"])
        a = agg[bk]
        a["n"] += 1
        a["sum_pred"] += j["fair_prob"]
        a["wins"] += j["outcome"]
        a["sum_cost"] += j["total_cost_cents"] or 0
        a["sum_payout"] += j["payout_cents"]
        a["sum_realized"] += j["realized_cents"]

    # Reference: audit's bucket actuals from the full 871K population
    AUDIT_ACTUALS = {
        "0-5%":   0.001,
        "5-20%":  0.027,
        "20-50%": 0.263,
        "50-80%": 0.760,
        "80-95%": 0.964,
        "95-100%": 1.000,
    }

    print("=" * 100)
    print("Selection-bias check: OUR trades' win rate vs audit's bucket-wide actuals")
    print("=" * 100)
    fmt = "  {:>8} {:>6} {:>8} {:>10} {:>14} {:>11} {:>10} {:>11} {:>10}"
    print(fmt.format("bucket", "n", "ourpred", "ourwinrate", "audit_actual", "delta_pp",
                     "totcost_$", "totpayout_$", "realized_$"))
    total_n = total_cost = total_payout = total_realized = 0
    for bk in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        a = agg.get(bk)
        if not a or a["n"] == 0:
            continue
        n = a["n"]
        pred = a["sum_pred"] / n
        win_rate = a["wins"] / n
        audit = AUDIT_ACTUALS.get(bk, 0.0)
        delta_pp = (win_rate - audit) * 100
        print(fmt.format(
            bk, n, f"{pred:.3f}", f"{win_rate:.3f}", f"{audit:.3f}",
            f"{delta_pp:+.1f}",
            f"{a['sum_cost']/100:.2f}", f"{a['sum_payout']/100:.2f}", f"{a['sum_realized']/100:+.2f}",
        ))
        total_n += n
        total_cost += a["sum_cost"]
        total_payout += a["sum_payout"]
        total_realized += a["sum_realized"]
    print(fmt.format("TOTAL", total_n, "", "", "", "",
                     f"{total_cost/100:.2f}", f"{total_payout/100:.2f}", f"{total_realized/100:+.2f}"))
    print()

    # Now the same analysis but applying the empirical calibrator. If the bot
    # had been using the calibrator from day one, which trades would it have
    # taken?  Filter by calibrated_prob >= min_fair_prob (0.60), same edge rule.
    print("=" * 100)
    print("Counterfactual: how would the calibrator have changed trade selection?")
    print("=" * 100)
    try:
        sys.path.insert(0, ROOT)
        from app.services.btcd_calibrator import get_btcd_calibrator
        cal = get_btcd_calibrator()
    except Exception as e:
        print(f"  Calibrator unavailable: {e}", file=sys.stderr)
        return 0

    # Bot's live gates (from config 2026-05-11): min_fair_prob=0.60, min_edge=8c
    MIN_FAIR_PROB = 0.60
    MIN_EDGE_C = 8

    kept = []
    dropped = []
    for j in joined:
        raw = j["fair_prob"]  # this is what the bot logged — already raw/parametric
        adj = cal.apply(raw)
        # Edge with calibrated prob vs yes_ask
        adj_edge = adj * 100 - (j["price_cents"] or 0)
        j2 = {**j, "raw_prob": raw, "adj_prob": adj, "adj_edge": adj_edge}
        if adj >= MIN_FAIR_PROB and adj_edge >= MIN_EDGE_C:
            kept.append(j2)
        else:
            dropped.append(j2)

    print(f"Of {len(joined)} historic trades, the calibrator+gates would have:")
    print(f"  KEPT {len(kept)} trades (calibrated prob >= {MIN_FAIR_PROB}, adj_edge >= {MIN_EDGE_C}c)")
    print(f"  DROPPED {len(dropped)} trades (gate fail)")
    print()

    def block_stats(rows, label):
        if not rows:
            print(f"  {label}: (none)")
            return
        n = len(rows)
        cost = sum(r["total_cost_cents"] or 0 for r in rows)
        pay = sum(r["payout_cents"] for r in rows)
        real = sum(r["realized_cents"] for r in rows)
        wins = sum(r["outcome"] for r in rows)
        print(f"  {label}: n={n}  cost=${cost/100:.2f}  payout=${pay/100:.2f}  "
              f"realized=${real/100:+.2f}  winrate={wins/n:.3f}")

    block_stats(kept, "KEPT subset (counterfactual)")
    block_stats(dropped, "DROPPED subset (avoided losses)")
    block_stats(joined, "ALL trades (actual history)")
    print()

    # Show per-trade detail for KEPT (small enough to print)
    print("=" * 100)
    print("KEPT trades — counterfactual portfolio with calibrator on")
    print("=" * 100)
    print(f"  {'timestamp':>27} {'ticker':>36} {'raw':>6} {'adj':>6} {'ask':>4} {'out':>4} {'P&L¢':>7}")
    for k in kept[:40]:  # first 40 if many
        ts = k["timestamp"][:19]
        print(f"  {ts:>27} {k['ticker']:>36} {k['raw_prob']:>6.3f} {k['adj_prob']:>6.3f} "
              f"{k['price_cents']:>4} {k['outcome']:>4} {k['realized_cents']:>+7}")
    if len(kept) > 40:
        print(f"  … and {len(kept)-40} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
