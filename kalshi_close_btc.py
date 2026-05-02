#!/usr/bin/env python
"""Close all open KXBTCD-26APR2017 positions before 5pm EDT settlement.

Placing limit orders at the aggressive side (yes_ask for closing shorts, yes_bid
for closing longs). Uses client_order_id for idempotency.
"""
import sys, os, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services.kalshi_client import KalshiTradingClient

client = KalshiTradingClient()

positions = client.get_positions()
btc = [p for p in positions if p.get('ticker', '').startswith('KXBTCD-26APR2017') and p.get('position', 0) != 0]

print(f"Closing {len(btc)} KXBTCD-26APR2017 positions:\n")

for p in sorted(btc, key=lambda x: x.get('ticker', '')):
    ticker = p['ticker']
    pos = p['position']
    qty = abs(pos)
    m = client.get_market(ticker) if hasattr(client, 'get_market') else None
    yes_bid = m.get('yes_bid', 0) if m else 0
    yes_ask = m.get('yes_ask', 100) if m else 100

    if pos < 0:
        # SHORT YES → buy YES at ask (or just below 100) to close
        action, side = 'buy', 'yes'
        price = min(99, max(1, yes_ask))
    else:
        # LONG YES → sell YES at bid (or just above 0) to close
        action, side = 'sell', 'yes'
        price = max(1, min(99, yes_bid))

    cid = f"close-{uuid.uuid4().hex[:12]}"
    try:
        result = client.place_order(
            ticker=ticker,
            side=side,
            action=action,
            count=qty,
            yes_price=price,
            order_type='limit',
            client_order_id=cid,
        )
        print(f"  OK   {ticker:<35} pos={pos:>4}  {action:4s} {side} {qty:>3} @ {price}¢  cid={cid}")
    except Exception as e:
        print(f"  FAIL {ticker:<35} pos={pos:>4}  {action} {side} {qty} @ {price}¢  err={e}")
    time.sleep(0.2)

print("\nDone. Re-checking positions in 5s...")
time.sleep(5)
positions2 = client.get_positions()
remaining = [p for p in positions2 if p.get('ticker', '').startswith('KXBTCD-26APR2017') and p.get('position', 0) != 0]
print(f"Still-open KXBTCD positions: {len(remaining)}")
for p in remaining:
    print(f"  {p['ticker']:<35} pos={p.get('position', 0):>4}  exposure=${float(p.get('market_exposure_dollars', 0)):.2f}")
