"""
Backtest execution engine.

Simulates bar-by-bar trading from a signals DataFrame and returns performance metrics.
Commission and slippage applied on fill. Supports long-only or long+short mode.
"""

from dataclasses import dataclass
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
            "Status":    status,
        }


def run_backtest(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    token: str,
    strategy: str,
    timeframe: str,
    initial_capital: float = 10_000.0,
    commission: float = 0.001,   # 0.1% per trade
    slippage: float = 0.0005,    # 0.05% per fill
) -> BacktestResult:
    """
    Simulate trades from a signals DataFrame.

    signals columns expected: entry_long, exit_long, entry_short, exit_short (bool).
    Position sizing: 100% of equity per trade (replicate TradingView default).
    """
    close = df["close"].values
    n = len(close)

    entry_long  = signals["entry_long"].values
    exit_long   = signals["exit_long"].values
    entry_short = signals["entry_short"].values
    exit_short  = signals["exit_short"].values

    equity = initial_capital
    position = 0        # 1=long, -1=short, 0=flat
    entry_price = 0.0
    trade_pnls: list[float] = []
    equity_curve = [equity]

    fill_cost = commission + slippage  # applied twice (entry + exit)

    for i in range(1, n):
        price = close[i]

        # ── Close existing position ──────────────────────────────────────────
        if position == 1 and exit_long[i]:
            pnl_pct = (price / entry_price - 1) - 2 * fill_cost
            pnl = equity * pnl_pct
            equity += pnl
            trade_pnls.append(pnl)
            position = 0

        elif position == -1 and exit_short[i]:
            pnl_pct = (entry_price / price - 1) - 2 * fill_cost
            pnl = equity * pnl_pct
            equity += pnl
            trade_pnls.append(pnl)
            position = 0

        # ── Open new position ────────────────────────────────────────────────
        if position == 0:
            if entry_long[i]:
                position = 1
                entry_price = price * (1 + fill_cost)
            elif entry_short[i]:
                position = -1
                entry_price = price * (1 - fill_cost)

        equity_curve.append(equity)

    # Close open position at last bar
    if position == 1:
        pnl_pct = (close[-1] / entry_price - 1) - 2 * fill_cost
        pnl = equity * pnl_pct
        equity += pnl
        trade_pnls.append(pnl)
    elif position == -1:
        pnl_pct = (entry_price / close[-1] - 1) - 2 * fill_cost
        pnl = equity * pnl_pct
        equity += pnl
        trade_pnls.append(pnl)

    # ── Metrics ──────────────────────────────────────────────────────────────
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
        passed=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )
