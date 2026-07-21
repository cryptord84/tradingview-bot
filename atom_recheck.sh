#!/bin/bash
# =============================================================================
# One-shot: re-check Liq Sweep/ATOM/1D walk-forward status after the nightly.
# Created 2026-06-15 per user request (ATOM flipped PASS->FAIL on the 06-15
# nightly, IS_PF 1.36 < gate). Fires once at 03:00 local (after the 00:03
# nightly + 02:00 HTF run), writes the verdict to a status file, then removes
# its own launchd job so it never runs again.
#
# Source of truth is the backtests DB table, NOT the summary .txt: when a combo
# FAILS it drops out of the summary's top-15, but the DB always has the row.
# =============================================================================

BOT_DIR="/Users/clawbot/Documents/Claude/Projects/tradingview-bot"
DB="$BOT_DIR/data/trades.db"
OUT="$BOT_DIR/backtesting/results/atom_recheck.txt"
LABEL="com.tradingbot.atom-recheck"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TODAY=$(date +%Y-%m-%d)
TS=$(date "+%Y-%m-%d %H:%M:%S %Z")

row=$(sqlite3 "$DB" \
  "SELECT created_at||'|'||profit_factor||'|'||win_rate||'|'||total_trades||'|'||notes
   FROM backtests
   WHERE strategy_name='Liq Sweep' AND symbol='ATOM' AND timeframe='1D'
     AND notes NOT LIKE '%htf%'
   ORDER BY created_at DESC LIMIT 1;")

{
  echo "ATOM  Liq Sweep / 1D  walk-forward re-check"
  echo "generated: $TS"
  echo "-------------------------------------------------------------"
  if [ -z "$row" ]; then
    echo "NO ATOM ROW in backtests table — the nightly may not have run."
  else
    created=$(echo "$row" | cut -d'|' -f1)
    pf=$(echo "$row" | cut -d'|' -f2)
    wr=$(echo "$row" | cut -d'|' -f3)
    trades=$(echo "$row" | cut -d'|' -f4)
    notes=$(echo "$row" | cut -d'|' -f5-)
    is_pf=$(echo "$notes" | grep -oE "IS_PF=[0-9.]+" | head -1 | cut -d= -f2)
    oos_pf=$(echo "$notes" | grep -oE "OOS_PF=[0-9.]+" | head -1 | cut -d= -f2)
    if echo "$notes" | grep -q "WF_FAIL"; then verdict="FAIL"; else verdict="PASS"; fi

    case "$created" in
      ${TODAY}*) fresh="yes — this is tonight's run" ;;
      *)         fresh="WARNING: newest row is $created, nightly may NOT have run tonight" ;;
    esac

    echo "VERDICT:  $verdict"
    echo "freshness: $fresh"
    echo "PF=$pf  WR=$wr%  Trades=$trades  IS_PF=$is_pf  OOS_PF=$oos_pf"
    echo
    echo "baseline: 06-13 PASS 1.76 | 06-14 PASS 1.76 | 06-15 FAIL 1.43 (IS_PF 1.36)"
    echo "watching: IS_PF back above the gate = recovered; still failing = a 2nd weak night"
    echo
    echo "full WF note:"
    echo "  $notes"
  fi
} > "$OUT" 2>&1

# Self-remove: this is a one-shot. bootout unschedules the (now-running) job;
# rm deletes the plist so it can't be reloaded. Both no-op harmlessly if absent.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$PLIST"
