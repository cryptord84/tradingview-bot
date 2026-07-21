#!/bin/bash
# =============================================================================
# TradingView SOL Bot - Health Check
# Runs via launchd every 15 minutes. Checks if the bot is responding,
# restarts it if not, and logs results.
# =============================================================================

BOT_DIR="/Users/clawbot/Documents/Claude/Projects/tradingview-bot"
LOG_FILE="$BOT_DIR/logs/healthcheck.log"
PID_FILE="$BOT_DIR/logs/bot.pid"
VENV="$BOT_DIR/venv/bin/python"
PORT=8000
HEALTH_URL="http://localhost:$PORT/health"
MAX_LOG_LINES=500

timestamp() {
    date "+%Y-%m-%d %H:%M:%S"
}

log() {
    echo "[$(timestamp)] $1" >> "$LOG_FILE"
}

# Ensure log directory exists
mkdir -p "$BOT_DIR/logs"

# Trim log if too long
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt "$MAX_LOG_LINES" ]; then
    tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# Check if bot is responding — RETRY to avoid false-positive restarts.
# A single transient timeout (e.g. the event loop briefly blocked by a slow
# Claude-CLI decision) must NOT trigger a kill+restart of a healthy bot.
# Require 3 consecutive failures (~20s) before deciding it's actually down.
healthy=""
for attempt in 1 2 3; do
    health_response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "$HEALTH_URL" 2>/dev/null)
    if [ "$health_response" = "200" ]; then healthy="yes"; break; fi
    log "WARN - health check $attempt/3 failed (HTTP $health_response)"
    [ "$attempt" -lt 3 ] && sleep 5
done

if [ -n "$healthy" ]; then
    log "OK - Bot healthy (HTTP 200)"
    exit 0
fi

log "WARN - Bot failed 3 consecutive health checks, stopping any running instance(s)..."

# SINGLE-INSTANCE GUARANTEE: stop EVERY bot process (the port holder AND any
# stray uvicorn), then confirm both the process list AND port $PORT are clean
# BEFORE starting a new one. If we cannot fully clear them, ABORT the start —
# better to stay down and retry next tick than to risk two bots trading.
BOT_PATTERN="venv/bin/uvicorn main:app"
pkill -f "$BOT_PATTERN" 2>/dev/null
[ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null
for i in $(seq 1 8); do
    pgrep -f "$BOT_PATTERN" >/dev/null 2>&1 || lsof -ti :$PORT >/dev/null 2>&1 || break
    pkill -9 -f "$BOT_PATTERN" 2>/dev/null
    port_pid=$(lsof -ti :$PORT 2>/dev/null)
    [ -n "$port_pid" ] && kill -9 $port_pid 2>/dev/null
    sleep 1
done

if pgrep -f "$BOT_PATTERN" >/dev/null 2>&1 || lsof -ti :$PORT >/dev/null 2>&1; then
    log "ERROR - could not fully stop existing bot / free port $PORT; ABORTING start to avoid a second instance. Will retry next tick."
    exit 1
fi
log "Confirmed: no bot process running and port $PORT is free."

# Start the bot in a visible Terminal window
log "ACTION - Starting bot in Terminal window..."

osascript <<'APPLESCRIPT'
tell application "Terminal"
    activate
    do script "sleep 1 && cd /Users/clawbot/Documents/Claude/Projects/tradingview-bot && sleep 1 && source venv/bin/activate && sleep 1 && set -a && [ -f .env ] && source .env && set +a && echo '=== TradingView SOL Bot ===' && uvicorn main:app --host 0.0.0.0 --port 8000"
end tell
APPLESCRIPT

# Wait for Terminal to open + sleeps + uvicorn startup
sleep 12
verify=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "$HEALTH_URL" 2>/dev/null)

if [ "$verify" = "200" ]; then
    # Grab the pid from the running port
    new_pid=$(lsof -ti :$PORT 2>/dev/null | head -1)
    echo "$new_pid" > "$PID_FILE"
    log "OK - Bot started in Terminal window (pid=$new_pid)"
else
    log "ERROR - Bot failed to start in Terminal (HTTP $verify)"
fi
