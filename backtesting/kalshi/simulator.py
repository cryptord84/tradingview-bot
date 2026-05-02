"""Walk-forward maker-side fill simulator for Kalshi binary markets.

Phase C realism layers (all default-off for backwards compatibility):

- `fee_per_contract_cents`: per-contract fee deducted on each fill (entry side).
  This is the cleanest signal — direct cost, scales linearly with fills.

- `fill_rate`: proportional-fill haircut, 0..1. Each optimistic fill is reduced
  to `fill_rate × size`. Models queue-priority loss (we sit behind other
  makers and only get a fraction of each taker order). Probabilistic-drop
  was the previous model; rejected because the per-market inventory cap
  masks the haircut effect.
  CAVEAT: `fill_rate` and `max_inventory_per_market` interact non-linearly.
  Smaller fill_rate → smaller per-fill qty → more fills before cap → more
  fee notches and total contracts. Read the calibration table directionally,
  not as a precise P&L predictor.

- Adverse-selection tracking: count fills where the next N trades drift
  AGAINST us by ≥ threshold. Reported as a metric (not deducted from P&L —
  the price drift already shows up in settlement).

Optimistic-fill model: every taker trade that crosses our resting quote price
fills us at our quoted price, before haircut. Ignores price-time priority.

Kalshi binary mechanics:
- 1 YES contract pays 100¢ if outcome=yes, else 0¢
- 1 NO contract pays 100¢ if outcome=no, else 0¢
- yes_price + no_price = 100¢ at every trade

Fill semantics for a maker quoting YES_bid + NO_bid:
- A taker buying NO at no_price (taker_side='no') means a maker on the other
  side bought YES at yes_price = 100 - no_price. Our YES_bid at p_y fills if
  yes_price <= p_y (under price-time priority assumption).
- A taker buying YES at yes_price (taker_side='yes') means a maker on the
  other side bought NO at no_price = 100 - yes_price. Our NO_bid at p_n
  fills if no_price <= p_n  ⇔  yes_price >= 100 - p_n.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .loader import MarketMeta, Trade


@dataclass
class SimConfig:
    """Realism knobs applied by the simulator (independent of strategy).

    Fee model precedence: if `fee_formula_pct > 0`, use Kalshi's variable formula
    `ceil(fee_formula_pct/100 × qty × P × (1-P) × 100)` cents per fill (P = price as
    decimal). Else use the flat `fee_per_contract_cents × qty`. Kalshi's published
    rates are 7% for sports markets and 3.5% for non-sports — set the field to 7.0
    or 3.5 accordingly. Defaults to flat (legacy) for backwards compatibility.
    """
    fee_per_contract_cents: int = 0    # flat-mode fee; spread_bot live uses 2
    fee_formula_pct: float = 0.0       # set to 7.0 (sports) or 3.5 (other) to enable formula
    fill_rate: float = 1.0             # 1.0 = optimistic; 0.3 = capture 30% of fill qty
    adverse_lookahead: int = 5         # # trades after a fill to inspect for adverse drift
    adverse_threshold_cents: int = 1   # ≥ this drift against us in lookahead = adverse


def compute_fill_fee_cents(qty: int, price_cents: int, sim_cfg: SimConfig) -> int:
    """Per-fill fee in cents.

    formula mode: ceil(fee_formula_pct × qty × P × (1-P)) cents
      where P = price_cents / 100. Kalshi rounds UP to the nearest cent per fill.
      At p=50¢ the formula gives 25 × 0.07 = 1.75¢/contract (max), shrinking to ~0
      at the tails. So tail prices pay much less than mid prices.
    flat mode: qty × fee_per_contract_cents.
    """
    if qty <= 0:
        return 0
    if sim_cfg.fee_formula_pct > 0:
        p = price_cents / 100.0
        raw = sim_cfg.fee_formula_pct * qty * p * (1 - p)
        return max(0, math.ceil(raw))  # cents, rounded up
    return qty * sim_cfg.fee_per_contract_cents


@dataclass
class Quote:
    side: str   # 'yes' or 'no' — which side of the book this bid is on
    price: int  # cents 1-99 we'll pay to BUY this side
    size: int   # contracts we're willing to absorb at this price


@dataclass
class Fill:
    ts: datetime
    side: str   # 'yes' or 'no' — what we acquired
    price: int  # cents we paid per contract
    qty: int


@dataclass
class State:
    yes_inv: int = 0
    no_inv: int = 0
    yes_cost_total: int = 0  # cumulative cents paid for YES contracts
    no_cost_total: int = 0   # cumulative cents paid for NO contracts
    last_yes_price: int = 50  # last observed market YES price
    cur_yes_bid: Optional[Quote] = None
    cur_no_bid: Optional[Quote] = None
    fills: list[Fill] = field(default_factory=list)


@dataclass
class Result:
    market_ticker: str
    settle_result: str
    n_trades_replayed: int
    n_fills: int
    n_adverse_fills: int   # fills picked off by next-N-trades drift
    fees_cents: int        # cumulative fees deducted
    yes_inv: int
    no_inv: int
    invested_cents: int    # gross cost basis (excludes fees)
    settled_payout_cents: int
    pnl_cents: int         # net of fees
    pnl_pct: float
    fills_per_1k_trades: float

    def summary(self) -> str:
        adv_pct = (100.0 * self.n_adverse_fills / self.n_fills) if self.n_fills else 0.0
        return (
            f"{self.market_ticker} → {self.settle_result.upper()}\n"
            f"  trades replayed:  {self.n_trades_replayed:,}\n"
            f"  fills:            {self.n_fills}  ({self.fills_per_1k_trades:.1f}/1k)\n"
            f"  adverse fills:    {self.n_adverse_fills}  ({adv_pct:.1f}%)\n"
            f"  inventory:        {self.yes_inv} YES, {self.no_inv} NO\n"
            f"  invested:         ${self.invested_cents/100:.2f}\n"
            f"  fees:             ${self.fees_cents/100:.2f}\n"
            f"  settled:          ${self.settled_payout_cents/100:.2f}\n"
            f"  P&L (net of fees):${self.pnl_cents/100:+.2f}  ({self.pnl_pct:+.1%})"
        )


# Strategy signature: (state, market_meta) -> (yes_bid_quote_or_None, no_bid_quote_or_None)
StrategyFn = Callable[[State, MarketMeta], tuple[Optional[Quote], Optional[Quote]]]


def simulate(
    market: MarketMeta,
    trades: list[Trade],
    strategy: StrategyFn,
    sim_cfg: Optional[SimConfig] = None,
) -> Result:
    """Walk forward through trades, posting quotes and recording fills.

    Order of operations per trade is critical: the strategy quotes based on the
    LAST observed price (no look-ahead), then we check whether the current trade
    crosses our resting quotes, then we update the price for the next loop.
    Setting state.last_yes_price BEFORE strategy() is the look-ahead bug that
    makes every sweep config produce identical results.
    """
    sim_cfg = sim_cfg or SimConfig()
    state = State()

    # Pre-compute future-window price drift for adverse-selection metric
    # (cheap: simple slice + mean inside the loop).
    yes_prices = [t.yes_price for t in trades]

    # Initial quote: warm up with first trade's price so we have something resting.
    if trades:
        state.last_yes_price = trades[0].yes_price
    yes_q, no_q = strategy(state, market)
    state.cur_yes_bid = yes_q
    state.cur_no_bid = no_q

    fees_cents = 0
    n_adverse = 0
    haircut = max(0.0, min(1.0, sim_cfg.fill_rate))

    def _haircut_qty(qty: int) -> int:
        """Scale fill qty by haircut, rounding to keep small caps usable."""
        if haircut >= 1.0:
            return qty
        if qty <= 0:
            return 0
        scaled = int(qty * haircut)
        return scaled if scaled > 0 else (1 if haircut > 0 else 0)

    for i, t in enumerate(trades):
        # 1. Check fill against the quotes that were resting BEFORE this trade.
        filled_side: Optional[str] = None
        filled_price: int = 0
        filled_qty: int = 0

        if t.taker_side == "no" and yes_q and t.yes_price <= yes_q.price:
            optimistic = min(t.count, yes_q.size)
            filled_qty = _haircut_qty(optimistic)
            if filled_qty > 0:
                filled_side = "yes"
                filled_price = yes_q.price
        elif t.taker_side == "yes" and no_q and t.no_price <= no_q.price:
            optimistic = min(t.count, no_q.size)
            filled_qty = _haircut_qty(optimistic)
            if filled_qty > 0:
                filled_side = "no"
                filled_price = no_q.price

        if filled_side:
            if filled_side == "yes":
                state.yes_inv += filled_qty
                state.yes_cost_total += filled_qty * filled_price
            else:
                state.no_inv += filled_qty
                state.no_cost_total += filled_qty * filled_price
            state.fills.append(Fill(t.ts, filled_side, filled_price, filled_qty))
            fees_cents += compute_fill_fee_cents(filled_qty, filled_price, sim_cfg)

            # Adverse-selection check: do the next N trades drift against us?
            window = yes_prices[i + 1 : i + 1 + sim_cfg.adverse_lookahead]
            if window:
                future_avg = sum(window) / len(window)
                if filled_side == "yes":
                    # We bought YES at filled_price. Adverse if avg drifts BELOW.
                    if future_avg <= filled_price - sim_cfg.adverse_threshold_cents:
                        n_adverse += 1
                else:
                    # We bought NO. Adverse if YES drifts UP (NO drifts down).
                    # Our NO cost was filled_price; future NO ≈ 100 - future_avg.
                    if (100 - future_avg) <= filled_price - sim_cfg.adverse_threshold_cents:
                        n_adverse += 1

        # 2. Update price reference and re-quote for the NEXT trade.
        state.last_yes_price = t.yes_price
        yes_q, no_q = strategy(state, market)
        state.cur_yes_bid = yes_q
        state.cur_no_bid = no_q

    # Settlement
    payout = (state.yes_inv * 100) if market.result == "yes" else 0
    payout += (state.no_inv * 100) if market.result == "no" else 0
    invested = state.yes_cost_total + state.no_cost_total
    pnl = payout - invested - fees_cents
    pnl_pct = (pnl / invested) if invested > 0 else 0.0
    n_trades = len(trades)

    return Result(
        market_ticker=market.ticker,
        settle_result=market.result,
        n_trades_replayed=n_trades,
        n_fills=len(state.fills),
        n_adverse_fills=n_adverse,
        fees_cents=fees_cents,
        yes_inv=state.yes_inv,
        no_inv=state.no_inv,
        invested_cents=invested,
        settled_payout_cents=payout,
        pnl_cents=pnl,
        pnl_pct=pnl_pct,
        fills_per_1k_trades=(1000 * len(state.fills) / n_trades) if n_trades else 0.0,
    )
