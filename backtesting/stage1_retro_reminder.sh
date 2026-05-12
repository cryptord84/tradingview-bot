#!/bin/bash
# One-shot reminder: 2026-05-19 Stage 1 spread_bot retro + whale-gate decision.
#
# Fires once via com.tradingbot.stage1-retro plist. The plist is set to a
# specific calendar date so it self-disables after firing. After 2026-05-19
# this whole reminder can be deleted.
set -u

cd "$(dirname "$0")/.."

# Pull current state for the reminder body
KSTATUS=$(curl -sS http://127.0.0.1:8000/api/kalshi/status 2>/dev/null || echo "{}")
CASH=$(echo "$KSTATUS" | venv/bin/python -c "
import sys, json
try:
    d = json.load(sys.stdin)
    cash = (d.get('balance') or {}).get('balance', 0) / 100
    pnl = (d.get('stats') or {}).get('total_pnl_cents', 0) / 100
    print(f'{cash:.2f}|{pnl:+.2f}')
except Exception:
    print('?|?')
")
CASH_USD=$(echo "$CASH" | cut -d'|' -f1)
PNL_USD=$(echo "$CASH" | cut -d'|' -f2)

# Whale-tracker row count this week
WHALES_THIS_WEEK=$(sqlite3 data/trades.db "SELECT COUNT(*) FROM kalshi_whales WHERE ts >= '2026-05-12'" 2>/dev/null || echo "?")

venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from app.services.telegram_service import TelegramService
async def main():
    tg = TelegramService()
    await tg.send_message(
        '📋 <b>Stage 1 retro due TODAY</b>\n\n'
        'Kalshi cash: \$${CASH_USD}\n'
        'Realized P&L (all-time DB): \$${PNL_USD}\n'
        'Whale samples logged this week: ${WHALES_THIS_WEEK}\n\n'
        '<b>Decisions to make:</b>\n'
        '1. If Kalshi total &lt; \$108 → disable spread_bot, end Kalshi side\n'
        '2. If ≥ \$108 + net positive → Stage 2 (contracts 2→3, exposure 3k→5k)\n'
        '3. Backtest whale-gate hypothesis on accumulated kalshi_whales table\n\n'
        'See project_kalshi_stage1_20260512.md + review_list.md.'
    )
    await tg.close()
asyncio.run(main())
" 2>/dev/null || true

echo "Stage 1 retro reminder sent"
