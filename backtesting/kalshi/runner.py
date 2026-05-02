"""Phase A runner: single-market end-to-end backtest.

Usage:
  venv/bin/python -m backtesting.kalshi.runner KXNBAGAME-25OCT21HOUOKC-HOU
  venv/bin/python -m backtesting.kalshi.runner --pick-top sports
"""

from __future__ import annotations

import argparse
import sys
import time

from .loader import find_finalized_markets, load_market, load_trades
from .simulator import simulate
from .strategies.spread import SpreadConfig, baseline


PICK_PRESETS = {
    "sports": "KXNBAGAME",
    "nba": "KXNBAGAME",
    "nfl": "KXNFLGAME",
    "mlb": "KXMLBGAME",
    "nhl": "KXNHLGAME",
}


def _print_strategy_block(cfg: SpreadConfig):
    print("Strategy: spread baseline")
    print(f"  half_spread_cents:        {cfg.half_spread_cents}")
    print(f"  contracts_per_side:       {cfg.contracts_per_side}")
    print(f"  dead_zone:                {cfg.dead_zone_lo}-{cfg.dead_zone_hi}¢")
    print(f"  inventory_skew_cents:     {cfg.inventory_skew_cents}")
    print(f"  max_inventory_per_market: {cfg.max_inventory_per_market}")
    print()


def run_one(ticker: str, cfg: SpreadConfig):
    print(f"Loading market {ticker}…")
    market = load_market(ticker)
    if not market:
        print(f"ERROR: ticker {ticker!r} not found or not finalized.", file=sys.stderr)
        sys.exit(2)

    print(f"  title:    {market.title}")
    print(f"  result:   {market.result}")
    print(f"  volume:   {market.volume:,}")
    print(f"  closed:   {market.close_time}")
    print()

    print("Loading trades…")
    t0 = time.time()
    trades = load_trades(ticker)
    print(f"  loaded {len(trades):,} trades in {time.time()-t0:.1f}s")
    print()

    _print_strategy_block(cfg)

    print("Simulating…")
    t0 = time.time()
    result = simulate(market, trades, baseline(cfg))
    print(f"  simulated in {time.time()-t0:.1f}s\n")
    print(result.summary())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", nargs="?", help="Specific market ticker to backtest")
    parser.add_argument("--pick-top", choices=list(PICK_PRESETS.keys()),
                        help="Pick the highest-volume finalized market matching a category")
    parser.add_argument("--half-spread", type=int, default=4)
    parser.add_argument("--size", type=int, default=10, help="contracts_per_side")
    parser.add_argument("--dead-lo", type=int, default=41)
    parser.add_argument("--dead-hi", type=int, default=50)
    parser.add_argument("--skew", type=int, default=0)
    parser.add_argument("--max-inv", type=int, default=50)
    args = parser.parse_args()

    cfg = SpreadConfig(
        half_spread_cents=args.half_spread,
        contracts_per_side=args.size,
        dead_zone_lo=args.dead_lo,
        dead_zone_hi=args.dead_hi,
        inventory_skew_cents=args.skew,
        max_inventory_per_market=args.max_inv,
    )

    if args.ticker:
        run_one(args.ticker, cfg)
        return

    if args.pick_top:
        prefix = PICK_PRESETS[args.pick_top]
        print(f"Picking top finalized market with event_ticker LIKE '{prefix}%'…")
        candidates = find_finalized_markets(event_prefix=prefix, min_volume=10000, limit=1)
        if not candidates:
            print("No matching markets found.", file=sys.stderr)
            sys.exit(2)
        run_one(candidates[0].ticker, cfg)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
