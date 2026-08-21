#!/bin/bash
# =============================================================================
# Autonomous nightly maintenance — invokes Claude to ACT on health findings.
#
# Why this exists (2026-08-20): nightly_digest.py reports and cannot act, so
# findings sat unchanged for days — the roster gap ran 9 days in the digest,
# price-source-audit failed 12+ days, the Kalshi whale tracker burned CPU for
# weeks. All were found only when the user opened a session. Reporting is not
# fixing. This closes that loop.
#
# Spend control: fires ONLY when health_report surfaces a finding set that
# differs from the last run. Same findings as yesterday = no invocation, no
# tokens. The weekly allowance is the binding constraint, so a daily agent that
# re-reads the same three items every morning is exactly what to avoid.
#
# Runs after the 07:00 digest so it sees the night's backtests and smoke test.
# =============================================================================

BOT_DIR="/Users/clawbot/Documents/Claude/Projects/tradingview-bot"
VENV="$BOT_DIR/venv/bin/python"
CLAUDE="${CLAUDE_BIN:-/Users/clawbot/.local/bin/claude}"   # overridable for testing
LOG="$BOT_DIR/logs/autonomous_nightly.log"
STATE="$BOT_DIR/logs/autonomous_nightly_state.txt"
MAX_LOG=2000

cd "$BOT_DIR" || exit 1
mkdir -p "$BOT_DIR/logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"; }

# Trim log
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LOG" ]; then
    tail -n "$MAX_LOG" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# ── 1. Collect findings (cheap, local, read-only) ────────────────────────────
FINDINGS=$("$VENV" scripts/health_report.py --quiet 2>/dev/null | grep '•' | sed 's/^[[:space:]]*//')
COUNT=$(printf '%s' "$FINDINGS" | grep -c '•')

if [ "$COUNT" -eq 0 ]; then
    log "OK - no findings, nothing to do (no spend)"
    printf '' > "$STATE"
    exit 0
fi

# ── 2. Only act on a CHANGED finding set ─────────────────────────────────────
# Strips digits so "3 signals hit X" and "4 signals hit X" count as the same
# finding — otherwise a counter ticking up re-fires this every single day.
#
# Also drops the CPU line before hashing. `ps -o %cpu` is a lifetime average on
# macOS and reads erratically — two runs seconds apart on 2026-08-20 gave 89%
# then 20.9%, so that finding appears and disappears on its own. Left in the
# hash it would re-fire this agent most days for no reason. It still shows in
# the report and the Telegram summary; it just cannot trigger an invocation by
# itself. Anything genuinely wrong with CPU will also show up as a real finding
# (stalled jobs, error spikes).
NORM=$(printf '%s' "$FINDINGS" | grep -v 'CPU' | sed 's/[0-9][0-9.,$-]*/N/g' | sort)
HASH=$(printf '%s' "$NORM" | shasum -a 256 | cut -d' ' -f1)
PREV=$(cat "$STATE" 2>/dev/null)

if [ "$HASH" = "$PREV" ]; then
    log "SKIP - $COUNT finding(s), unchanged since last run (no spend)"
    exit 0
fi

log "ACTION - $COUNT finding(s), set changed. Invoking Claude."
log "findings: $(printf '%s' "$FINDINGS" | tr '\n' '|')"

# ── 3. Invoke Claude to analyse and fix ──────────────────────────────────────
# Written to a temp file rather than $(cat <<EOF ...): the prompt contains
# parentheses and quotes, and nesting a heredoc inside command substitution
# breaks bash's parser on them.
PROMPT_FILE=$(mktemp "${TMPDIR:-/tmp}/autonomous_prompt.XXXXXX")
trap 'rm -f "$PROMPT_FILE"' EXIT
cat > "$PROMPT_FILE" <<EOF
You are running UNATTENDED as the nightly maintenance agent for this trading bot.
Nobody is watching. Be conservative and finish cleanly.

Today's health report surfaced these findings:

$FINDINGS

Your job:
1. Run: venv/bin/python scripts/health_report.py   (full output, for context)
2. Investigate each finding. Determine root cause from evidence, not assumption.
3. FIX what you can safely fix.
4. Append every action to DECISIONS.md using the existing What/Why/Risk/Reversible
   format, then commit and push.

IN SCOPE unattended — do these without asking:
  - Bug fixes, test fixes, observability, log/alert tuning
  - Config changes that do not increase capital at risk
  - Restarting the bot (verify /health 200 + scan logs/bot.log for NameError,
    "Application startup failed", and a stall after the Kamino step)
  - Backtest runs, analysis, documentation, memory updates

OUT OF SCOPE unattended — log to DECISIONS.md as "deferred, needs a human" and
do NOT do them:
  - Raising position sizing tiers, max_open_positions, or leverage
  - Deleting or deactivating TradingView alerts (no-cull policy)
  - Moving or withdrawing funds; any on-chain action beyond a normal trade
  - Raising loss_budget.max_loss_usd (that bound is the whole point)

Rules that still bind you: everything in CLAUDE.md, especially the Pine slot
rules and "never let a BUY execute without a TP/SL".

If a finding is already known and logged in review_list.md with no new
information, say so and move on rather than re-investigating it.

Finish with a 5-line summary: what you fixed, what you deferred, and why.
EOF

OUT=$("$CLAUDE" -p "$(cat "$PROMPT_FILE")" \
        --permission-mode acceptEdits \
        --allowed-tools "Bash,Read,Edit,Write,Grep,Glob" \
        2>&1)
RC=$?

log "claude exited rc=$RC"
printf '%s\n' "$OUT" | tail -40 >> "$LOG"

# Record the finding set we just acted on, so tomorrow is silent if unchanged.
printf '%s' "$HASH" > "$STATE"

# ── 4. Telegram the summary ──────────────────────────────────────────────────
export SUMMARY
SUMMARY=$(printf '%s' "$OUT" | tail -18)
export RC COUNT

"$VENV" - <<'PY' 2>>"$LOG"
import sys, asyncio, os, html
sys.path.insert(0, "/Users/clawbot/Documents/Claude/Projects/tradingview-bot")
from app.config import load_config; load_config()
from app.services.telegram_service import TelegramService
rc    = os.environ.get("RC", "?")
count = os.environ.get("COUNT", "?")
# Escape: Claude's output routinely contains <, > and & which break HTML parse
body  = html.escape(os.environ.get("SUMMARY", "(no output)")[:2500])
msg = (f"\U0001F916 <b>Autonomous nightly run</b>\n"
       f"Findings acted on: {count} · claude rc={rc}\n\n"
       f"<pre>{body}</pre>")
asyncio.run(TelegramService().send_message(msg))
PY

log "done"
exit 0
