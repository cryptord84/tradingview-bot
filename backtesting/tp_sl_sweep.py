#!/usr/bin/env python3
"""
TP/SL multiplier grid sweep per (strategy, token, timeframe).

Runs a grid of atr_sl_mult × atr_tp_mult combinations against the standard
backtest engine and picks a winner per slot. Output:
  - backtesting/results/tpsl_sweep_<ts>.csv     — every cell
  - config_tpsl_overrides.yaml                  — winners only, ready to load

Ranking objective: PF × confidence(trades), gated by max_dd ceiling.
Ties broken by avg_rr then net_profit.

Usage:
    venv/bin/python backtesting/tp_sl_sweep.py
    venv/bin/python backtesting/tp_sl_sweep.py --strategies "Mean Rev,RSI Div" --tfs 1H,4H
    venv/bin/python backtesting/tp_sl_sweep.py --tokens SOL,ETH,WIF --bars 3000
    venv/bin/python backtesting/tp_sl_sweep.py --dry-run   # don't write winners yaml
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtesting.data import (
    fetch_all, fetch_binance, fetch_coingecko,
    BINANCE_TOKENS, COINGECKO_TOKENS, TIMEFRAMES,
)
from backtesting.engine import (
    run_backtest, run_walkforward, BacktestResult, WalkForwardResult,
    RiskConfig, DEFAULT_RISK,
)
from backtesting.strategies import STRATEGIES

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_SL_GRID = [1.0, 1.25, 1.5, 2.0, 2.5]
DEFAULT_TP_GRID = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]

# Strategies with deployed Pine alerts / shortlist candidates
DEFAULT_STRATEGIES = [
    "Mean Rev", "RSI Div", "EMA Ribbon", "VWAP Dev",
    "Donchian", "Stoch RSI", "FVG", "Liq Sweep",
]

DEFAULT_TIMEFRAMES = ["1H", "4H"]

# Gate: reject cells where max_dd exceeds this
MAX_DD_CEILING = 25.0

# Confidence: trades / MIN_TRADES_FULL_CONF, capped at 1.0
MIN_TRADES_FULL_CONF = 30


# ── Score + winner picking ───────────────────────────────────────────────────

@dataclass
class Cell:
    strategy: str
    token: str
    timeframe: str
    sl_mult: float
    tp_mult: float
    pf: float
    win_rate: float
    trades: int
    max_dd: float
    net_profit: float
    sharpe: float
    avg_rr: float
    score: float
    passed: bool
    reject_reason: str
    # Walk-forward validation (populated only for winners when --walkforward is on)
    wf_passed: Optional[bool] = None
    wf_retention: Optional[float] = None
    wf_is_pf: Optional[float] = None
    wf_oos_pf: Optional[float] = None
    wf_fail_reason: str = ""

    def as_row(self) -> dict:
        return {
            "strategy": self.strategy,
            "token": self.token,
            "timeframe": self.timeframe,
            "sl_mult": self.sl_mult,
            "tp_mult": self.tp_mult,
            "pf": round(self.pf, 3),
            "win_rate": round(self.win_rate, 2),
            "trades": self.trades,
            "max_dd": round(self.max_dd, 2),
            "net_profit": round(self.net_profit, 2),
            "sharpe": round(self.sharpe, 3),
            "avg_rr": round(self.avg_rr, 3),
            "score": round(self.score, 4),
            "passed": self.passed,
            "reject_reason": self.reject_reason,
            "wf_passed": self.wf_passed if self.wf_passed is not None else "",
            "wf_retention": round(self.wf_retention, 3) if self.wf_retention is not None else "",
            "wf_is_pf": round(self.wf_is_pf, 3) if self.wf_is_pf is not None else "",
            "wf_oos_pf": round(self.wf_oos_pf, 3) if self.wf_oos_pf is not None else "",
            "wf_fail_reason": self.wf_fail_reason,
        }


def score_result(r: BacktestResult) -> tuple[float, bool, str]:
    """Return (score, passed, reject_reason).

    Gate first: hard reject on DD ceiling, zero trades, or non-finite PF.
    Otherwise score = PF × min(1, trades/MIN_TRADES_FULL_CONF).
    """
    if r.trade_count == 0:
        return 0.0, False, "no_trades"
    if r.max_drawdown >= MAX_DD_CEILING:
        return 0.0, False, f"max_dd>={MAX_DD_CEILING}"
    if r.profit_factor <= 0 or r.profit_factor != r.profit_factor:  # NaN
        return 0.0, False, "pf_invalid"
    if r.net_profit <= 0:
        return 0.0, False, "unprofitable"

    confidence = min(1.0, r.trade_count / MIN_TRADES_FULL_CONF)
    score = r.profit_factor * confidence
    return score, True, ""


def pick_winner(cells: list[Cell]) -> Optional[Cell]:
    """Select the best cell for a slot. Returns None if nothing passes."""
    passing = [c for c in cells if c.passed]
    if not passing:
        return None
    # Sort: score desc, avg_rr desc, net_profit desc
    passing.sort(key=lambda c: (c.score, c.avg_rr, c.net_profit), reverse=True)
    return passing[0]


def walkforward_filter(
    winners: dict[tuple[str, str, str], Cell],
    ohlcv_by_tf: dict[str, dict],
    base_risk: RiskConfig,
    min_oos_retention: float = 0.6,
    min_oos_pf_absolute: float = 1.2,
    split_pct: float = 0.7,
) -> tuple[dict, dict]:
    """Validate each winner via walk-forward split with its picked (sl, tp).

    Returns (validated_winners, rejected_winners). Mutates each Cell with
    wf_passed/wf_retention/wf_is_pf/wf_oos_pf/wf_fail_reason.

    Re-computes signals per slot (cheap) to avoid caching across the sweep.
    """
    validated: dict[tuple[str, str, str], Cell] = {}
    rejected: dict[tuple[str, str, str], Cell] = {}
    total = len(winners)

    print(f"\n{'='*80}")
    print(f"  WALK-FORWARD VALIDATION ({total} winners, {split_pct:.0%}/{(1-split_pct):.0%} split)")
    print(f"  Gate: OOS PF retention >= {min_oos_retention:.0%} AND OOS PF >= {min_oos_pf_absolute}")
    print(f"{'='*80}")

    for (strategy, token, tf), cell in winners.items():
        df = ohlcv_by_tf.get(tf, {}).get(token)
        if df is None:
            cell.wf_passed = False
            cell.wf_fail_reason = "no_df"
            rejected[(strategy, token, tf)] = cell
            continue

        strat_fn = STRATEGIES[strategy]
        try:
            signals = strat_fn(df)
        except Exception as e:
            cell.wf_passed = False
            cell.wf_fail_reason = f"signal_error:{e.__class__.__name__}"
            rejected[(strategy, token, tf)] = cell
            continue

        risk = RiskConfig(
            risk_per_trade_pct=base_risk.risk_per_trade_pct,
            atr_sl_mult=cell.sl_mult,
            atr_tp_mult=cell.tp_mult,
            trail_activation_atr=base_risk.trail_activation_atr,
            trail_offset_atr=base_risk.trail_offset_atr,
            trail_enabled=base_risk.trail_enabled,
            use_atr_stops=True,
        )
        try:
            wf: WalkForwardResult = run_walkforward(
                df, signals, token, strategy, tf,
                split_pct=split_pct,
                min_oos_pf_retention=min_oos_retention,
                min_oos_pf_absolute=min_oos_pf_absolute,
                risk=risk,
            )
        except Exception as e:
            cell.wf_passed = False
            cell.wf_fail_reason = f"wf_error:{e.__class__.__name__}"
            rejected[(strategy, token, tf)] = cell
            continue

        cell.wf_passed = wf.passed
        cell.wf_retention = wf.oos_pf_retention
        cell.wf_is_pf = wf.in_sample.profit_factor
        cell.wf_oos_pf = wf.out_of_sample.profit_factor

        if wf.passed:
            validated[(strategy, token, tf)] = cell
        else:
            cell.wf_fail_reason = "; ".join(wf.fail_reasons)[:120]
            rejected[(strategy, token, tf)] = cell

    print(f"  Validated: {len(validated)}/{total}   Rejected: {len(rejected)}")
    return validated, rejected


# ── Main sweep ───────────────────────────────────────────────────────────────

def build_grid(sl_grid: list[float], tp_grid: list[float]) -> list[tuple[float, float]]:
    """Return (sl, tp) pairs where tp > sl (RR > 1)."""
    return [(sl, tp) for sl in sl_grid for tp in tp_grid if tp > sl]


def _fetch_subset(tokens: list[str], timeframe: str, bars: int) -> dict:
    """Fetch only the specified tokens (skip the full 20-token fetch_all scan)."""
    import time as _time
    interval = TIMEFRAMES[timeframe]
    results: dict = {}
    for token in tokens:
        token_up = token.upper()
        if token_up in BINANCE_TOKENS:
            pair = BINANCE_TOKENS[token_up]
            print(f"  {token_up} ({pair})…", end=" ", flush=True)
            df = fetch_binance(pair, interval, bars)
            _time.sleep(0.1)
        elif token_up in COINGECKO_TOKENS:
            cg_id = COINGECKO_TOKENS[token_up]
            print(f"  {token_up} (CoinGecko:{cg_id})…", end=" ", flush=True)
            df = fetch_coingecko(cg_id, interval)
            _time.sleep(3.0)
        else:
            print(f"  [skip] unknown token: {token_up}")
            continue
        if df is not None and len(df) >= 50:
            results[token_up] = df
            print(f"{len(df)} bars")
        else:
            print("skipped")
    return results


def sweep_slot(
    strategy: str, token: str, timeframe: str,
    df, signals, grid: list[tuple[float, float]],
    base_risk: RiskConfig,
) -> list[Cell]:
    """Run every (sl, tp) cell for one slot. Signals computed once per slot."""
    cells: list[Cell] = []
    for sl, tp in grid:
        risk = RiskConfig(
            risk_per_trade_pct=base_risk.risk_per_trade_pct,
            atr_sl_mult=sl,
            atr_tp_mult=tp,
            trail_activation_atr=base_risk.trail_activation_atr,
            trail_offset_atr=base_risk.trail_offset_atr,
            trail_enabled=base_risk.trail_enabled,
            use_atr_stops=True,
        )
        try:
            r = run_backtest(df, signals, token, strategy, timeframe, risk=risk)
        except Exception as e:
            cells.append(Cell(
                strategy=strategy, token=token, timeframe=timeframe,
                sl_mult=sl, tp_mult=tp,
                pf=0.0, win_rate=0.0, trades=0, max_dd=0.0, net_profit=0.0,
                sharpe=0.0, avg_rr=0.0, score=0.0, passed=False,
                reject_reason=f"error:{e.__class__.__name__}",
            ))
            continue

        score, passed, reason = score_result(r)
        cells.append(Cell(
            strategy=strategy, token=token, timeframe=timeframe,
            sl_mult=sl, tp_mult=tp,
            pf=r.profit_factor, win_rate=r.win_rate, trades=r.trade_count,
            max_dd=r.max_drawdown, net_profit=r.net_profit,
            sharpe=r.sharpe_ratio, avg_rr=r.avg_rr,
            score=score, passed=passed, reject_reason=reason,
        ))
    return cells


def save_csv(cells: list[Cell], path: str) -> None:
    if not cells:
        return
    fieldnames = list(cells[0].as_row().keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in cells:
            writer.writerow(c.as_row())


def save_overrides_yaml(
    winners: dict, path: str, default_sl: float, default_tp: float,
    min_trades: int,
) -> tuple[int, int]:
    """Write winners into config-compatible YAML at config_tpsl_overrides.yaml.

    Shape:
      strategy_token_tf_overrides:
        <Strategy>:
          <TOKEN>:
            <TF>: {atr_sl_mult: X, atr_tp_mult: Y}

    Filters:
      - drops cells with trades < min_trades (low confidence)
      - drops cells matching the default (no override needed)

    Returns (written_count, filtered_low_conf_count).
    """
    try:
        import yaml
    except ImportError:
        print("  PyYAML not installed — skipping YAML output. Install: pip install pyyaml")
        return (0, 0)

    shape: dict = {}
    written = 0
    filtered_low_conf = 0
    for (strat, token, tf), c in winners.items():
        if c.trades < min_trades:
            filtered_low_conf += 1
            continue
        if c.sl_mult == default_sl and c.tp_mult == default_tp:
            continue
        shape.setdefault(strat, {}).setdefault(token, {})[tf] = {
            "atr_sl_mult": float(c.sl_mult),
            "atr_tp_mult": float(c.tp_mult),
        }
        written += 1
    with open(path, "w") as f:
        f.write(
            "# Generated by backtesting/tp_sl_sweep.py\n"
            f"# Baseline: sl={default_sl} tp={default_tp}. Only deviations listed.\n"
            f"# Ranking: PF × min(1, trades/{MIN_TRADES_FULL_CONF}) with max_dd < {MAX_DD_CEILING}%.\n"
            f"# Min trades to qualify: {min_trades} (low-confidence winners fall back to strategy defaults).\n\n"
        )
        yaml.safe_dump({"strategy_token_tf_overrides": shape}, f, sort_keys=True)
    return (written, filtered_low_conf)


def run_sweep(
    strategies: list[str], tokens: Optional[list[str]], tfs: list[str],
    bars: int, sl_grid: list[float], tp_grid: list[float],
    base_risk: RiskConfig, dry_run: bool, min_trades: int,
    walkforward: bool = True, min_oos_retention: float = 0.6,
) -> None:
    grid = build_grid(sl_grid, tp_grid)
    slots_planned = len(strategies) * (len(tokens) if tokens else "?") * len(tfs)
    print(f"\n{'='*80}")
    print(f"  TP/SL SWEEP")
    print(f"{'='*80}")
    print(f"  Strategies: {strategies}")
    print(f"  Tokens:     {tokens if tokens else 'ALL fetched'}")
    print(f"  TFs:        {tfs}")
    print(f"  SL grid:    {sl_grid}")
    print(f"  TP grid:    {tp_grid}")
    print(f"  Valid cells per slot: {len(grid)} (tp > sl)")
    print(f"  Slots planned: {slots_planned} → ~{slots_planned * len(grid) if isinstance(slots_planned, int) else '?'} backtests")
    print(f"  Bars: {bars}")
    print(f"{'='*80}\n")

    all_cells: list[Cell] = []
    winners: dict[tuple[str, str, str], Cell] = {}

    # Fetch data once per TF (heaviest op by far)
    ohlcv_by_tf: dict[str, dict] = {}
    for tf in tfs:
        print(f"Fetching {tf} data…")
        if tokens:
            ohlcv_by_tf[tf] = _fetch_subset(tokens, tf, bars)
        else:
            ohlcv_by_tf[tf] = fetch_all(tf, bars=bars)
        print(f"  → {len(ohlcv_by_tf[tf])} tokens available on {tf}\n")

    # Sweep loop
    for strategy in strategies:
        strat_fn = STRATEGIES.get(strategy)
        if strat_fn is None:
            print(f"[skip] unknown strategy: {strategy}")
            continue

        for tf in tfs:
            ohlcv_map = ohlcv_by_tf.get(tf, {})
            for token, df in ohlcv_map.items():
                print(f"  {strategy:<14} {token:<8} {tf}  ", end="", flush=True)
                try:
                    signals = strat_fn(df)
                except Exception as e:
                    print(f"ERROR signal gen: {e}")
                    continue

                cells = sweep_slot(strategy, token, tf, df, signals, grid, base_risk)
                all_cells.extend(cells)
                winner = pick_winner(cells)
                if winner is None:
                    print("no passing cells")
                else:
                    winners[(strategy, token, tf)] = winner
                    print(
                        f"winner: sl={winner.sl_mult} tp={winner.tp_mult} "
                        f"PF={winner.pf:.2f} trades={winner.trades} DD={winner.max_dd:.1f}%"
                    )

    # ── Walk-forward validation pass (post-filter) ───────────────────────────
    rejected_winners: dict = {}
    if walkforward and winners:
        winners, rejected_winners = walkforward_filter(
            winners, ohlcv_by_tf, base_risk,
            min_oos_retention=min_oos_retention,
        )

    # ── Outputs ──────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)

    csv_path = os.path.join(results_dir, f"tpsl_sweep_{ts}.csv")
    save_csv(all_cells, csv_path)
    print(f"\n  Cells CSV: {csv_path}")

    if dry_run:
        print("  [dry-run] skipping winners YAML")
        written = filtered_low_conf = 0
    else:
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "config_tpsl_overrides.yaml",
        )
        written, filtered_low_conf = save_overrides_yaml(
            winners, yaml_path,
            default_sl=base_risk.atr_sl_mult, default_tp=base_risk.atr_tp_mult,
            min_trades=min_trades,
        )
        print(f"  Winners YAML: {yaml_path}")
        print(f"    {written} overrides written, {filtered_low_conf} filtered (trades<{min_trades})")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n  Slots evaluated:  {len(all_cells) // max(len(grid), 1)}")
    print(f"  Cells tested:     {len(all_cells)}")
    if walkforward:
        print(f"  WF-validated:     {len(winners)}  (rejected: {len(rejected_winners)})")
    print(f"  Winners kept:     {len(winners)}")
    if winners:
        # Show deviations from default
        deviations = [
            (k, c) for k, c in winners.items()
            if c.sl_mult != base_risk.atr_sl_mult or c.tp_mult != base_risk.atr_tp_mult
        ]
        print(f"  Non-default winners: {len(deviations)}")
        print("\n  Top 10 by score:")
        top = sorted(winners.values(), key=lambda c: c.score, reverse=True)[:10]
        for c in top:
            print(
                f"    {c.strategy:<14} {c.token:<8} {c.timeframe:<4} "
                f"sl={c.sl_mult} tp={c.tp_mult}  "
                f"PF={c.pf:.2f} WR={c.win_rate:.1f}% trades={c.trades} "
                f"DD={c.max_dd:.1f}% score={c.score:.2f}"
            )


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_list(s: Optional[str]) -> Optional[list[str]]:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_float_list(s: Optional[str], default: list[float]) -> list[float]:
    if not s:
        return default
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="TP/SL multiplier grid sweep")
    parser.add_argument("--strategies", type=str, default=None,
                        help=f"Comma-separated strategy names (default: {','.join(DEFAULT_STRATEGIES)})")
    parser.add_argument("--tokens", type=str, default=None,
                        help="Comma-separated tokens (default: ALL fetched)")
    parser.add_argument("--tfs", type=str, default=None,
                        help=f"Comma-separated timeframes (default: {','.join(DEFAULT_TIMEFRAMES)})")
    parser.add_argument("--sl-grid", type=str, default=None,
                        help=f"Comma-separated SL multipliers (default: {DEFAULT_SL_GRID})")
    parser.add_argument("--tp-grid", type=str, default=None,
                        help=f"Comma-separated TP multipliers (default: {DEFAULT_TP_GRID})")
    parser.add_argument("--bars", type=int, default=2000,
                        help="Bars per symbol (default: 2000)")
    parser.add_argument("--risk-pct", type=float, default=2.0,
                        help="Risk per trade %% (default: 2.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip writing winners YAML")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="Minimum trade count for a winner to be written to YAML (default: 20)")
    parser.add_argument("--no-walkforward", action="store_true",
                        help="Skip walk-forward validation pass (not recommended for production)")
    parser.add_argument("--min-oos-retention", type=float, default=0.6,
                        help="Minimum OOS PF retention for walk-forward pass (default: 0.6)")
    args = parser.parse_args()

    strategies = _parse_list(args.strategies) or DEFAULT_STRATEGIES
    tokens = _parse_list(args.tokens)  # None = all
    tfs = _parse_list(args.tfs) or DEFAULT_TIMEFRAMES
    sl_grid = _parse_float_list(args.sl_grid, DEFAULT_SL_GRID)
    tp_grid = _parse_float_list(args.tp_grid, DEFAULT_TP_GRID)

    base_risk = RiskConfig(
        risk_per_trade_pct=args.risk_pct,
        atr_sl_mult=DEFAULT_RISK.atr_sl_mult,
        atr_tp_mult=DEFAULT_RISK.atr_tp_mult,
        trail_activation_atr=DEFAULT_RISK.trail_activation_atr,
        trail_offset_atr=DEFAULT_RISK.trail_offset_atr,
        trail_enabled=DEFAULT_RISK.trail_enabled,
        use_atr_stops=True,
    )

    run_sweep(
        strategies=strategies, tokens=tokens, tfs=tfs,
        bars=args.bars, sl_grid=sl_grid, tp_grid=tp_grid,
        base_risk=base_risk, dry_run=args.dry_run,
        min_trades=args.min_trades,
        walkforward=not args.no_walkforward,
        min_oos_retention=args.min_oos_retention,
    )


if __name__ == "__main__":
    main()
