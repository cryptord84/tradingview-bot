"""BTCD Brier-score audit — pre-hotfix vs post-hotfix calibration.

Reads kalshi_strikes_calibration.jsonl, derives outcomes from the
closest-to-settlement spot per ticker, computes Brier score and reliability
buckets segmented by time period and prediction probability.

Hotfix landed 2026-04-24 (per memory project_btcd_hotfix_optionA).
"""
import json
from collections import defaultdict
from datetime import datetime

JSONL_PATH = "data/kalshi_strikes_calibration.jsonl"
HOTFIX_TS = "2026-04-24T00:00:00"
SETTLEMENT_HOURS = 0.15  # within ~9 min of settlement = treat spot as resolution


def load_entries(path: str):
    with open(path) as f:
        for line in f:
            yield json.loads(line)


def derive_outcomes(entries_by_ticker: dict) -> dict:
    """For each ticker, find smallest-hours entry. If close enough to settlement,
    outcome = 1 if spot >= strike else 0."""
    outcomes = {}
    for ticker, entries in entries_by_ticker.items():
        entries.sort(key=lambda e: e["hours"])
        first = entries[0]
        if first["hours"] <= SETTLEMENT_HOURS:
            outcomes[ticker] = 1 if first["spot"] >= first["strike"] else 0
    return outcomes


def horizon_bucket(h: float) -> str:
    if h < 1: return "<1h"
    if h < 4: return "1-4h"
    if h < 12: return "4-12h"
    if h < 24: return "12-24h"
    return "24h+"


def prob_bucket(p: float) -> str:
    if p < 0.05: return "0-5%"
    if p < 0.20: return "5-20%"
    if p < 0.50: return "20-50%"
    if p < 0.80: return "50-80%"
    if p < 0.95: return "80-95%"
    return "95-100%"


def main():
    print("Loading JSONL…")
    by_ticker = defaultdict(list)
    for e in load_entries(JSONL_PATH):
        by_ticker[e["ticker"]].append(e)
    print(f"  {len(by_ticker)} unique tickers, {sum(len(v) for v in by_ticker.values())} rows")

    outcomes = derive_outcomes(by_ticker)
    print(f"  {len(outcomes)} tickers have settlement outcomes (hours <= {SETTLEMENT_HOURS})")

    # Drop the resolution snapshots themselves (we want predictions, not the answer)
    # Bucket by (period, prob, horizon)
    buckets = defaultdict(lambda: {"n": 0, "brier": 0.0, "pred": 0.0, "actual": 0.0})
    overall = defaultdict(lambda: {"n": 0, "brier": 0.0, "pred": 0.0, "actual": 0.0})

    skipped_no_outcome = 0
    skipped_too_close = 0
    used = 0

    for ticker, entries in by_ticker.items():
        if ticker not in outcomes:
            skipped_no_outcome += len(entries)
            continue
        outcome = outcomes[ticker]
        for e in entries:
            if e["hours"] <= SETTLEMENT_HOURS:
                skipped_too_close += 1
                continue  # this is the resolution observation, not a prediction
            p = e["fair_prob"]
            brier = (p - outcome) ** 2
            period = "post-hotfix" if e["ts"] >= HOTFIX_TS else "pre-hotfix"
            pb = prob_bucket(p)
            hb = horizon_bucket(e["hours"])

            for key in [(period, pb, hb), (period, pb, "ALL"), (period, "ALL", hb), (period, "ALL", "ALL")]:
                b = buckets[key]
                b["n"] += 1
                b["brier"] += brier
                b["pred"] += p
                b["actual"] += outcome
            used += 1

    print(f"\nUsed {used} predictions across {len(outcomes)} resolved tickers")
    print(f"  Skipped {skipped_no_outcome} (ticker not yet resolved)")
    print(f"  Skipped {skipped_too_close} resolution-snapshots\n")

    # Print summary tables
    for period in ["pre-hotfix", "post-hotfix"]:
        print(f"\n{'='*78}")
        print(f"  {period.upper()}")
        print(f"{'='*78}")

        # Overall
        b = buckets[(period, "ALL", "ALL")]
        if b["n"] > 0:
            print(f"\n  Overall:  n={b['n']:6d}  Brier={b['brier']/b['n']:.4f}  "
                  f"avg_pred={b['pred']/b['n']:.3f}  actual_freq={b['actual']/b['n']:.3f}")

        # By probability bucket
        print(f"\n  By prediction bucket:")
        print(f"    {'bucket':>10} {'n':>7} {'brier':>8} {'pred':>7} {'actual':>7} {'gap':>7}")
        for pb in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
            b = buckets[(period, pb, "ALL")]
            if b["n"] < 20:
                continue
            pred = b["pred"]/b["n"]
            actual = b["actual"]/b["n"]
            print(f"    {pb:>10} {b['n']:>7d} {b['brier']/b['n']:>8.4f} "
                  f"{pred:>7.3f} {actual:>7.3f} {actual-pred:>+7.3f}")

        # By horizon bucket
        print(f"\n  By horizon-to-settlement:")
        print(f"    {'bucket':>10} {'n':>7} {'brier':>8} {'pred':>7} {'actual':>7} {'gap':>7}")
        for hb in ["<1h", "1-4h", "4-12h", "12-24h", "24h+"]:
            b = buckets[(period, "ALL", hb)]
            if b["n"] < 20:
                continue
            pred = b["pred"]/b["n"]
            actual = b["actual"]/b["n"]
            print(f"    {hb:>10} {b['n']:>7d} {b['brier']/b['n']:>8.4f} "
                  f"{pred:>7.3f} {actual:>7.3f} {actual-pred:>+7.3f}")

    # Reliability bucket comparison
    print(f"\n\n{'='*78}")
    print(f"  RELIABILITY DELTA — pre vs post hotfix (per probability bucket)")
    print(f"{'='*78}")
    print(f"  {'bucket':>10} | {'pre n':>6} {'pre brier':>10} {'pre gap':>8} | "
          f"{'post n':>7} {'post brier':>11} {'post gap':>9}")
    for pb in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        pre = buckets[("pre-hotfix", pb, "ALL")]
        post = buckets[("post-hotfix", pb, "ALL")]
        pre_str = f"{pre['n']:>6d} {pre['brier']/pre['n']:>10.4f} {(pre['actual']-pre['pred'])/pre['n']:>+8.3f}" if pre["n"] >= 20 else f"{'-':>6} {'-':>10} {'-':>8}"
        post_str = f"{post['n']:>7d} {post['brier']/post['n']:>11.4f} {(post['actual']-post['pred'])/post['n']:>+9.3f}" if post["n"] >= 20 else f"{'-':>7} {'-':>11} {'-':>9}"
        print(f"  {pb:>10} | {pre_str} | {post_str}")


if __name__ == "__main__":
    main()
