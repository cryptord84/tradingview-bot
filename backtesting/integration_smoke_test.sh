#!/bin/bash
# Nightly integration smoke test wrapper. Runs the Python suite, captures
# output to backtesting/results/, and Telegrams on FAIL.
#
# Schedule via ~/Library/LaunchAgents/com.tradingbot.integration-smoke.plist.
# Fires daily at 04:00 ET — after backtest nightly (03:30) and after the
# BTCD calibrator rebuild (01:40) + audit (01:45) finish.
set -u

cd "$(dirname "$0")/.."
RUN_ID=$(date +%Y%m%d_%H%M)
OUT="backtesting/results/integration_smoke_${RUN_ID}.log"

venv/bin/python -m backtesting.integration_smoke_test > "$OUT" 2>&1
RC=$?

# Summarize result + Telegram only on FAIL (silent on PASS — every passing run
# is just noise in the chat).
if [ $RC -ne 0 ]; then
    SUMMARY=$(grep -E '^Failures:|^  - ' "$OUT" | head -10 | tr '\n' '\n')
    PASSED_LINE=$(grep -E '^=== Summary' "$OUT" | head -1)
    venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from app.services.telegram_service import TelegramService
async def main():
    tg = TelegramService()
    body = '''⚠️ <b>Integration smoke test FAILED</b>
${PASSED_LINE}

<pre>${SUMMARY}</pre>

See ${OUT} for full output.'''
    await tg.send_message(body)
    await tg.close()
asyncio.run(main())
" 2>/dev/null || true
    echo "Integration smoke test FAILED — see $OUT"
    exit $RC
fi

echo "Integration smoke test PASS — $OUT"
exit 0
