#!/bin/bash
# Nightly rebuild of the BTCD empirical calibration table.
#
# Re-trains data/btcd_calibration.json on the latest accumulated calibration
# snapshots so the bot's pred→actual mapping stays self-healing as the BTC
# vol regime drifts. Saves the build log to backtesting/results/btcd_calibrator_*.log.
# Telegrams a summary line if any mid bucket exceeds ±5pt — that's a regression
# signal worth waking up to.
#
# Schedule via ~/Library/LaunchAgents/com.tradingbot.btcd-calibrator.plist.
# Fires 01:40 ET — between Kalshi nightly (01:30, ~5min) and BTCD audit (01:45).
set -e

cd "$(dirname "$0")/.."
RUN_ID=$(date +%Y%m%d_%H%M)
OUT="backtesting/results/btcd_calibrator_${RUN_ID}.log"
PREV_TABLE="data/btcd_calibration.json"
TMP_TABLE_BACKUP="data/btcd_calibration.prev.json"

# Snapshot the previous table for drift comparison
if [ -f "$PREV_TABLE" ]; then
    cp "$PREV_TABLE" "$TMP_TABLE_BACKUP"
fi

# Rebuild (overwrites data/btcd_calibration.json)
venv/bin/python backtesting/build_btcd_calibrator.py > "$OUT" 2>&1
BUILD_RC=$?

if [ $BUILD_RC -ne 0 ]; then
    echo "Calibrator rebuild FAILED (rc=$BUILD_RC). See $OUT." >&2
    # Restore previous table so the bot doesn't end up with a stale or broken file
    if [ -f "$TMP_TABLE_BACKUP" ]; then
        cp "$TMP_TABLE_BACKUP" "$PREV_TABLE"
        echo "Restored previous calibration table from backup." >&2
    fi
    exit $BUILD_RC
fi

echo "BTCD calibrator rebuilt at $OUT"

# Telegram alert if the build flagged any mid bucket > 5pt — this means the
# vol regime has shifted enough that the new fit can't keep all buckets within
# tolerance, even with fresh data. Worth investigating.
if grep -q "GATE: FAIL" "$OUT"; then
    GATE_LINE=$(grep -A 5 "GATE: FAIL" "$OUT" | head -6 | tr '\n' ' ')
    venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from app.services.telegram_service import TelegramService
async def main():
    tg = TelegramService()
    await tg.send_message(
        '⚠️ <b>BTCD calibrator drift</b>\n'
        'Nightly rebuild failed the ≤5pt gate.\n'
        '<pre>$GATE_LINE</pre>\n'
        'Check $OUT for full bucket detail.'
    )
    await tg.close()
asyncio.run(main())
" 2>/dev/null || true
fi
