"""Multi-market batch runner + parameter sweeps for Phase B.

Sequential execution, but trade history is bulk-loaded ONCE per universe (one
DuckDB parquet scan), then reused across every config in a sweep. This makes
N-config × M-market sweeps cost ~M trade-scans, not N×M.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable, Optional

from .aggregate import Aggregate, aggregate
from .loader import (
    MarketMeta, Trade, find_finalized_markets, load_trades_for_many,
)
from .simulator import Result, SimConfig, simulate
from .strategies.spread import SpreadConfig, baseline


@dataclass
class BatchOutcome:
    cfg: SpreadConfig
    sim_cfg: SimConfig
    agg: Aggregate
    results: list[Result]


def run_batch_with_data(
    markets: list[MarketMeta],
    trades_by_ticker: dict[str, list[Trade]],
    cfg: SpreadConfig,
    sim_cfg: Optional[SimConfig] = None,
    progress: Optional[callable] = None,
) -> BatchOutcome:
    """Run one (strategy, sim) config pair across pre-loaded markets+trades."""
    sim_cfg = sim_cfg or SimConfig()
    n = len(markets)
    results: list[Result] = []
    strategy = baseline(cfg)
    for i, m in enumerate(markets):
        if progress:
            progress(i, n, m.ticker)
        trades = trades_by_ticker.get(m.ticker)
        if not trades:
            continue
        results.append(simulate(m, trades, strategy, sim_cfg))
    return BatchOutcome(cfg=cfg, sim_cfg=sim_cfg, agg=aggregate(results), results=results)


def expand_sweep(base: SpreadConfig, sweep: dict[str, list]) -> list[SpreadConfig]:
    """Cartesian-product expand sweep params over a base config.

    expand_sweep(SpreadConfig(), {"half_spread_cents": [1,2,3]}) returns 3
    SpreadConfigs differing only in that field.
    """
    if not sweep:
        return [base]
    keys = list(sweep.keys())
    vals = [sweep[k] for k in keys]
    out = []
    for combo in product(*vals):
        d = asdict(base)
        d.update(dict(zip(keys, combo)))
        out.append(SpreadConfig(**d))
    return out


def pick_universe(
    event_prefixes: list[str],
    per_prefix: int = 100,
    min_volume: int = 50_000,
) -> list[MarketMeta]:
    """Pick top-volume finalized markets across given event prefixes."""
    universe: list[MarketMeta] = []
    seen = set()
    for prefix in event_prefixes:
        for m in find_finalized_markets(
            event_prefix=prefix, min_volume=min_volume, limit=per_prefix
        ):
            if m.ticker not in seen:
                universe.append(m)
                seen.add(m.ticker)
    return universe


def load_universe_trades(markets: list[MarketMeta]) -> dict[str, list[Trade]]:
    """Bulk-load all trades for a universe in one parquet scan."""
    return load_trades_for_many([m.ticker for m in markets])
