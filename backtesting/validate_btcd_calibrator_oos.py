"""Out-of-sample validation for the BTCD empirical calibration table.

In-sample fits always look good — they're optimized against the data they're
trained on. OOS validation reveals whether the bias-correction shape generalizes
to unseen data, or whether it just memorized the training set.

Split: train on `[HOTFIX_TS, SPLIT_TS)`, test on `[SPLIT_TS, end]`.
Trains a fresh table on the train half (same PAV isotonic as production),
applies it to the test half, reports bucket gaps.

Re-enable gate: all non-extreme buckets (5%–95%) must show |gap| ≤ 5pt on TEST.

Usage:
    venv/bin/python backtesting/validate_btcd_calibrator_oos.py
    venv/bin/python backtesting/validate_btcd_calibrator_oos.py --split 2026-05-04
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backtesting.build_btcd_calibrator import (
    JSONL_PATH, SETTLEMENT_HOURS, HOTFIX_TS, NUM_BUCKETS,
    stream_entries, derive_outcomes,
    build_calibration_table,
)

# Default split: train on first ~10 days post-hotfix, test on remaining ~7
DEFAULT_SPLIT_TS = "2026-05-04T00:00:00"


def collect_samples_with_ts(path: str, outcomes: dict[str, int]) -> list[tuple[str, float, int]]:
    """Same as build_btcd_calibrator.collect_samples but keeps ts for splitting."""
    samples: list[tuple[str, float, int]] = []
    for e in stream_entries(path):
        if e["ts"] < HOTFIX_TS:
            continue
        t = e["ticker"]
        if t not in outcomes:
            continue
        if e["hours"] <= SETTLEMENT_HOURS:
            continue
        samples.append((e["ts"], float(e["fair_prob"]), outcomes[t]))
    return samples


def bucket(p: float) -> str:
    if p < 0.05: return "0-5%"
    if p < 0.20: return "5-20%"
    if p < 0.50: return "20-50%"
    if p < 0.80: return "50-80%"
    if p < 0.95: return "80-95%"
    return "95-100%"


def apply_table(breakpoints, p: float) -> float:
    if not breakpoints:
        return p
    if p <= breakpoints[0][0]:
        return breakpoints[0][1]
    if p >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= p <= x1:
            if x1 == x0:
                return y0
            t = (p - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return p


def bucket_stats(pairs):
    """pairs: list[(prob_for_bucketing, predicted_prob, outcome)]"""
    agg = defaultdict(lambda: {"n": 0, "sum_pred": 0.0, "sum_act": 0.0, "brier": 0.0})
    for buck_p, pred, outcome in pairs:
        bk = bucket(buck_p)
        agg[bk]["n"] += 1
        agg[bk]["sum_pred"] += pred
        agg[bk]["sum_act"] += outcome
        agg[bk]["brier"] += (pred - outcome) ** 2
    out = {}
    for bk, v in agg.items():
        n = v["n"]
        if n == 0:
            continue
        out[bk] = {
            "n": n,
            "pred": round(v["sum_pred"] / n, 4),
            "actual": round(v["sum_act"] / n, 4),
            "gap": round(v["sum_act"] / n - v["sum_pred"] / n, 4),
            "brier": round(v["brier"] / n, 4),
        }
    return out


def print_table(title: str, stats: dict):
    print(f"  {title}")
    print(f"  {'bucket':>8} {'n':>8} {'pred':>8} {'actual':>8} {'gap':>8} {'brier':>8}")
    for bk in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        v = stats.get(bk)
        if v:
            print(f"  {bk:>8} {v['n']:>8} {v['pred']:>8} {v['actual']:>8} {v['gap']:>+8.4f} {v['brier']:>8}")
    print()


def gate_check(stats: dict, label: str):
    failing = []
    max_abs = 0.0
    for bk in ["5-20%", "20-50%", "50-80%", "80-95%"]:
        v = stats.get(bk, {})
        gap = abs(v.get("gap", 0))
        if gap > max_abs:
            max_abs = gap
        if gap > 0.05:
            failing.append((bk, v.get("gap")))
    if failing:
        print(f"GATE on {label}: FAIL — {len(failing)} bucket(s) > 5pt:")
        for bk, gap in failing:
            print(f"  {bk}: gap={gap:+.4f}")
    else:
        print(f"GATE on {label}: PASS — all mid buckets ≤ ±5pt (max {max_abs:.4f})")
    return not failing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default=DEFAULT_SPLIT_TS,
                        help="ISO timestamp; samples before = train, samples after = test")
    args = parser.parse_args()
    split_ts = args.split
    if "T" not in split_ts:
        split_ts = split_ts + "T00:00:00"

    print(f"Loading jsonl from {JSONL_PATH}")
    print(f"  size: {os.path.getsize(JSONL_PATH) / 1e6:.1f} MB")
    print(f"Split: train < {split_ts} ≤ test")
    print()

    print("Pass 1: per-ticker outcomes…")
    outcomes = derive_outcomes(JSONL_PATH)
    print(f"  {len(outcomes)} resolved tickers, base rate {sum(outcomes.values())/max(1,len(outcomes)):.3f}")
    print()

    print("Pass 2: collecting timestamped samples…")
    all_samples = collect_samples_with_ts(JSONL_PATH, outcomes)
    print(f"  {len(all_samples)} total samples")
    train = [(p, o) for ts, p, o in all_samples if ts < split_ts]
    test = [(p, o) for ts, p, o in all_samples if ts >= split_ts]
    print(f"  train: {len(train)} samples (< {split_ts})")
    print(f"  test:  {len(test)} samples (>= {split_ts})")
    print()

    if len(train) < 1000 or len(test) < 1000:
        print(f"ERROR: train or test too small (< 1000)", file=sys.stderr)
        return 1

    print(f"Training PAV-isotonic table on train set ({NUM_BUCKETS} buckets)…")
    table = build_calibration_table(train, NUM_BUCKETS)
    breakpoints = table["breakpoints"]
    print(f"  {len(breakpoints)} breakpoints")
    print()

    print("=" * 78)
    print("  TRAIN SET diagnostics")
    print("=" * 78)
    train_pre = bucket_stats([(p, p, o) for p, o in train])
    train_post = bucket_stats([(p, apply_table(breakpoints, p), o) for p, o in train])
    print_table("PRE-calibration (parametric only, training data):", train_pre)
    print_table("POST-calibration (applied to training data — in-sample fit):", train_post)
    train_pass = gate_check(train_post, "TRAIN")
    print()

    print("=" * 78)
    print("  TEST SET diagnostics (OUT-OF-SAMPLE — the real test)")
    print("=" * 78)
    test_pre = bucket_stats([(p, p, o) for p, o in test])
    test_post = bucket_stats([(p, apply_table(breakpoints, p), o) for p, o in test])
    print_table("PRE-calibration (parametric only, test data):", test_pre)
    print_table("POST-calibration (applied to test data — OOS performance):", test_post)
    test_pass = gate_check(test_post, "TEST")
    print()

    print("=" * 78)
    print("  Bucket-by-bucket delta (PRE vs POST on TEST)")
    print("=" * 78)
    print(f"  {'bucket':>8} {'pre_gap':>10} {'post_gap':>10} {'|Δ|':>10}")
    for bk in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        pre_g = test_pre.get(bk, {}).get("gap")
        post_g = test_post.get(bk, {}).get("gap")
        if pre_g is None or post_g is None:
            continue
        delta = abs(pre_g) - abs(post_g)
        print(f"  {bk:>8} {pre_g:>+10.4f} {post_g:>+10.4f} {delta:>+10.4f}")
    print()

    if test_pass:
        print("OOS verdict: PASS — calibration generalizes; safe to re-enable at small size with this table.")
    else:
        print("OOS verdict: FAIL — calibration overfit or test-period regime shifted; do not re-enable.")
    return 0 if test_pass else 1


if __name__ == "__main__":
    sys.exit(main())
