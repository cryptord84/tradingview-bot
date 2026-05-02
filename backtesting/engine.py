"""
Backtest execution engine.

Simulates bar-by-bar trading from a signals DataFrame and returns performance metrics.
Commission and slippage applied on fill. Supports long-only or long+short mode.

Risk management features:
- ATR-based stop-loss and take-profit
- Position sizing (risk % of equity per trade)
- Trailing stop-loss (ratchets up after activation)
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# Thresholds for pass/fail evaluation
THRESHOLDS = {
    "profit_factor": 1.40,
    "win_rate":      30.0,   # percent
    "trade_count":   30,
    "max_drawdown":  35.0,   # percent (must be BELOW this)
    "net_profit":    0.0,    # percent (must be ABOVE this)
}


@dataclass
class RiskConfig:
    """Risk management parameters for the backtest engine.

    Defaults mirror config.yaml position_monitor globals so backtest evaluations
    match live engine behavior. Per-slot overrides (config_tpsl_overrides.yaml)
    and per-token SL overrides are layered on top via risk_for() — see nightly.py.
    """
    # Position sizing: risk this % of equity per trade
    risk_per_trade_pct: float = 2.0
    # ATR-based stop-loss multiplier (SL = entry ± atr_sl_mult * ATR)
    atr_sl_mult: float = 1.5
    # ATR-based take-profit multiplier (live default in config.yaml is 4.0)
    atr_tp_mult: float = 4.0
    # Trailing stop: activate after price moves this many ATRs in profit
    trail_activation_atr: float = 1.5
    # Trailing offset — live widened to 2.0 on 2026-04-18 (was clipping winners early)
    trail_offset_atr: float = 2.0
    # Enable trailing stop
    trail_enabled: bool = True
    # Use ATR-based SL/TP (if False, rely purely on strategy exit signals)
    use_atr_stops: bool = True


# Default: no risk management (legacy behavior for comparison)
LEGACY_RISK = RiskConfig(
    risk_per_trade_pct=100.0,
    use_atr_stops=False,
    trail_enabled=False,
)

# Recommended risk config
DEFAULT_RISK = RiskConfig()


@dataclass
class BacktestResult:
    token: str
    strategy: str
    timeframe: str
    profit_factor: float
    win_rate: float
    trade_count: int
    max_drawdown: float
    net_profit: float
    passed: bool
    fail_reasons: list[str]
    sharpe_ratio: float = 0.0
    avg_rr: float = 0.0  # average risk:reward achieved

    def summary_row(self) -> dict:
        status = "PASS" if self.passed else f"FAIL ({', '.join(self.fail_reasons)})"
        return {
            "Token":     self.token,
            "Strategy":  self.strategy,
            "TF":        self.timeframe,
            "PF":        f"{self.profit_factor:.2f}",
            "WR%":       f"{self.win_rate:.1f}",
            "Trades":    str(self.trade_count),
            "MaxDD%":    f"{self.max_drawdown:.1f}",
            "NetPft%":   f"{self.net_profit:.1f}",
            "Sharpe":    f"{self.sharpe_ratio:.2f}",
            "AvgRR":     f"{self.avg_rr:.2f}",
            "Status":    status,
        }


def run_backtest(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    token: str,
    strategy: str,
    timeframe: str,
    initial_capital: float = 10_000.0,
    commission: float = 0.002,   # 0.2% per trade — Jupiter realistic
    slippage: float = 0.001,     # 0.1% per fill — SOL/meme realistic
    risk: Optional[RiskConfig] = None,
    atr_series: Optional[pd.Series] = None,
) -> BacktestResult:
    """
    Simulate trades from a signals DataFrame with risk management.

    signals columns expected: entry_long, exit_long, entry_short, exit_short (bool).

    When risk.use_atr_stops is True, ATR-based SL/TP are applied per-trade.
    Position size = (equity * risk_per_trade_pct%) / (atr_sl_mult * ATR).
    """
    if risk is None:
        risk = DEFAULT_RISK

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(close)

    # Compute ATR if not provided and risk management needs it
    if atr_series is None and risk.use_atr_stops:
        from backtesting.indicators import atr as calc_atr
        atr_series = calc_atr(df["high"], df["low"], df["close"], period=14)

    atr_vals = atr_series.values if atr_series is not None else np.zeros(n)

    entry_long  = signals["entry_long"].values
    exit_long   = signals["exit_long"].values
    entry_short = signals["entry_short"].values
    exit_short  = signals["exit_short"].values

    equity = initial_capital
    position = 0        # 1=long, -1=short, 0=flat
    entry_price = 0.0
    position_size = 0.0  # number of units held
    sl_price = 0.0
    tp_price = 0.0
    trail_sl = 0.0
    trail_active = False
    current_atr = 0.0    # ATR at entry
    trade_pnls: list[float] = []
    trade_rrs: list[float] = []  # risk:reward ratios achieved
    equity_curve = [equity]

    fill_cost = commission + slippage

    for i in range(1, n):
        price = close[i]
        bar_high = high[i]
        bar_low = low[i]

        # ── Check SL/TP hits on current bar (using high/low for intra-bar) ──
        if position == 1 and risk.use_atr_stops:
            effective_sl = max(sl_price, trail_sl) if risk.trail_enabled else sl_price

            # Check SL first (worst case)
            if bar_low <= effective_sl:
                exit_at = effective_sl
                pnl_pct = (exit_at / entry_price - 1) - 2 * fill_cost
                pnl = position_size * exit_at * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
                trade_pnls.append(pnl)
                _record_rr(trade_rrs, entry_price, exit_at, sl_price)
                equity += pnl
                position = 0
                trail_sl = 0.0
                trail_active = False
                equity_curve.append(equity)
                continue

            # Check TP
            if bar_high >= tp_price:
                exit_at = tp_price
                pnl_pct = (exit_at / entry_price - 1) - 2 * fill_cost
                pnl = position_size * exit_at * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
                trade_pnls.append(pnl)
                _record_rr(trade_rrs, entry_price, exit_at, sl_price)
                equity += pnl
                position = 0
                trail_sl = 0.0
                trail_active = False
                equity_curve.append(equity)
                continue

            # Update trailing stop
            if risk.trail_enabled and current_atr > 0:
                activation_price = entry_price + (risk.trail_activation_atr * current_atr)
                if bar_high >= activation_price:
                    trail_active = True
                if trail_active:
                    new_trail = price - (risk.trail_offset_atr * current_atr)
                    if new_trail > trail_sl:
                        trail_sl = new_trail

        elif position == -1 and risk.use_atr_stops:
            effective_sl = min(sl_price, trail_sl) if (risk.trail_enabled and trail_sl > 0) else sl_price

            if bar_high >= effective_sl:
                exit_at = effective_sl
                pnl_pct = (entry_price / exit_at - 1) - 2 * fill_cost
                pnl = position_size * exit_at * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
                trade_pnls.append(pnl)
                _record_rr(trade_rrs, entry_price, exit_at, sl_price)
                equity += pnl
                position = 0
                trail_sl = 0.0
                trail_active = False
                equity_curve.append(equity)
                continue

            if bar_low <= tp_price:
                exit_at = tp_price
                pnl_pct = (entry_price / exit_at - 1) - 2 * fill_cost
                pnl = position_size * exit_at * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
                trade_pnls.append(pnl)
                _record_rr(trade_rrs, entry_price, exit_at, sl_price)
                equity += pnl
                position = 0
                trail_sl = 0.0
                trail_active = False
                equity_curve.append(equity)
                continue

            # Trailing stop for shorts
            if risk.trail_enabled and current_atr > 0:
                activation_price = entry_price - (risk.trail_activation_atr * current_atr)
                if bar_low <= activation_price:
                    trail_active = True
                if trail_active:
                    new_trail = price + (risk.trail_offset_atr * current_atr)
                    if trail_sl == 0 or new_trail < trail_sl:
                        trail_sl = new_trail

        # ── Close on strategy signal (works with or without ATR stops) ──
        if position == 1 and exit_long[i]:
            pnl_pct = (price / entry_price - 1) - 2 * fill_cost
            pnl = position_size * price * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
            trade_pnls.append(pnl)
            _record_rr(trade_rrs, entry_price, price, sl_price)
            equity += pnl
            position = 0
            trail_sl = 0.0
            trail_active = False

        elif position == -1 and exit_short[i]:
            pnl_pct = (entry_price / price - 1) - 2 * fill_cost
            pnl = position_size * price * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
            trade_pnls.append(pnl)
            _record_rr(trade_rrs, entry_price, price, sl_price)
            equity += pnl
            position = 0
            trail_sl = 0.0
            trail_active = False

        # ── Open new position ────────────────────────────────────────────
        if position == 0:
            if entry_long[i]:
                position = 1
                entry_price = price * (1 + fill_cost)
                current_atr = atr_vals[i] if atr_vals[i] > 0 else 0

                if risk.use_atr_stops and current_atr > 0:
                    sl_price = entry_price - (risk.atr_sl_mult * current_atr)
                    tp_price = entry_price + (risk.atr_tp_mult * current_atr)

                    # Position sizing: risk X% of equity
                    risk_per_unit = entry_price - sl_price  # dollar risk per unit
                    risk_amount = equity * (risk.risk_per_trade_pct / 100)
                    position_size = risk_amount / risk_per_unit if risk_per_unit > 0 else 0
                else:
                    sl_price = 0
                    tp_price = float("inf")
                    position_size = equity / entry_price if entry_price > 0 else 0

                trail_sl = 0.0
                trail_active = False

            elif entry_short[i]:
                position = -1
                entry_price = price * (1 - fill_cost)
                current_atr = atr_vals[i] if atr_vals[i] > 0 else 0

                if risk.use_atr_stops and current_atr > 0:
                    sl_price = entry_price + (risk.atr_sl_mult * current_atr)
                    tp_price = entry_price - (risk.atr_tp_mult * current_atr)

                    risk_per_unit = sl_price - entry_price
                    risk_amount = equity * (risk.risk_per_trade_pct / 100)
                    position_size = risk_amount / risk_per_unit if risk_per_unit > 0 else 0
                else:
                    sl_price = float("inf")
                    tp_price = 0
                    position_size = equity / entry_price if entry_price > 0 else 0

                trail_sl = 0.0
                trail_active = False

        equity_curve.append(equity)

    # Close open position at last bar
    if position == 1:
        pnl_pct = (close[-1] / entry_price - 1) - 2 * fill_cost
        pnl = position_size * close[-1] * pnl_pct if risk.risk_per_trade_pct < 100 else equity * pnl_pct
        equity += pnl
        trade_pnls.append(pnl)
    elif position == -1:
        pnl_pct = (entry_price / close[-1] - 1) - 2 * fill_cost
        pnl = position_size * close[-1] * abs(pnl_pct) if risk.risk_per_trade_pct < 100 else equity * pnl_pct
        equity += pnl
        trade_pnls.append(pnl)

    # ── Metrics ──────────────────────────────────────────────────────────
    trade_count = len(trade_pnls)

    if trade_count == 0:
        return BacktestResult(
            token=token, strategy=strategy, timeframe=timeframe,
            profit_factor=0.0, win_rate=0.0, trade_count=0,
            max_drawdown=0.0, net_profit=0.0,
            passed=False, fail_reasons=["no trades"],
        )

    wins  = [p for p in trade_pnls if p > 0]
    loss  = [p for p in trade_pnls if p <= 0]

    gross_profit = sum(wins) if wins else 0.0
    gross_loss   = abs(sum(loss)) if loss else 0.0

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    win_rate      = len(wins) / trade_count * 100
    net_profit    = (equity / initial_capital - 1) * 100

    # Max drawdown from equity curve
    eq_arr  = np.array(equity_curve)
    peak    = np.maximum.accumulate(eq_arr)
    dd      = (peak - eq_arr) / peak * 100
    max_dd  = float(dd.max())

    # Sharpe ratio (annualized, assuming ~252 trading days)
    returns = np.diff(eq_arr) / eq_arr[:-1]
    sharpe = 0.0
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252))

    # Average R:R achieved
    avg_rr = float(np.mean(trade_rrs)) if trade_rrs else 0.0

    # Pass/fail evaluation
    fail_reasons = []
    if profit_factor < THRESHOLDS["profit_factor"]:
        fail_reasons.append(f"PF {profit_factor:.2f}")
    if win_rate < THRESHOLDS["win_rate"]:
        fail_reasons.append(f"WR {win_rate:.1f}%")
    if trade_count < THRESHOLDS["trade_count"]:
        fail_reasons.append(f"{trade_count} trades")
    if max_dd >= THRESHOLDS["max_drawdown"]:
        fail_reasons.append(f"DD {max_dd:.1f}%")
    if net_profit <= THRESHOLDS["net_profit"]:
        fail_reasons.append(f"NP {net_profit:.1f}%")

    return BacktestResult(
        token=token, strategy=strategy, timeframe=timeframe,
        profit_factor=round(profit_factor, 2),
        win_rate=round(win_rate, 1),
        trade_count=trade_count,
        max_drawdown=round(max_dd, 1),
        net_profit=round(net_profit, 1),
        sharpe_ratio=round(sharpe, 2),
        avg_rr=round(avg_rr, 2),
        passed=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )


def _record_rr(trade_rrs: list, entry: float, exit_price: float, sl: float) -> None:
    """Record the realized risk:reward ratio for a trade."""
    risk = abs(entry - sl) if sl > 0 else 0
    reward = abs(exit_price - entry)
    if risk > 0:
        trade_rrs.append(reward / risk)


@dataclass
class WalkForwardResult:
    """Result of a walk-forward split backtest."""
    in_sample: BacktestResult
    out_of_sample: BacktestResult
    combined: BacktestResult      # full-window result (for reference/leverage sizing)
    passed: bool                  # True only if both IS and OOS pass AND OOS doesn't collapse
    fail_reasons: list[str]

    @property
    def oos_pf_retention(self) -> float:
        """OOS PF as a fraction of IS PF. 1.0 = OOS matches IS, <0.5 = severe degradation."""
        if self.in_sample.profit_factor <= 0:
            return 0.0
        return self.out_of_sample.profit_factor / self.in_sample.profit_factor


def run_walkforward(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    token: str,
    strategy: str,
    timeframe: str,
    split_pct: float = 0.7,
    min_oos_pf_retention: float = 0.6,
    min_oos_pf_absolute: float = 1.2,
    risk: Optional[RiskConfig] = None,
    atr_series: Optional[pd.Series] = None,
    **backtest_kwargs,
) -> WalkForwardResult:
    """
    Run backtest as a walk-forward split to detect overfitting.

    Splits the data at `split_pct` (default 70/30). Runs the engine on each segment
    independently. A combo is only marked passed if:
      1. Both in-sample AND out-of-sample segments pass the base thresholds.
      2. OOS profit factor retains >= min_oos_pf_retention of IS profit factor
         (e.g., 0.6 means OOS PF must be at least 60% of IS PF).
      3. OOS PF is at least min_oos_pf_absolute (guards against IS also being weak).

    The `combined` result is the full-window run, kept for leverage sizing and
    dashboard display. Only use `passed` from WalkForwardResult for deploy decisions.
    """
    n = len(df)
    split_idx = int(n * split_pct)

    # Guard: need enough bars on each side for meaningful stats
    min_bars = 200
    if split_idx < min_bars or (n - split_idx) < min_bars:
        # Not enough data for a real split — run combined only, mark as failed WF
        combined = run_backtest(
            df, signals, token, strategy, timeframe,
            risk=risk, atr_series=atr_series, **backtest_kwargs,
        )
        return WalkForwardResult(
            in_sample=combined,
            out_of_sample=combined,
            combined=combined,
            passed=False,
            fail_reasons=[f"insufficient bars for WF ({n} total)"],
        )

    df_is = df.iloc[:split_idx].reset_index(drop=True)
    df_oos = df.iloc[split_idx:].reset_index(drop=True)
    sig_is = signals.iloc[:split_idx].reset_index(drop=True)
    sig_oos = signals.iloc[split_idx:].reset_index(drop=True)

    atr_is = atr_series.iloc[:split_idx].reset_index(drop=True) if atr_series is not None else None
    atr_oos = atr_series.iloc[split_idx:].reset_index(drop=True) if atr_series is not None else None

    is_result = run_backtest(
        df_is, sig_is, token, strategy, timeframe,
        risk=risk, atr_series=atr_is, **backtest_kwargs,
    )
    oos_result = run_backtest(
        df_oos, sig_oos, token, strategy, timeframe,
        risk=risk, atr_series=atr_oos, **backtest_kwargs,
    )
    combined = run_backtest(
        df, signals, token, strategy, timeframe,
        risk=risk, atr_series=atr_series, **backtest_kwargs,
    )

    fail_reasons: list[str] = []

    # Trade count is a sample-size gate; apply it to the combined window only.
    # Per-segment, only require quality thresholds (PF/WR/DD/NP).
    def _quality_reasons(r: BacktestResult) -> list[str]:
        return [x for x in r.fail_reasons if not x.endswith(" trades")]

    is_quality_reasons = _quality_reasons(is_result)
    oos_quality_reasons = _quality_reasons(oos_result)
    if is_quality_reasons:
        fail_reasons.append(f"IS fail: {','.join(is_quality_reasons)}")
    if oos_quality_reasons:
        fail_reasons.append(f"OOS fail: {','.join(oos_quality_reasons)}")

    if combined.trade_count < THRESHOLDS["trade_count"]:
        fail_reasons.append(
            f"combined {combined.trade_count} trades < {THRESHOLDS['trade_count']}"
        )

    retention = 0.0
    if is_result.profit_factor > 0:
        retention = oos_result.profit_factor / is_result.profit_factor
        if retention < min_oos_pf_retention:
            fail_reasons.append(
                f"OOS PF retention {retention:.0%} < {min_oos_pf_retention:.0%}"
            )

    if oos_result.profit_factor < min_oos_pf_absolute:
        fail_reasons.append(
            f"OOS PF {oos_result.profit_factor:.2f} < {min_oos_pf_absolute:.2f}"
        )

    return WalkForwardResult(
        in_sample=is_result,
        out_of_sample=oos_result,
        combined=combined,
        passed=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )


# ── Live-engine TP/SL resolution (mirrors trade_engine._resolve_tp_sl) ────────
import os as _os
from pathlib import Path as _Path

_PROJECT_ROOT = _Path(__file__).resolve().parents[1]
_TPSL_PATH = _PROJECT_ROOT / "config_tpsl_overrides.yaml"
_LIVE_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

_tpsl_cache: Optional[dict] = None
_token_overrides_cache: Optional[dict] = None


def _load_tpsl_overrides() -> dict:
    global _tpsl_cache
    if _tpsl_cache is not None:
        return _tpsl_cache
    try:
        import yaml
        with open(_TPSL_PATH) as f:
            data = yaml.safe_load(f) or {}
        _tpsl_cache = data.get("strategy_token_tf_overrides", {}) or {}
    except Exception:
        _tpsl_cache = {}
    return _tpsl_cache


def _load_token_sl_overrides() -> dict:
    """Per-token SL multiplier overrides from config.yaml position_monitor.token_overrides."""
    global _token_overrides_cache
    if _token_overrides_cache is not None:
        return _token_overrides_cache
    try:
        import yaml
        with open(_LIVE_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        _token_overrides_cache = (data.get("position_monitor", {}) or {}).get("token_overrides", {}) or {}
    except Exception:
        _token_overrides_cache = {}
    return _token_overrides_cache


def risk_for(strategy: str, token: str, timeframe: str,
             base: Optional[RiskConfig] = None) -> RiskConfig:
    """Return a RiskConfig for (strategy, token, tf) with live-engine override resolution.

    Precedence (matches trade_engine._resolve_tp_sl):
      1. Per-slot sweep override (config_tpsl_overrides.yaml) — wins outright if matched
      2. Per-token SL override (config.yaml position_monitor.token_overrides) — SL only
      3. base defaults (DEFAULT_RISK)

    Token lookup strips USD/USDT suffix and uses the bare ticker (e.g. RENDERUSDT → RENDER).
    """
    if base is None:
        base = DEFAULT_RISK

    # Strip exchange suffix to match live engine's token-key normalization
    bare = token.replace("USDT", "").replace("USD", "")

    sl_mult = base.atr_sl_mult
    tp_mult = base.atr_tp_mult

    # 2. Per-token SL override
    token_overrides = _load_token_sl_overrides()
    if bare in token_overrides:
        sl_mult = token_overrides[bare].get("sl_multiplier", sl_mult)

    # 1. Per-slot sweep — wins for matched slot
    tpsl = _load_tpsl_overrides()
    slot = tpsl.get(strategy, {}).get(bare, {}).get(timeframe)
    if slot:
        tp_mult = slot.get("atr_tp_mult", tp_mult)
        sl_mult = slot.get("atr_sl_mult", sl_mult)

    # Build a copy of base with overrides applied
    return RiskConfig(
        risk_per_trade_pct=base.risk_per_trade_pct,
        atr_sl_mult=sl_mult,
        atr_tp_mult=tp_mult,
        trail_activation_atr=base.trail_activation_atr,
        trail_offset_atr=base.trail_offset_atr,
        trail_enabled=base.trail_enabled,
        use_atr_stops=base.use_atr_stops,
    )
