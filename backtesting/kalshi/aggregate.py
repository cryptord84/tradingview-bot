"""Aggregate per-market backtest results into summary stats."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Iterable

from .simulator import Result


@dataclass
class Aggregate:
    n_markets: int
    n_with_fills: int
    total_pnl_cents: int
    total_invested_cents: int
    total_fills: int
    total_adverse_fills: int
    total_fees_cents: int
    mean_pnl_cents: float
    median_pnl_cents: float
    win_rate_pct: float          # % of markets where pnl > 0
    sharpe_like: float           # mean / stdev across markets (per-market)
    max_drawdown_cents: int       # peak-to-trough on cumulative P&L (markets in load order)
    roi_pct: float               # total_pnl / total_invested
    adverse_pct: float           # adverse fills / total fills

    def summary_lines(self) -> list[str]:
        wr_pct = f"{self.win_rate_pct:.1f}%"
        roi = f"{self.roi_pct:+.2f}%"
        return [
            f"  Markets:           {self.n_markets} ({self.n_with_fills} with fills)",
            f"  Total fills:       {self.total_fills:,}  (adverse: {self.total_adverse_fills:,} / {self.adverse_pct:.1f}%)",
            f"  Total invested:    ${self.total_invested_cents/100:,.2f}",
            f"  Total fees:        ${self.total_fees_cents/100:,.2f}",
            f"  Total P&L (net):   ${self.total_pnl_cents/100:+,.2f}  ROI {roi}",
            f"  Mean P&L/market:   ${self.mean_pnl_cents/100:+.2f}",
            f"  Median P&L/market: ${self.median_pnl_cents/100:+.2f}",
            f"  Win rate:          {wr_pct}",
            f"  Sharpe-like:       {self.sharpe_like:.2f}",
            f"  Max drawdown:      ${self.max_drawdown_cents/100:.2f}",
        ]


def aggregate(results: Iterable[Result]) -> Aggregate:
    rs = list(results)
    n = len(rs)
    if not n:
        return Aggregate(0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)

    pnls = [r.pnl_cents for r in rs]
    invested = sum(r.invested_cents for r in rs)
    fills = sum(r.n_fills for r in rs)
    adv = sum(r.n_adverse_fills for r in rs)
    fees = sum(r.fees_cents for r in rs)
    n_with_fills = sum(1 for r in rs if r.n_fills > 0)
    wins = sum(1 for p in pnls if p > 0)
    sd = pstdev(pnls) if n > 1 else 0
    mu = mean(pnls)
    sharpe = (mu / sd) if sd > 0 else 0.0

    # Max drawdown of cumulative P&L
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    return Aggregate(
        n_markets=n,
        n_with_fills=n_with_fills,
        total_pnl_cents=sum(pnls),
        total_invested_cents=invested,
        total_fills=fills,
        total_adverse_fills=adv,
        total_fees_cents=fees,
        mean_pnl_cents=mu,
        median_pnl_cents=median(pnls),
        win_rate_pct=(100.0 * wins / n),
        sharpe_like=sharpe,
        max_drawdown_cents=max_dd,
        roi_pct=(100.0 * sum(pnls) / invested) if invested > 0 else 0.0,
        adverse_pct=(100.0 * adv / fills) if fills else 0.0,
    )
