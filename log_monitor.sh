#!/bin/bash
# =============================================================================
# Trading Bot Log Monitor — Lightweight health check
# Runs every 2 hours. Checks error rates, service health, balances.
# Writes issues to logs/monitor_alert.json for Claude Code to analyze.
# =============================================================================

BOT_DIR="/Users/clawbot/Documents/Claude/Projects/tradingview-bot"
LOG_FILE="$BOT_DIR/logs/bot.log"
ALERT_FILE="$BOT_DIR/logs/monitor_alert.json"
MONITOR_LOG="$BOT_DIR/logs/monitor.log"
HEALTH_URL="http://localhost:8000/health"
MAX_ERRORS_PER_HOUR=50
MAX_REPEATED_ERROR=20

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(timestamp)] $1" >> "$MONITOR_LOG"; }

# Trim monitor log
if [ -f "$MONITOR_LOG" ] && [ "$(wc -l < "$MONITOR_LOG")" -gt 200 ]; then
    tail -n 100 "$MONITOR_LOG" > "$MONITOR_LOG.tmp" && mv "$MONITOR_LOG.tmp" "$MONITOR_LOG"
fi

issues=()

# ── 1. Bot health check ──
health=$(curl -s -o /tmp/bot_health.json -w "%{http_code}" --connect-timeout 5 --max-time 10 "$HEALTH_URL" 2>/dev/null)
if [ "$health" != "200" ]; then
    issues+=("BOT_DOWN: Health endpoint returned HTTP $health")
    log "FAIL - Bot not responding (HTTP $health)"
else
    # Check for stopped services that should be running
    stopped=$(python3 -c "
import json
with open('/tmp/bot_health.json') as f:
    d = json.load(f)
for svc, info in d.get('services', {}).items():
    if info.get('enabled', True) and not info.get('running', True):
        print(f'{svc}: enabled but not running')
" 2>/dev/null)
    if [ -n "$stopped" ]; then
        issues+=("SERVICES_DOWN: $stopped")
        log "WARN - Services down: $stopped"
    fi
fi

# ── 2. Error rate check (last 2 hours) ──
if [ -f "$LOG_FILE" ]; then
    two_hours_ago=$(date -v-2H "+%Y-%m-%d %H:%M" 2>/dev/null || date -d "2 hours ago" "+%Y-%m-%d %H:%M" 2>/dev/null)
    recent_errors=$(grep -c '\[ERROR\]' "$LOG_FILE" 2>/dev/null || echo 0)

    if [ "$recent_errors" -gt "$MAX_ERRORS_PER_HOUR" ]; then
        # Get top error types
        top_errors=$(grep '\[ERROR\]' "$LOG_FILE" | awk -F'\\[ERROR\\] ' '{print $2}' | cut -d: -f1-2 | sort | uniq -c | sort -rn | head -3)
        issues+=("HIGH_ERROR_RATE: $recent_errors errors in log. Top: $top_errors")
        log "WARN - High error rate: $recent_errors errors"
    fi

    # Check for repeated identical errors (spam)
    repeated=$(grep '\[ERROR\]' "$LOG_FILE" | awk -F'\\[ERROR\\] ' '{print $2}' | cut -d: -f1-2 | sort | uniq -c | sort -rn | head -1 | awk '{if ($1 > '$MAX_REPEATED_ERROR') print $0}')
    if [ -n "$repeated" ]; then
        issues+=("ERROR_SPAM: $repeated")
        log "WARN - Repeated error spam: $repeated"
    fi
fi

# ── 3. Stale positions check ──
stale_positions=$(python3 -c "
import sqlite3, os
db = os.path.join('$BOT_DIR', 'data', 'trades.db')
if not os.path.exists(db):
    exit()
conn = sqlite3.connect(db)
rows = conn.execute(\"SELECT count(*) FROM positions WHERE status = 'open'\").fetchone()
if rows[0] > 0:
    # Check if any are older than 24 hours
    old = conn.execute(\"SELECT count(*) FROM positions WHERE status = 'open' AND created_at < datetime('now', '-24 hours')\").fetchone()
    if old[0] > 0:
        print(f'{old[0]} stale positions older than 24h (of {rows[0]} open)')
conn.close()
" 2>/dev/null)
if [ -n "$stale_positions" ]; then
    issues+=("STALE_POSITIONS: $stale_positions")
    log "WARN - $stale_positions"
fi

# ── 4. Kalshi balance check ──
kalshi_balance=$(python3 -c "
import sys, os
sys.path.insert(0, '$BOT_DIR')
os.chdir('$BOT_DIR')
try:
    from app.services.kalshi_client import KalshiTradingClient
    client = KalshiTradingClient()
    bal = client.get_balance()
    print(bal)
except:
    print(-1)
" 2>/dev/null)
if [ "$kalshi_balance" != "-1" ] && [ -n "$kalshi_balance" ]; then
    bal_dollars=$(echo "scale=2; $kalshi_balance / 100" | bc 2>/dev/null || echo "?")
    if [ "$kalshi_balance" -lt 5000 ] 2>/dev/null; then
        issues+=("LOW_KALSHI_BALANCE: \$$bal_dollars ($kalshi_balance cents)")
        log "WARN - Low Kalshi balance: \$$bal_dollars"
    fi
fi

# ── 5. Disk / log size check ──
log_size_mb=$(du -sm "$BOT_DIR/logs" 2>/dev/null | awk '{print $1}')
if [ -n "$log_size_mb" ] && [ "$log_size_mb" -gt 500 ] 2>/dev/null; then
    issues+=("LOG_SIZE: logs directory is ${log_size_mb}MB")
    log "WARN - Log directory ${log_size_mb}MB"
fi

# ── Write results ──
FINGERPRINT_FILE="$BOT_DIR/logs/.monitor_last_fingerprint"

if [ ${#issues[@]} -eq 0 ]; then
    log "OK - All checks passed"
    rm -f "$ALERT_FILE"
    rm -f "$FINGERPRINT_FILE"
else
    log "ALERT - ${#issues[@]} issues found"
    ISSUES_TMP="$BOT_DIR/logs/.monitor_issues.tmp"
    printf '%s\n' "${issues[@]}" > "$ISSUES_TMP"
    python3 - "$ISSUES_TMP" "$ALERT_FILE" "$FINGERPRINT_FILE" "$BOT_DIR" <<'PY'
import sys, os, json, datetime, hashlib

issues_tmp, alert_file, fp_file, bot_dir = sys.argv[1:5]

with open(issues_tmp) as f:
    issues = [line.strip() for line in f if line.strip()]

alert = {
    "timestamp": datetime.datetime.now().isoformat(),
    "issue_count": len(issues),
    "issues": issues,
}
with open(alert_file, "w") as f:
    json.dump(alert, f, indent=2)

fingerprint = hashlib.sha256("\n".join(sorted(issues)).encode()).hexdigest()
prev_fp = ""
if os.path.exists(fp_file):
    with open(fp_file) as f:
        prev_fp = f.read().strip()

if fingerprint == prev_fp:
    print(f"Wrote {len(issues)} issues; same as last alert, skipping Telegram")
    sys.exit(0)

with open(fp_file, "w") as f:
    f.write(fingerprint)

sys.path.insert(0, bot_dir)
os.chdir(bot_dir)
try:
    import yaml, httpx
    with open(os.path.join(bot_dir, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    tg = cfg.get("telegram", {}) or {}
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if tg.get("enabled") and token and chat_id:
        text = f"[BOT MONITOR] {len(issues)} issue(s)\n\n" + "\n\n".join(
            f"- {i}" for i in issues
        )
        if len(text) > 4000:
            text = text[:4000] + "\n...(truncated)"
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": str(chat_id), "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        print(f"Wrote {len(issues)} issues; Telegram sent")
    else:
        print(f"Wrote {len(issues)} issues; Telegram disabled, no send")
except Exception as e:
    print(f"Wrote {len(issues)} issues; Telegram send failed: {e}")
PY
    rm -f "$ISSUES_TMP"
fi
