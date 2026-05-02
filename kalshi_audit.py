#!/usr/bin/env python
"""Kalshi account audit — balance, open positions, fills, fee reconciliation."""
import sys, os, json
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services.kalshi_client import KalshiTradingClient

client = KalshiTradingClient()

print("=" * 80)
print("KALSHI ACCOUNT AUDIT")
print("=" * 80)

# ── Balance ───────────────────────────────────────────────────────
bal = client.get_balance()
balance_cents = bal.get('balance', 0)
print(f"\nCASH BALANCE: ${balance_cents/100:.2f}")

# ── Positions ─────────────────────────────────────────────────────
positions = client.get_positions()
active = [p for p in positions if p.get('position', 0) != 0]
zero_pos = [p for p in positions if p.get('position', 0) == 0 and float(p.get('realized_pnl_dollars', 0)) != 0]

total_exposure_cents = 0
total_fees_cents = 0
total_realized_pnl_dollars = 0.0
family = defaultdict(lambda: {'exposure_cents': 0, 'realized_pnl': 0.0, 'count': 0})

for p in positions:
    exposure = int(p.get('market_exposure', 0) or 0)
    total_exposure_cents += exposure
    fees_dollars = float(p.get('fees_paid_dollars', 0) or 0)
    total_fees_cents += int(round(fees_dollars * 100))
    realized = float(p.get('realized_pnl_dollars', 0) or 0)
    total_realized_pnl_dollars += realized
    ticker = p.get('ticker', '')
    prefix = ticker.split('-')[0] if '-' in ticker else ticker[:10]
    family[prefix]['exposure_cents'] += exposure
    family[prefix]['realized_pnl'] += realized
    family[prefix]['count'] += 1 if p.get('position', 0) != 0 else 0

print(f"\n── POSITIONS ─────────────────────────────────────────────────────")
print(f"Total position rows: {len(positions)}")
print(f"Currently open (nonzero): {len(active)}")
print(f"Closed w/ realized P&L: {len(zero_pos)}")
print(f"Open market exposure: ${total_exposure_cents/100:.2f}")
print(f"Realized P&L (all-time): ${total_realized_pnl_dollars:.2f}")
print(f"Fees paid (cumulative): ${total_fees_cents/100:.2f}")

print(f"\n── BY TICKER FAMILY ─────────────────────────────────────────────")
print(f"{'Family':<18} {'Open':>5} {'Exposure':>10} {'Realized P&L':>14}")
for fam, v in sorted(family.items(), key=lambda x: -x[1]['exposure_cents']):
    print(f"{fam:<18} {v['count']:>5} ${v['exposure_cents']/100:>8.2f}  ${v['realized_pnl']:>12.2f}")

print(f"\n── TOP 15 OPEN POSITIONS (by exposure) ─────────────────────────")
active_sorted = sorted(active, key=lambda p: -int(p.get('market_exposure', 0) or 0))
print(f"{'Ticker':<32} {'Pos':>6} {'Exposure':>10} {'Traded':>9} {'Rlz P&L':>9}")
for p in active_sorted[:15]:
    print(f"{p.get('ticker', ''):<32} {p.get('position', 0):>6} "
          f"${int(p.get('market_exposure', 0))/100:>8.2f} "
          f"${float(p.get('total_traded_dollars', 0)):>7.2f} "
          f"${float(p.get('realized_pnl_dollars', 0)):>7.2f}")

# ── Fills (recent) ────────────────────────────────────────────────
print(f"\n── RECENT FILLS ──────────────────────────────────────────────────")
fills = client.get_fills(limit=200)
print(f"Total fills fetched: {len(fills)}")
if fills:
    total_buy = sum((f.get('total_cost') or 0) for f in fills if f.get('action') == 'buy')
    total_sell = sum((f.get('total_cost') or 0) for f in fills if f.get('action') == 'sell')
    total_fee = sum((f.get('fee') or f.get('fees_paid') or 0) for f in fills)
    print(f"Buy notional (fills):  ${total_buy/100:.2f}")
    print(f"Sell notional (fills): ${total_sell/100:.2f}")
    print(f"Fees on fills:         ${total_fee/100:.2f}")

# ── Reconciliation ────────────────────────────────────────────────
print(f"\n── RECONCILIATION ────────────────────────────────────────────────")
print(f"Account funded:        $220.00")
print(f"Current cash balance:  ${balance_cents/100:.2f}")
print(f"Open exposure (cost):  ${total_exposure_cents/100:.2f}")
print(f"Account value:         ${(balance_cents + total_exposure_cents)/100:.2f}")
print(f"Unrealized drawdown:   ${220 - (balance_cents + total_exposure_cents)/100:.2f}")
print(f"Realized P&L (sum):    ${total_realized_pnl_dollars:.2f}")
print(f"Fees cumulative:       ${total_fees_cents/100:.2f}")
