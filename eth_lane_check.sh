#!/bin/bash
# =============================================================================
# One-shot: verdict on the whale-dry-run ETH lanes after ~1 week of paper data.
# Created 2026-06-30. The first read (n=7-14) showed ETH15M +22c / ETHD +7c but
# was too thin to trust; BTC lanes were break-even/negative. This re-runs the
# realized-edge-net-of-slippage analysis on the now-larger sample and writes a
# verdict: is the ETH edge real (net positive after fees) or small-sample noise?
# Then it removes its own launchd job (fires once).
# =============================================================================
BOT_DIR="/Users/clawbot/Documents/Claude/Projects/tradingview-bot"
OUT="$BOT_DIR/backtesting/results/eth_lane_check.txt"
LABEL="com.tradingbot.eth-lane-check"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cd "$BOT_DIR" || exit 1

"$BOT_DIR/venv/bin/python" - > "$OUT" 2>&1 << 'PYEOF'
import sqlite3
from datetime import datetime
from collections import defaultdict
FEE = 2  # conservative Kalshi fee estimate, cents/contract
c = sqlite3.connect('data/trades.db'); c.row_factory = sqlite3.Row
rows = c.execute("SELECT * FROM whale_crypto_dryrun WHERE status='settled' AND realized_pnl_cents IS NOT NULL").fetchall()
def series(t):
    for s in ['KXBTC15M','KXETH15M','KXBTCD','KXETHD']:
        if t.startswith(s+'-'): return s
    return '?'
g = defaultdict(list)
for r in rows: g[series(r['ticker'])].append(r)
def st(rs):
    n=len(rs)
    if not n: return None
    edge=sum(r['realized_pnl_cents'] for r in rs)/n
    return n, sum(r['won'] for r in rs)/n*100, sum(r['entry_price_cents'] for r in rs)/n, sum(r['slippage_cents'] for r in rs)/n, edge
print("ETH-lane verdict —", datetime.now().strftime("%Y-%m-%d %H:%M"))
print("="*64)
print(f"{'lane':10} {'n':>4} {'winpct':>6} {'entry':>6} {'slip':>5} {'gross':>6} {'net(-'+str(FEE)+'c)':>10}")
eth_net=[]; eth_n=0
for k in sorted(g):
    s=st(g[k])
    if not s or s[0]<3:
        print(f"{k:10} {s[0] if s else 0:>4}  (too few)"); continue
    net=s[4]-FEE
    tag=""
    if k.startswith('KXETH'):
        eth_net.append((s[4]*s[0])); eth_n+=s[0]
        tag = " <-- ETH"
    print(f"{k:10} {s[0]:>4} {s[1]:4.0f}% {s[2]:5.0f}c {s[3]:+4.0f}c {s[4]:+5.1f}c {net:+10.1f}c{tag}")
print("-"*64)
if eth_n:
    eth_gross = sum(eth_net)/eth_n; eth_netedge = eth_gross-FEE
    print(f"ETH lanes combined: n={eth_n}  gross={eth_gross:+.1f}c  NET(after {FEE}c fee)={eth_netedge:+.1f}c")
    print()
    if eth_n < 40:
        print("VERDICT: STILL THIN (n<40) — extend or treat as inconclusive.")
    elif eth_netedge >= 4:
        print("VERDICT: ETH EDGE HOLDS net-positive on a real sample -> candidate for a")
        print("         SMALL live trial on ETH lanes only (mid-band, $15/day breaker).")
    elif eth_netedge > 0:
        print("VERDICT: marginal (0-4c net) — not worth live risk; the early +22c was")
        print("         mostly small-sample noise. Hold paper or wind down.")
    else:
        print("VERDICT: ETH edge did NOT survive the bigger sample (net<=0). The early")
        print("         positive was noise. Whale-following is dead across all lanes.")
print()
print("(baseline 06-30: ETH15M +22.4c n=14, ETHD +7c n=7, BTC negative; +8c avg slippage was the killer)")
PYEOF

# self-remove (one-shot)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$PLIST"
