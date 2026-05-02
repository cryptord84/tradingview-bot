"""Pure-function spread bot strategy for Phase A backtests.

Replicates the live `app/services/kalshi_spread_bot.py` quoting math without
the live bot's IO side effects (Kalshi API, DB writes, async loops). Diverges
from live code intentionally — keep this as a *minimal* reference quote model
so backtest sweeps can vary parameters cleanly.

Phase A scope: midpoint-anchored symmetric quotes, dead zone skip, optional
inventory skew. Selection logic and category filtering are NOT here — the
runner picks markets up front.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..loader import MarketMeta
from ..simulator import Quote, State


@dataclass
class SpreadConfig:
    half_spread_cents: int = 4       # quote at mid ± half_spread on each side
    contracts_per_side: int = 10     # quote size
    dead_zone_lo: int = 41           # skip quoting if mid in [lo, hi]
    dead_zone_hi: int = 50           # Becker 2026-04-28: 41-50 is true dead zone
    inventory_skew_cents: int = 0    # MVP: no skew (Phase B can enable)
    max_inventory_per_market: int = 50  # cap exposure per side


def baseline(cfg: Optional[SpreadConfig] = None):
    """Return a strategy fn for the simulator.

    The returned closure reads from `state` and `market` to decide quotes.
    """
    cfg = cfg or SpreadConfig()

    def _strategy(state: State, market: MarketMeta):
        mid = state.last_yes_price

        # Skip dead zone
        if cfg.dead_zone_lo <= mid <= cfg.dead_zone_hi:
            return None, None

        # Skip if either side is at the cap (don't accumulate one-sided risk)
        yes_room = max(0, cfg.max_inventory_per_market - state.yes_inv)
        no_room = max(0, cfg.max_inventory_per_market - state.no_inv)

        # Symmetric quote around mid
        yes_bid = mid - cfg.half_spread_cents
        no_bid = (100 - mid) - cfg.half_spread_cents

        # Inventory skew: lean prices to encourage flattening
        if cfg.inventory_skew_cents:
            imbalance = state.yes_inv - state.no_inv
            # Positive imbalance (long YES) → lower YES_bid, raise NO_bid
            yes_bid -= min(cfg.inventory_skew_cents, max(0, imbalance // 5))
            no_bid += min(cfg.inventory_skew_cents, max(0, imbalance // 5))
            no_bid -= min(cfg.inventory_skew_cents, max(0, -imbalance // 5))
            yes_bid += min(cfg.inventory_skew_cents, max(0, -imbalance // 5))

        # Clamp to legal price range
        yes_bid = max(1, min(99, yes_bid))
        no_bid = max(1, min(99, no_bid))

        # Build quotes (skip a leg if we're at inventory cap)
        yes_q = Quote("yes", yes_bid, min(cfg.contracts_per_side, yes_room)) if yes_room else None
        no_q = Quote("no", no_bid, min(cfg.contracts_per_side, no_room)) if no_room else None

        return yes_q, no_q

    return _strategy
