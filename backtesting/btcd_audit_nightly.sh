#!/bin/bash
# Nightly BTCD Brier-score audit wrapper. Captures the script's stdout to a
# timestamped summary file under backtesting/results/, alongside the TV +
# Kalshi nightly summaries.
#
# Schedule via ~/Library/LaunchAgents/com.tradingbot.btcd-audit.plist.
set -e

cd "$(dirname "$0")/.."
RUN_ID=$(date +%Y%m%d_%H%M)
OUT="backtesting/results/btcd_audit_${RUN_ID}.txt"

venv/bin/python backtesting/btcd_brier_audit.py > "$OUT" 2>&1
echo "BTCD audit written to $OUT"
