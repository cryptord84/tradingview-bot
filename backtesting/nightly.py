#!/usr/bin/env python3
"""
Nightly backtest runner — runs full strategy matrix and inserts results into dashboard DB.

Designed to run at midnight when Claude usage is lowest.
Generates a summary report and suggests leverage per strategy/token pair.

Usage:
    venv/bin/python backtesting/nightly.py
    venv/bin/python backtesting/nightly.py --bars 3000
    venv/bin/python backtesting/nightly.py --dry-run   # print results, don't insert into DB
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtesting.data import fetch_all, TIMEFRAMES, BINANCE_TOKENS, COINGECKO_TOKENS
from backtesting.engine import (
    run_backtest, run_walkforward, BacktestResult, WalkForwardResult,
    RiskConfig, DEFAULT_RISK, risk_for,
)
from backtesting.strategies import STRATEGIES, with_htf_filter


# ── Leverage recommendation logic ────────────────────────────────────────────
# Based on backtest quality: PF, win rate, Sharpe, max drawdown, trade count

def suggest_leverage(r: BacktestResult) -> float:
    """
    Recommend leverage multiplier based on backtest performance.

    Returns 0.0 (don't trade) to 3.0 (max confidence).
    Criteria:
      - Must pass base thresholds to get > 1.0x
      - Higher leverage requires progressively better stats
      - Drawdown is the primary governor — high DD = low leverage regardless
    """
    # Don't trade: failed thresholds or too few trades
    if not r.passed:
        return 0.0

    score = 1.0  # base: 1x leverage (no leverage)

    # Profit factor bonus
    if r.profit_factor >= 2.5:
        score += 0.5
    elif r.profit_factor >= 2.0:
        score += 0.3
    elif r.profit_factor >= 1.7:
        score += 0.15

    # Win rate bonus
    if r.win_rate >= 65:
        score += 0.3
    elif r.win_rate >= 55:
        score += 0.15

    # Sharpe ratio bonus
    if r.sharpe_ratio >= 1.5:
        score += 0.4
    elif r.sharpe_ratio >= 1.0:
        score += 0.2
    elif r.sharpe_ratio >= 0.5:
        score += 0.1

    # Trade count confidence bonus (more trades = more reliable)
    if r.trade_count >= 100:
        score += 0.2
    elif r.trade_count >= 60:
        score += 0.1

    # Drawdown penalty — the hard cap
    if r.max_drawdown >= 25:
        score = min(score, 1.0)  # cap at 1x if DD > 25%
    elif r.max_drawdown >= 15:
        score = min(score, 1.5)  # cap at 1.5x if DD > 15%
    elif r.max_drawdown >= 10:
        score = min(score, 2.0)  # cap at 2x if DD > 10%

    return min(round(score, 1), 3.0)


# ── Focus tokens: all tradeable tokens (Binance + CoinGecko) ─────────────────
FOCUS_TOKENS = list(BINANCE_TOKENS.keys()) + list(COINGECKO_TOKENS.keys())

# ── Timeframes to test ───────────────────────────────────────────────────────
FOCUS_TIMEFRAMES = ["15m", "1H", "4H"]

# ── Core strategies (skip regime-filtered duplicates in nightly — they rarely help)
CORE_STRATEGIES = [
    "Supertrend", "Donchian", "EMA Ribbon", "VWAP Dev", "Stoch RSI",
    "FVG", "MACD Vol", "Liq Sweep", "RSI Div", "Mean Rev",
]


def run_nightly(
    bars: int = 2000,
    dry_run: bool = False,
    include_regime: bool = False,
    htf_filter: bool = False,
    long_only: bool = True,
):
    """Run the full nightly backtest matrix with walk-forward validation.

    long_only defaults True because the live bot is long-only (Pine indicators emit
    no shorts). Combined long+short PFs systematically overstate live edge — see
    perps follow-up memo for the full analysis.
    """
    run_id = datetime.now(timezone.utc).strftime("nightly_%Y%m%d_%H%M")
    if htf_filter:
        run_id += "_htf"
    if not long_only:
        run_id += "_combined"
    strategies = list(STRATEGIES.keys()) if include_regime else CORE_STRATEGIES

    print(f"\n{'='*80}")
    print(f"  NIGHTLY BACKTEST — {run_id}")
    print(f"  Strategies: {len(strategies)} | Tokens: {len(FOCUS_TOKENS)} | TFs: {FOCUS_TIMEFRAMES}")
    print(f"  Bars: {bars} | Risk: {DEFAULT_RISK.risk_per_trade_pct}%/trade")
    print(f"  Validation: 70/30 walk-forward (IS must pass + OOS PF retention >= 60%)")
    print(f"  HTF filter: {'ON (4× TF EMA(20) slope gates entries)' if htf_filter else 'off'}")
    print(f"  Shorts:     {'enabled (combined long+short — analysis only)' if not long_only else 'disabled (long-only — matches live bot)'}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE (inserting into DB)'}")
    print(f"{'='*80}\n")

    all_results: list[BacktestResult] = []
    all_with_wf: list[tuple[BacktestResult, WalkForwardResult]] = []
    passing: list[tuple[BacktestResult, float, WalkForwardResult]] = []

    for tf in FOCUS_TIMEFRAMES:
        print(f"\nFetching {tf} data for {len(FOCUS_TOKENS)} tokens...")
        ohlcv_map = fetch_all(tf, bars=bars)
        if not ohlcv_map:
            print(f"  No data for {tf}, skipping.")
            continue

        for strat_name in strategies:
            if strat_name not in STRATEGIES:
                continue
            strat_fn = STRATEGIES[strat_name]
            if htf_filter:
                strat_fn = with_htf_filter(strat_fn, htf_multiplier=4, ema_length=20)

            for token, df in ohlcv_map.items():
                if token not in FOCUS_TOKENS:
                    continue
                try:
                    signals = strat_fn(df, enable_short=not long_only)
                    risk = risk_for(strat_name, token, tf)
                    wf = run_walkforward(
                        df, signals, token, strat_name, tf,
                        split_pct=0.7,
                        min_oos_pf_retention=0.6,
                        min_oos_pf_absolute=1.2,
                        risk=risk,
                    )
                except Exception as e:
                    print(f"  ERROR: {strat_name}/{token}/{tf}: {e}")
                    continue

                result = wf.combined  # use full-window for leverage/display
                all_results.append(result)
                all_with_wf.append((result, wf))
                # Leverage is gated on WF pass — failed WF = don't deploy
                lev = suggest_leverage(result) if wf.passed else 0.0

                if wf.passed:
                    passing.append((result, lev, wf))

                if not dry_run:
                    _insert_result(result, lev, run_id, bars, wf=wf)

    # ── Summary report ───────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  NIGHTLY RESULTS — {run_id}")
    print(f"{'='*80}")
    print(f"  Total tests: {len(all_results)}")
    print(f"  Passing: {len(passing)}")

    if passing:
        print(f"\n  {'Strategy':<14} {'Token':<7} {'TF':<4} {'PF':>5} {'WR%':>6} "
              f"{'Trades':>7} {'DD%':>6} {'NP%':>7} {'Sharpe':>7} "
              f"{'IS PF':>6} {'OOS PF':>7} {'Ret%':>5} {'Lev':>5}")
        print(f"  {'-'*92}")
        for r, lev, wf in sorted(passing, key=lambda x: x[1], reverse=True):
            lev_str = f"{lev:.1f}x"
            if lev >= 2.0:
                lev_str += " **"
            elif lev >= 1.5:
                lev_str += " *"
            print(f"  {r.strategy:<14} {r.token:<7} {r.timeframe:<4} {r.profit_factor:>5.2f} "
                  f"{r.win_rate:>5.1f}% {r.trade_count:>7} {r.max_drawdown:>5.1f}% "
                  f"{r.net_profit:>6.1f}% {r.sharpe_ratio:>6.2f} "
                  f"{wf.in_sample.profit_factor:>6.2f} {wf.out_of_sample.profit_factor:>7.2f} "
                  f"{wf.oos_pf_retention*100:>4.0f}% {lev_str:>7}")
    else:
        print("\n  No strategies passed walk-forward validation.")

    # ── Top combos by combined PF (regardless of WF pass) — distribution view
    near = [(r, wf) for r, wf in all_with_wf if r.trade_count >= 30]
    near.sort(key=lambda x: x[0].profit_factor, reverse=True)
    if near:
        print(f"\n  Top 15 by combined PF (≥30 trades, WF status shown):")
        print(f"  {'Strategy':<14} {'Token':<8} {'TF':<4} {'PF':>5} {'WR%':>6} "
              f"{'Trades':>7} {'DD%':>6} {'IS PF':>6} {'OOS PF':>7} {'Ret%':>5} {'WF':>5}")
        print(f"  {'-'*88}")
        for r, wf in near[:15]:
            wf_tag = "PASS" if wf.passed else "fail"
            print(f"  {r.strategy:<14} {r.token:<8} {r.timeframe:<4} {r.profit_factor:>5.2f} "
                  f"{r.win_rate:>5.1f}% {r.trade_count:>7} {r.max_drawdown:>5.1f}% "
                  f"{wf.in_sample.profit_factor:>6.2f} {wf.out_of_sample.profit_factor:>7.2f} "
                  f"{wf.oos_pf_retention*100:>4.0f}% {wf_tag:>5}")

    # PF distribution histogram
    pfs = [r.profit_factor for r in all_results if r.trade_count >= 30]
    if pfs:
        bins = [(0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.2),
                (1.2, 1.5), (1.5, 2.0), (2.0, 999)]
        print(f"\n  PF distribution (n={len(pfs)} combos with ≥30 trades):")
        for lo, hi in bins:
            n = sum(1 for p in pfs if lo <= p < hi)
            bar = "█" * min(n, 60)
            label = f"{lo:.2f}-{hi:.2f}" if hi < 999 else f"{lo:.2f}+"
            print(f"    {label:>12}  {n:>4}  {bar}")

    # Write summary to file
    summary_path = os.path.join(os.path.dirname(__file__), "results", f"{run_id}_summary.txt")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w") as f:
        f.write(f"Nightly Backtest Summary — {run_id}\n")
        f.write(f"Tests: {len(all_results)} | Passing (WF-validated): {len(passing)}\n\n")
        if passing:
            for r, lev, wf in sorted(passing, key=lambda x: x[1], reverse=True):
                f.write(f"{r.strategy:<14} {r.token:<7} {r.timeframe:<4} "
                        f"PF={r.profit_factor:.2f} WR={r.win_rate:.1f}% "
                        f"Trades={r.trade_count} DD={r.max_drawdown:.1f}% "
                        f"NP={r.net_profit:.1f}% Sharpe={r.sharpe_ratio:.2f} "
                        f"IS_PF={wf.in_sample.profit_factor:.2f} "
                        f"OOS_PF={wf.out_of_sample.profit_factor:.2f} "
                        f"Ret={wf.oos_pf_retention*100:.0f}% "
                        f"Lev={lev:.1f}x\n")
        else:
            f.write("No strategies passed walk-forward validation.\n")

        # Near-pass: top 15 by combined PF among combos with >=30 trades
        if near:
            f.write("\nTop 15 by combined PF (>=30 trades, WF status shown):\n")
            for r, wf in near[:15]:
                wf_tag = "PASS" if wf.passed else "fail"
                f.write(f"  {r.strategy:<14} {r.token:<7} {r.timeframe:<4} "
                        f"PF={r.profit_factor:.2f} WR={r.win_rate:.1f}% "
                        f"Trades={r.trade_count} DD={r.max_drawdown:.1f}% "
                        f"IS_PF={wf.in_sample.profit_factor:.2f} "
                        f"OOS_PF={wf.out_of_sample.profit_factor:.2f} "
                        f"Ret={wf.oos_pf_retention*100:.0f}% [{wf_tag}]\n")

        # PF distribution histogram
        if pfs:
            f.write(f"\nPF distribution (n={len(pfs)} combos with >=30 trades):\n")
            for lo, hi in bins:
                n = sum(1 for p in pfs if lo <= p < hi)
                label = f"{lo:.2f}-{hi:.2f}" if hi < 999 else f"{lo:.2f}+"
                f.write(f"  {label:>12}  {n:>4}\n")
    print(f"\n  Summary saved: {summary_path}")

    # Regenerate tiered position-sizing overrides for the live bot.
    # Only emit overrides from long-only runs — combined long+short overstates live edge.
    if not dry_run and long_only:
        try:
            write_sizing_overrides(passing, htf=htf_filter)
        except Exception as e:
            print(f"  sizing overrides update failed: {e}")

    return all_results, passing


# ── Tiered position sizing — regenerates config_sizing_overrides.yaml ────────
# PF thresholds: A >= 3.0 (20%), B 2.0-3.0 (15%), C 1.5-2.0 (10%). Below 1.5 = omit.
SIZING_TIERS = [
    (3.0, "A", 20.0),
    (2.0, "B", 15.0),
    (1.5, "C", 10.0),
]


def _tier_for_pf(pf: float) -> Optional[tuple[str, float]]:
    for min_pf, tier, size_pct in SIZING_TIERS:
        if pf >= min_pf:
            return tier, size_pct
    return None


def write_sizing_overrides(passing: list, htf: bool = False) -> None:
    """Merge `passing` into config_sizing_overrides.yaml — max PF wins per slot.

    Called after each nightly run so main and HTF runs accumulate into a single file.
    Strategies with PF < 1.5 are omitted (signal default applies).
    """
    import yaml

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "config_sizing_overrides.yaml")
    source_tag = "htf" if htf else "main"

    # Load existing to merge (max PF wins).
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = (yaml.safe_load(f) or {}).get("strategy_token_tf_sizes", {}) or {}
        except Exception as e:
            print(f"  warn: could not parse existing sizing overrides ({e}), starting fresh")
            existing = {}

    merged = {s: {t: dict(tfs) for t, tfs in toks.items()} for s, toks in existing.items()}

    for r, _lev, _wf in passing:
        pf = float(r.profit_factor)
        tier_info = _tier_for_pf(pf)
        if not tier_info:
            continue
        tier, size_pct = tier_info
        prior = merged.get(r.strategy, {}).get(r.token, {}).get(r.timeframe)
        if prior and prior.get("pf", 0) >= pf:
            continue  # existing entry has better/equal PF — keep it
        merged.setdefault(r.strategy, {}).setdefault(r.token, {})[r.timeframe] = {
            "tier": tier,
            "size_pct": size_pct,
            "pf": round(pf, 2),
            "source": source_tag,
        }

    header = (
        "# Auto-generated by backtesting/nightly.py write_sizing_overrides().\n"
        "# Regenerated after each nightly run (main + HTF merge; max PF per slot wins).\n"
        "# Tiers: A PF>=3.0 (20%), B PF 2.0-3.0 (15%), C PF 1.5-2.0 (10%).\n"
        "# Slots below PF 1.5 are omitted (signal's suggested size applies).\n"
        "# Final size is still capped by risk.max_position_size_percent in config.yaml.\n\n"
    )
    body = {
        "tiers": {"A": 20.0, "B": 15.0, "C": 10.0},
        "strategy_token_tf_sizes": merged,
    }
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump(body, f, sort_keys=True, default_flow_style=False)

    slots = sum(len(tfs) for toks in merged.values() for tfs in toks.values())
    print(f"  Sizing overrides updated: {slots} slots ({source_tag} run) → {path}")


def _insert_result(r: BacktestResult, leverage: float, run_id: str, bars: int,
                   wf: Optional[WalkForwardResult] = None):
    """Insert a single backtest result into the dashboard DB."""
    try:
        from app.database import insert_backtest

        if wf is not None:
            status = "pass" if wf.passed else "tested"
            wf_note = (
                f"WF: IS_PF={wf.in_sample.profit_factor:.2f} "
                f"OOS_PF={wf.out_of_sample.profit_factor:.2f} "
                f"Ret={wf.oos_pf_retention*100:.0f}% "
                f"IS_Trades={wf.in_sample.trade_count} "
                f"OOS_Trades={wf.out_of_sample.trade_count}"
            )
            fail_note = "" if wf.passed else " | WF_FAIL: " + "; ".join(wf.fail_reasons)
            notes = f"Run: {run_id} | Lev: {leverage:.1f}x | {wf_note}{fail_note}"
        else:
            status = "pass" if r.passed else "tested"
            notes = f"Run: {run_id} | Lev: {leverage:.1f}x | {'PASS' if r.passed else 'FAIL: ' + ', '.join(r.fail_reasons)}"

        bt = {
            "strategy_name": r.strategy,
            "version": "nightly",
            "timeframe": r.timeframe,
            "symbol": r.token,
            "initial_capital": 10000.0,
            "net_profit_pct": r.net_profit,
            "profit_factor": r.profit_factor,
            "total_trades": r.trade_count,
            "win_rate": r.win_rate,
            "max_drawdown": r.max_drawdown,
            "sharpe_ratio": r.sharpe_ratio,
            "source_file": f"nightly/{bars}bars",
            "notes": notes,
            "status": status,
            "suggested_leverage": leverage,
            "run_id": run_id,
            "avg_rr": r.avg_rr,
        }
        insert_backtest(bt)
    except Exception as e:
        print(f"  DB insert error for {r.strategy}/{r.token}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Nightly backtest runner")
    parser.add_argument("--bars", type=int, default=2000,
                        help="Number of bars per symbol (default: 2000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without inserting into DB")
    parser.add_argument("--include-regime", action="store_true",
                        help="Include regime-filtered strategy variants")
    parser.add_argument("--htf-filter", action="store_true",
                        help="Gate entries on higher-TF EMA(20) slope (4× current TF)")
    parser.add_argument("--with-shorts", action="store_true",
                        help="Include short signals (analysis only — live bot is long-only)")
    args = parser.parse_args()

    run_nightly(
        bars=args.bars,
        dry_run=args.dry_run,
        include_regime=args.include_regime,
        htf_filter=args.htf_filter,
        long_only=not args.with_shorts,
    )


if __name__ == "__main__":
    main()
