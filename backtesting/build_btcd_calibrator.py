"""Train an empirical calibration table for the BTCD strike-probability model.

The parametric log-normal model in `app/services/kalshi_crypto_strikes.fair_prob`
has known overdispersion: in the 2026-05-11 audit, the 20-50% bucket overpredicts
YES by ~8pts and the 50-80% bucket underpredicts by ~10pts. Model probabilities
get pulled toward 0.5; extremes are too soft.

This script learns the empirical pred→actual mapping from accumulated calibration
snapshots, smooths it via Pool Adjacent Violators (isotonic regression), and
saves a JSON lookup table that the bot uses at trading time.

Output: `data/btcd_calibration.json` with:
  - breakpoints: list of (pred_x, calibrated_y) tuples (monotonic in x, increasing in y)
  - meta: training data summary (sample count, date range, bucket diagnostics)

Runtime: ~2 minutes on a 243MB / 900k row jsonl input.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

# Repo-relative paths
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSONL_PATH = os.path.join(ROOT, "data", "kalshi_strikes_calibration.jsonl")
OUTPUT_PATH = os.path.join(ROOT, "data", "btcd_calibration.json")

# Training filters — match the audit script's settled-tick definition
SETTLEMENT_HOURS = 0.15  # closest-to-settlement entry counts as outcome
HOTFIX_TS = "2026-04-24T00:00:00"  # vol-model change; only use post-hotfix data
NUM_BUCKETS = 50  # equal-population bins for the regression


def stream_entries(path: str):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def derive_outcomes(path: str) -> dict[str, int]:
    """For each ticker, find the snapshot closest to settlement. If within
    `SETTLEMENT_HOURS`, outcome = 1 if spot >= strike else 0."""
    closest: dict[str, dict] = {}
    for e in stream_entries(path):
        t = e["ticker"]
        h = e["hours"]
        cur = closest.get(t)
        if cur is None or h < cur["hours"]:
            closest[t] = e
    outcomes = {}
    for t, e in closest.items():
        if e["hours"] <= SETTLEMENT_HOURS:
            outcomes[t] = 1 if e["spot"] >= e["strike"] else 0
    return outcomes


def collect_samples(path: str, outcomes: dict[str, int]) -> list[tuple[float, int]]:
    """Pair every non-settlement snapshot with its ticker's eventual outcome.
    Filters to post-hotfix and skips outcome-snapshots themselves (avoid trivial
    near-zero/near-one autocorrelation)."""
    samples: list[tuple[float, int]] = []
    for e in stream_entries(path):
        if e["ts"] < HOTFIX_TS:
            continue
        t = e["ticker"]
        if t not in outcomes:
            continue
        if e["hours"] <= SETTLEMENT_HOURS:
            continue  # don't train on the outcome-defining row
        samples.append((float(e["fair_prob"]), outcomes[t]))
    return samples


def pool_adjacent_violators(bins: list[dict]) -> list[dict]:
    """Force monotonic-increasing mean(actual) across pred-sorted bins.
    Each bin is {"pred": mean_pred, "actual": mean_actual, "n": count}.
    Pools adjacent bins where actual drops; weighted by n."""
    out = [dict(b) for b in bins]
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(out) - 1:
            if out[i]["actual"] > out[i + 1]["actual"]:
                # Pool i and i+1
                n = out[i]["n"] + out[i + 1]["n"]
                pred = (out[i]["pred"] * out[i]["n"] + out[i + 1]["pred"] * out[i + 1]["n"]) / n
                actual = (out[i]["actual"] * out[i]["n"] + out[i + 1]["actual"] * out[i + 1]["n"]) / n
                out[i] = {"pred": pred, "actual": actual, "n": n}
                del out[i + 1]
                changed = True
            else:
                i += 1
    return out


def build_calibration_table(samples: list[tuple[float, int]], n_buckets: int) -> dict:
    """Sort by pred, bin into equal-population buckets, PAV to enforce monotonicity."""
    samples.sort(key=lambda s: s[0])
    n = len(samples)
    if n == 0:
        return {"breakpoints": [], "meta": {"n_samples": 0}}

    bin_size = max(1, n // n_buckets)
    bins = []
    for i in range(0, n, bin_size):
        chunk = samples[i:i + bin_size]
        if not chunk:
            continue
        mean_pred = sum(p for p, _ in chunk) / len(chunk)
        mean_actual = sum(o for _, o in chunk) / len(chunk)
        bins.append({"pred": mean_pred, "actual": mean_actual, "n": len(chunk)})

    smoothed = pool_adjacent_violators(bins)

    # Edge guards — anchor endpoints to (0.001, 0.001) and (0.999, 0.999) so we
    # never produce 0 or 1 (would imply certainty the bot doesn't have).
    breakpoints = []
    if smoothed and smoothed[0]["pred"] > 0.01:
        breakpoints.append([0.001, max(0.001, smoothed[0]["actual"] * 0.5)])
    for b in smoothed:
        breakpoints.append([b["pred"], max(0.001, min(0.999, b["actual"]))])
    if smoothed and smoothed[-1]["pred"] < 0.99:
        breakpoints.append([0.999, min(0.999, smoothed[-1]["actual"] + (1.0 - smoothed[-1]["actual"]) * 0.5)])

    return {
        "breakpoints": breakpoints,
        "meta": {
            "n_samples": n,
            "n_buckets_pre_pav": len(bins),
            "n_buckets_post_pav": len(smoothed),
            "training_filter": {"hotfix_ts": HOTFIX_TS, "settlement_hours": SETTLEMENT_HOURS},
            "built_at": datetime.utcnow().isoformat() + "Z",
            "bin_stats": [
                {"pred": round(b["pred"], 4), "actual": round(b["actual"], 4), "n": b["n"]}
                for b in smoothed
            ],
        },
    }


def evaluate(samples: list[tuple[float, int]], table: dict) -> dict:
    """Apply the calibration to the training samples; report Brier + bucket gaps
    pre and post calibration."""
    breakpoints = table["breakpoints"]
    if not breakpoints:
        return {}

    def lookup(p: float) -> float:
        xs = [b[0] for b in breakpoints]
        ys = [b[1] for b in breakpoints]
        if p <= xs[0]:
            return ys[0]
        if p >= xs[-1]:
            return ys[-1]
        # Linear interp
        for i in range(len(xs) - 1):
            if xs[i] <= p <= xs[i + 1]:
                t = (p - xs[i]) / (xs[i + 1] - xs[i])
                return ys[i] + t * (ys[i + 1] - ys[i])
        return p

    def bucket(p: float) -> str:
        if p < 0.05: return "0-5%"
        if p < 0.20: return "5-20%"
        if p < 0.50: return "20-50%"
        if p < 0.80: return "50-80%"
        if p < 0.95: return "80-95%"
        return "95-100%"

    pre = defaultdict(lambda: {"n": 0, "sum_pred": 0.0, "sum_act": 0.0, "brier": 0.0})
    post = defaultdict(lambda: {"n": 0, "sum_pred": 0.0, "sum_act": 0.0, "brier": 0.0})
    for p, o in samples:
        a = lookup(p)
        pb = bucket(p)
        for d, pred in ((pre, p), (post, a)):
            ab = bucket(pred)
            d[ab]["n"] += 1
            d[ab]["sum_pred"] += pred
            d[ab]["sum_act"] += o
            d[ab]["brier"] += (pred - o) ** 2

    def summarize(d):
        out = {}
        for bk, vals in d.items():
            n = vals["n"]
            if n == 0:
                continue
            mean_pred = vals["sum_pred"] / n
            mean_act = vals["sum_act"] / n
            brier = vals["brier"] / n
            out[bk] = {"n": n, "pred": round(mean_pred, 4),
                       "actual": round(mean_act, 4),
                       "gap": round(mean_act - mean_pred, 4),
                       "brier": round(brier, 4)}
        return out

    return {"pre": summarize(pre), "post": summarize(post)}


def main():
    print(f"Loading jsonl from {JSONL_PATH}")
    if not os.path.exists(JSONL_PATH):
        print(f"  ERROR: file not found", file=sys.stderr)
        return 1

    print(f"  size: {os.path.getsize(JSONL_PATH) / 1e6:.1f} MB")
    print()

    print("Pass 1: deriving per-ticker outcomes…")
    outcomes = derive_outcomes(JSONL_PATH)
    print(f"  {len(outcomes)} tickers with outcome (hours <= {SETTLEMENT_HOURS})")
    yes_share = sum(outcomes.values()) / max(1, len(outcomes))
    print(f"  base rate (YES): {yes_share:.3f}")
    print()

    print(f"Pass 2: collecting (fair_prob, outcome) samples post-hotfix {HOTFIX_TS}…")
    samples = collect_samples(JSONL_PATH, outcomes)
    print(f"  {len(samples)} samples collected")
    print()

    print(f"Building calibration table with {NUM_BUCKETS} buckets + PAV…")
    table = build_calibration_table(samples, NUM_BUCKETS)
    print(f"  {len(table['breakpoints'])} breakpoints after smoothing")
    print()

    print("Evaluating pre vs post calibration…")
    eval_result = evaluate(samples, table)

    fmt = "  {:>8} {:>8} {:>8} {:>8} {:>8} {:>8}"
    print("  PRE (parametric)")
    print(fmt.format("bucket", "n", "pred", "actual", "gap", "brier"))
    for bk in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        v = eval_result.get("pre", {}).get(bk)
        if v:
            print(fmt.format(bk, v["n"], v["pred"], v["actual"], v["gap"], v["brier"]))
    print()
    print("  POST (calibrated)")
    print(fmt.format("bucket", "n", "pred", "actual", "gap", "brier"))
    for bk in ["0-5%", "5-20%", "20-50%", "50-80%", "80-95%", "95-100%"]:
        v = eval_result.get("post", {}).get(bk)
        if v:
            print(fmt.format(bk, v["n"], v["pred"], v["actual"], v["gap"], v["brier"]))
    print()

    # Persist
    out = {**table, "validation": eval_result}
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUTPUT_PATH}")

    # Pass/fail vs the ≤5pt re-enable gate
    max_abs_gap = 0.0
    failing = []
    for bk in ["5-20%", "20-50%", "50-80%", "80-95%"]:
        v = eval_result.get("post", {}).get(bk, {})
        gap = abs(v.get("gap", 0))
        if gap > max_abs_gap:
            max_abs_gap = gap
        if gap > 0.05:
            failing.append((bk, v.get("gap")))
    print()
    if failing:
        print(f"GATE: FAIL — {len(failing)} non-extreme bucket(s) still > 5pt:")
        for bk, gap in failing:
            print(f"  {bk}: gap={gap:+.4f}")
    else:
        print(f"GATE: PASS — all mid buckets ≤ ±5pt (max {max_abs_gap:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
