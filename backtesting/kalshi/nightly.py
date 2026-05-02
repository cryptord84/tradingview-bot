#!/usr/bin/env python3
"""Nightly Kalshi backtest sweep — fires from launchd at ~00:30 ET.

Runs a curated parameter sweep across a curated market universe and writes a
markdown summary to backtesting/results/kalshi_nightly_<run_id>_summary.txt
matching the TV nightly format (consumed by dashboard / human review).

Usage:
  venv/bin/python -m backtesting.kalshi.nightly
  venv/bin/python -m backtesting.kalshi.nightly --markets-per-prefix 50  # quicker dev run
  venv/bin/python -m backtesting.kalshi.nightly --dry-run                 # don't write file
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root via -m or directly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtesting.kalshi.batch import (
    BatchOutcome, expand_sweep, load_universe_trades,
    pick_universe, run_batch_with_data,
)
from backtesting.kalshi.simulator import SimConfig
from backtesting.kalshi.strategies.spread import SpreadConfig


# ── Sweep configuration (edit here to change nightly behavior) ─────────────

# Event prefixes spanning Sports + World Events + Entertainment + Crypto strikes.
# Per Becker 2026-04-28 analysis: top maker-edge categories.
DEFAULT_EVENT_PREFIXES = [
    "KXNBAGAME",
    "KXNFLGAME",
    "KXMLBGAME",
    "KXNHLGAME",
    "KXNOBELPEACE",
    "KXOSCAR",
    "KXEPSTEIN",
    "KXARREST",
    "KXLAGODAYS",
    "KXBTCD",
    "KXETHD",
]

# Strategy param sweep grid (12 combos by default).
DEFAULT_SWEEP = {
    "half_spread_cents": [1, 2, 3],
    "dead_zone_lo": [35, 41],
    "dead_zone_hi": [50, 55],
}
BASE_CFG = SpreadConfig(
    half_spread_cents=2,
    contracts_per_side=10,
    dead_zone_lo=41,
    dead_zone_hi=50,
    inventory_skew_cents=0,
    max_inventory_per_market=50,
)

# Phase C/D realism / calibration sweep. Run the BEST strategy config across these
# sim configs to see how P&L responds to fee model + fill-rate haircut.
#
# Fee modes:
# - flat 0c: optimistic baseline (no fees)
# - flat 2c: legacy live spread_bot config — overestimates fees on tail prices
# - formula 7%: Kalshi's actual sports-market rate, ceil(7 × qty × P × (1-P)) cents
# - formula 3.5%: Kalshi's actual non-sports rate (most BTCD strikes etc.)
CALIBRATION_SIMS = [
    SimConfig(fee_per_contract_cents=0, fill_rate=1.0),                          # optimistic
    SimConfig(fee_per_contract_cents=2, fill_rate=1.0),                          # legacy flat 2c
    SimConfig(fee_formula_pct=7.0, fill_rate=1.0),                               # Kalshi sports
    SimConfig(fee_formula_pct=3.5, fill_rate=1.0),                               # Kalshi non-sports
    SimConfig(fee_formula_pct=7.0, fill_rate=0.50),                              # sports + queue loss
    SimConfig(fee_formula_pct=7.0, fill_rate=0.30),
    SimConfig(fee_formula_pct=7.0, fill_rate=0.10),
]


def _format_cfg(cfg: SpreadConfig) -> str:
    return (
        f"hs={cfg.half_spread_cents:>2}c  dz={cfg.dead_zone_lo}-{cfg.dead_zone_hi}c  "
        f"sz={cfg.contracts_per_side:>2}  inv={cfg.max_inventory_per_market:>3}  "
        f"skew={cfg.inventory_skew_cents}"
    )


def _format_sim(s: SimConfig) -> str:
    if s.fee_formula_pct > 0:
        fee_label = f"fee={s.fee_formula_pct:.1f}%×P(1-P)"
    else:
        fee_label = f"fee={s.fee_per_contract_cents}c flat"
    return f"{fee_label:<22} fill_rate={s.fill_rate:.2f}"


def _format_outcome_row(o: BatchOutcome) -> str:
    a = o.agg
    return (
        f"{_format_cfg(o.cfg)} | "
        f"P&L=${a.total_pnl_cents/100:+,.2f}  "
        f"ROI={a.roi_pct:+6.2f}%  "
        f"WR={a.win_rate_pct:5.1f}%  "
        f"Sharpe={a.sharpe_like:5.2f}  "
        f"DD=${a.max_drawdown_cents/100:.2f}  "
        f"fills={a.total_fills:,}  "
        f"adv={a.adverse_pct:.0f}%  "
        f"mkts={a.n_markets}"
    )


def _write_summary(
    run_id: str,
    started_at: datetime,
    elapsed_s: float,
    universe_count: int,
    sweep_outcomes: list[BatchOutcome],
    calib_outcomes: list[BatchOutcome],
    out_dir: Path,
) -> Path:
    """Write nightly markdown summary file in the same shape as TV nightly results."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kalshi_nightly_{run_id}_summary.txt"

    sorted_sweep = sorted(sweep_outcomes, key=lambda o: o.agg.roi_pct, reverse=True)

    with open(path, "w") as f:
        f.write(f"Kalshi Nightly Backtest — {run_id}\n")
        f.write(
            f"Started: {started_at.isoformat()}  Elapsed: {elapsed_s:.1f}s  "
            f"Strategy configs: {len(sweep_outcomes)}  "
            f"Calibration runs: {len(calib_outcomes)}  "
            f"Markets/config: {universe_count}\n"
        )
        f.write("Sim defaults for the strategy sweep: fee=0c, fill_rate=1.0 (optimistic).\n\n")

        f.write("=== STRATEGY SWEEP — sorted by ROI ===\n")
        for o in sorted_sweep:
            f.write(_format_outcome_row(o) + "\n")
        f.write("\n")

        if calib_outcomes:
            f.write("=== CALIBRATION SWEEP (best-strategy × fee × fill_rate) ===\n")
            f.write(f"Strategy: {_format_cfg(calib_outcomes[0].cfg)}\n")
            for o in calib_outcomes:
                f.write(f"  {_format_sim(o.sim_cfg):<42} | "
                        f"P&L=${o.agg.total_pnl_cents/100:+,.2f}  "
                        f"ROI={o.agg.roi_pct:+6.2f}%  "
                        f"WR={o.agg.win_rate_pct:5.1f}%  "
                        f"fees=${o.agg.total_fees_cents/100:.2f}  "
                        f"fills={o.agg.total_fills:,}  "
                        f"adv={o.agg.adverse_pct:.0f}%\n")
            f.write("\n")

        # Per-config detail for top strategy configs (3 best + 3 worst)
        f.write("=== PER-CONFIG BREAKDOWN (strategy sweep) ===\n")
        show = sorted_sweep[:3] + sorted_sweep[-3:] if len(sorted_sweep) > 6 else sorted_sweep
        seen = set()
        for o in show:
            key = id(o)
            if key in seen:
                continue
            seen.add(key)
            f.write(f"\n{_format_cfg(o.cfg)}\n")
            for line in o.agg.summary_lines():
                f.write(line + "\n")
            top = sorted(o.results, key=lambda r: r.pnl_cents, reverse=True)[:3]
            bot = sorted(o.results, key=lambda r: r.pnl_cents)[:3]
            f.write("  best 3 markets:\n")
            for r in top:
                f.write(f"    {r.market_ticker:<35} P&L=${r.pnl_cents/100:+.2f}  fills={r.n_fills}\n")
            f.write("  worst 3 markets:\n")
            for r in bot:
                f.write(f"    {r.market_ticker:<35} P&L=${r.pnl_cents/100:+.2f}  fills={r.n_fills}\n")

    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets-per-prefix", type=int, default=30,
                        help="Top-N markets to backtest per event prefix")
    parser.add_argument("--min-volume", type=int, default=50_000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write summary file")
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc)
    run_id = started_at.astimezone().strftime("%Y%m%d_%H%M")

    print(f"Kalshi nightly backtest — {run_id}")
    print(f"Picking universe: {len(DEFAULT_EVENT_PREFIXES)} prefixes × "
          f"{args.markets_per_prefix} markets each (min_volume={args.min_volume})")
    t0 = time.time()
    universe = pick_universe(
        DEFAULT_EVENT_PREFIXES,
        per_prefix=args.markets_per_prefix,
        min_volume=args.min_volume,
    )
    print(f"  universe: {len(universe)} markets ({time.time()-t0:.1f}s)\n")

    if not universe:
        print("ERROR: empty universe. Check event prefixes and min_volume.", file=sys.stderr)
        sys.exit(2)

    print("Bulk-loading all trades for the universe (one parquet scan)…", flush=True)
    t_load = time.time()
    trades_by_ticker = load_universe_trades(universe)
    n_with_trades = sum(1 for ts in trades_by_ticker.values() if ts)
    total_trades = sum(len(ts) for ts in trades_by_ticker.values())
    print(f"  loaded {total_trades:,} trades across {n_with_trades} markets "
          f"({time.time()-t_load:.1f}s)")

    configs = expand_sweep(BASE_CFG, DEFAULT_SWEEP)
    print(f"\nSweep grid: {len(configs)} configs × {len(universe)} markets = "
          f"{len(configs) * len(universe):,} backtests")

    sweep_outcomes: list[BatchOutcome] = []
    for ci, cfg in enumerate(configs, 1):
        t1 = time.time()
        outcome = run_batch_with_data(universe, trades_by_ticker, cfg)
        sweep_outcomes.append(outcome)
        print(
            f"[{ci:>2}/{len(configs)}] {_format_cfg(cfg)} → "
            f"{_format_outcome_row(outcome).split('|',1)[1].strip()} "
            f"({time.time()-t1:.1f}s)",
            flush=True,
        )

    # Calibration sweep: pick the best strategy config, run it through each SimConfig.
    print(f"\n--- Calibration sweep (best strategy × {len(CALIBRATION_SIMS)} sim configs) ---", flush=True)
    best_cfg = max(sweep_outcomes, key=lambda o: o.agg.roi_pct).cfg
    print(f"Best strategy: {_format_cfg(best_cfg)}")
    calib_outcomes: list[BatchOutcome] = []
    for sim_cfg in CALIBRATION_SIMS:
        t1 = time.time()
        outcome = run_batch_with_data(universe, trades_by_ticker, best_cfg, sim_cfg=sim_cfg)
        calib_outcomes.append(outcome)
        print(
            f"  {_format_sim(sim_cfg):<42} → "
            f"P&L=${outcome.agg.total_pnl_cents/100:+,.2f}  "
            f"ROI={outcome.agg.roi_pct:+6.2f}%  "
            f"adv={outcome.agg.adverse_pct:.0f}%  "
            f"fills={outcome.agg.total_fills:,}  "
            f"({time.time()-t1:.1f}s)",
            flush=True,
        )

    elapsed = time.time() - t0
    print(f"\nTotal elapsed: {elapsed:.1f}s")

    if args.dry_run:
        print("(--dry-run: not writing summary file)")
        return

    out_dir = Path(__file__).resolve().parents[1] / "results"
    path = _write_summary(run_id, started_at, elapsed, len(universe),
                          sweep_outcomes, calib_outcomes, out_dir)
    print(f"\nSummary written: {path}")


if __name__ == "__main__":
    main()
