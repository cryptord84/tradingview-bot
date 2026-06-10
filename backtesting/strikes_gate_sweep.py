"""Gate-sensitivity sweep for kalshi crypto_strikes (NO side).

Replays the accumulated scoring log against candidate gate thresholds and
reports, per config: how many NO trades would fire and their realized P&L
(reconstructed settlement). Goal: loosen the gate enough for some activity
WITHOUT re-creating losing selection.

NO-side economics (mirrors kalshi_crypto_strikes_bot.py:218-225):
  no_edge_cents = yes_bid - fair_prob*100
  no_cost_cents = 100 - yes_bid           # taker fill at the NO ask
  NO wins (pays 100c) when outcome == 0   # i.e. spot < strike at settle
  realized_per_contract = (100 if outcome==0 else 0) - no_cost

Outcome derivation copied from btcd_selection_bias_audit.derive_outcomes:
  per ticker, snapshot closest to settle; outcome=1 if spot>=strike.
"""
from __future__ import annotations
import json, os, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSONL = os.path.join(ROOT, "data", "kalshi_strikes_calibration.jsonl")
SETTLE_HOURS = 0.15
ERA_MIN = "2026-05-11"   # calibrator-era only (logged fair_prob is calibrated)

# Per-ticker: outcome candidate (min hours) + best NO entry candidate (max no_edge
# within tradeable window, loose pre-filter so the sweep range is covered).
oc = {}        # ticker -> (hours, spot, strike)
entry = {}     # ticker -> dict(ts, fair, yes_bid, hours, no_edge, series)

n_lines = 0
with open(JSONL) as f:
    for line in f:
        n_lines += 1
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("ticker")
        if not t:
            continue
        h = e.get("hours")
        if h is None:
            continue
        # outcome candidate (any era — need settlement)
        c = oc.get(t)
        if c is None or h < c[0]:
            oc[t] = (h, e.get("spot"), e.get("strike"))
        # entry candidate — calibrator era, tradeable window, loose pre-filter
        ts = e.get("ts", "")
        if ts < ERA_MIN:
            continue
        if not (0 < h <= 48):
            continue
        fair = e.get("fair_prob"); yb = e.get("yes_bid")
        if fair is None or yb is None:
            continue
        if fair > 0.60 or not (5 <= yb <= 60):   # loose envelope around all swept configs
            continue
        no_edge = yb - fair * 100.0
        cur = entry.get(t)
        if cur is None or no_edge > cur["no_edge"]:
            entry[t] = {"ts": ts, "fair": fair, "yes_bid": yb, "hours": h,
                        "no_edge": no_edge, "series": e.get("series")}

# derive outcomes
outcome = {}
for t, (h, spot, strike) in oc.items():
    if h <= SETTLE_HOURS and spot is not None and strike is not None:
        outcome[t] = 1 if spot >= strike else 0

# build trade candidates (need entry + settled outcome)
cands = []
for t, en in entry.items():
    if t in outcome:
        cands.append({**en, "ticker": t, "outcome": outcome[t]})

print(f"jsonl lines={n_lines:,}  tickers={len(oc):,}  settled={len(outcome):,}  "
      f"calibrator-era entry candidates w/ outcome={len(cands):,}")
print()

def evaluate(min_edge, min_fair_prob, yb_lo, yb_hi):
    sel = [c for c in cands
           if c["no_edge"] >= min_edge
           and c["fair"] <= (1.0 - min_fair_prob)
           and yb_lo <= c["yes_bid"] <= yb_hi]
    n = len(sel)
    if n == 0:
        return (n, 0, 0.0, 0.0, 0.0)
    pnl = 0.0
    wins = 0
    for c in sel:
        cost = 100 - c["yes_bid"]
        pay = 100 if c["outcome"] == 0 else 0
        pnl += (pay - cost)
        wins += 1 if c["outcome"] == 0 else 0
    return (n, wins, wins / n, pnl, pnl / n)

CONFIGS = [
    ("CURRENT  (edge5 fair0.50 bid15-50)", 5, 0.50, 15, 50),
    ("loosen-A (edge3 fair0.50 bid15-50)", 3, 0.50, 15, 50),
    ("loosen-B (edge5 fair0.45 bid12-55)", 5, 0.45, 12, 55),
    ("loosen-C (edge3 fair0.45 bid12-55)", 3, 0.45, 12, 55),
    ("loosen-D (edge2 fair0.40 bid10-60)", 2, 0.40, 10, 60),
    ("loosen-E (edge3 fair0.40 bid10-55)", 3, 0.40, 10, 55),
]
hdr = "  {:38} {:>6} {:>6} {:>8} {:>12} {:>11}"
print(hdr.format("config", "trades", "wins", "winrate", "tot_pnl_1c$", "avg_pnl_1c¢"))
for name, me, mfp, lo, hi in CONFIGS:
    n, wins, wr, pnl, avg = evaluate(me, mfp, lo, hi)
    print(hdr.format(name, n, wins, f"{wr:.3f}" if n else "-",
                     f"{pnl/100:+.2f}" if n else "-",
                     f"{avg:+.2f}" if n else "-"))
print()
print("Notes: P&L is at 1 contract/trade; live sizing is ~3-5 contracts (max_cost 300c).")
print("'trades' counts distinct settled tickers; real bot dedups + caps max_open_positions.")
