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

# Check if bot is responding
health_response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "$HEALTH_URL" 2>/dev/null)

if [ "$health_response" = "200" ]; then
    log "OK - Bot healthy (HTTP $health_response)"
    exit 0
fi

log "WARN - Bot not responding (HTTP $health_response), checking process..."

# Check if process is running
bot_pid=""
if [ -f "$PID_FILE" ]; then
    bot_pid=$(cat "$PID_FILE")
    if ! kill -0 "$bot_pid" 2>/dev/null; then
        log "WARN - Stale PID file (pid=$bot_pid not running)"
        bot_pid=""
    fi
fi

# Also check by port
port_pid=$(lsof -ti :$PORT 2>/dev/null | head -1)

if [ -n "$port_pid" ]; then
    log "WARN - Process on port $PORT (pid=$port_pid) not responding to health check, killing..."
    kill "$port_pid" 2>/dev/null
    sleep 2
    kill -9 "$port_pid" 2>/dev/null
elif [ -n "$bot_pid" ]; then
    log "WARN - Bot process (pid=$bot_pid) exists but port $PORT not open, killing..."
    kill "$bot_pid" 2>/dev/null
    sleep 2
    kill -9 "$bot_pid" 2>/dev/null
fi

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
