"""Webhook reconciliation — catch TV→bot delivery gaps and silent rejections.

Compares ngrok inspector's /webhook POST history (TV→ngrok→bot delivery layer)
to the bot's signals_log table (bot's processed-signal record). Flags any:

  - Status != 200            → bot rejected the webhook (wrong secret, 422, etc.)
  - Status == 200 but missing → bot returned 200 (e.g. "paused" path) but
                                 didn't enqueue the signal — silent loss.

Runs as cron via launchd `com.tradingbot.webhook-reconciliation` every 30 min.
Sends Telegram on anomaly. Idempotent via local state file (alerted IDs not
re-alerted in subsequent runs).

Limitations:
  - Only sees what reached ngrok. If TV's outbound delivery itself failed, this
    won't catch it. (For that, would need /list_fires comparison from a TV-auth'd
    session, which a cron job can't do.)
  - ngrok-free inspector caches ~100 most-recent requests. At our fire rate
    (~10/day) that's ~10 days of history — plenty for a 30-min window.
"""
from __future__ import annotations

import sys, os, json, base64, sqlite3, asyncio, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

NGROK_INSPECTOR_URL = "http://127.0.0.1:4040/api/requests/http"
DB_PATH = "data/trades.db"
STATE_FILE = "backtesting/results/webhook_recon_state.json"
LOG_FILE = "backtesting/results/webhook_recon_history.txt"

# Lookback window: only flag webhooks within the last N minutes. Wider than the
# cron interval (30 min) so transient timing wobbles don't cause false positives.
DEFAULT_WINDOW_MIN = 60


def fetch_ngrok_webhooks(window_min: int) -> list[dict]:
    """Pull /webhook POSTs from ngrok inspector inside the time window."""
    try:
        r = httpx.get(NGROK_INSPECTOR_URL, params={"limit": "100"}, timeout=10)
        data = r.json()
    except Exception as e:
        print(f"  ngrok inspector unreachable: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    out = []
    for req in data.get("requests", []):
        if req.get("request", {}).get("uri") != "/webhook":
            continue
        # ngrok times are like "2026-05-08T16:00:15-04:00"
        try:
            t = datetime.fromisoformat(req["start"]).astimezone(timezone.utc)
        except Exception:
            continue
        if t < cutoff:
            continue
        # Decode body
        raw_b64 = req.get("request", {}).get("raw", "")
        body_obj = {}
        try:
            raw = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
            if "\r\n\r\n" in raw:
                body = raw.split("\r\n\r\n", 1)[1]
                body_obj = json.loads(body)
        except Exception:
            pass
        out.append({
            "id": req.get("id", ""),
            "ts_utc": t,
            "status": req.get("response", {}).get("status_code", 0),
            "symbol": body_obj.get("symbol", "?"),
            "signal_type": body_obj.get("signal_type", "?"),
            "strategy": body_obj.get("strategy", "?"),
            "secret_present": bool(body_obj.get("secret")),
        })
    return out


def fetch_signals_log(window_min: int) -> list[dict]:
    """Pull signals_log entries inside the time window. Timestamps are UTC."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, raw_payload FROM signals_log WHERE timestamp >= ? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["timestamp"]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        try:
            p = json.loads(r["raw_payload"])
        except Exception:
            p = {}
        out.append({
            "ts_utc": t,
            "symbol": p.get("symbol", "?"),
            "signal_type": p.get("signal_type", "?"),
            "strategy": p.get("strategy", "?"),
        })
    return out


def match_ngrok_to_log(ngrok_req: dict, log_entries: list[dict]) -> bool:
    """Find a signals_log entry that matches this ngrok webhook (within ±90s)."""
    for entry in log_entries:
        if entry["symbol"] != ngrok_req["symbol"]:
            continue
        if entry["signal_type"] != ngrok_req["signal_type"]:
            continue
        delta = abs((entry["ts_utc"] - ngrok_req["ts_utc"]).total_seconds())
        if delta <= 90:
            return True
    return False


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"alerted_ids": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted_ids": []}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # Trim alerted_ids — only keep the last 200 entries (older ones can't recur
    # in our 60-min lookback window anyway).
    state["alerted_ids"] = state.get("alerted_ids", [])[-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


async def send_telegram_alert(anomalies: list[dict]) -> None:
    try:
        from app.services.telegram_service import TelegramService
        ts = TelegramService()
        lines = ["<b>⚠️ Webhook reconciliation — anomalies found</b>", ""]
        for a in anomalies:
            lines.append(
                f"• {a['kind']}: {a['signal_type']} {a['symbol']} "
                f"({a['strategy']}) at {a['ts_utc'].strftime('%H:%M:%S UTC')}, "
                f"status={a['status']}"
            )
        lines.append("")
        lines.append(f"See {LOG_FILE} for the full history.")
        await ts.send_message("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        print(f"  (telegram alert failed: {e})")


def append_history(line: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN,
                        help="lookback window in minutes (default 60)")
    parser.add_argument("--no-telegram", action="store_true",
                        help="don't send telegram (for testing)")
    parser.add_argument("--no-state", action="store_true",
                        help="don't save state (re-alert all for testing)")
    args = parser.parse_args()

    print(f"\n{'='*72}")
    print(f"  WEBHOOK RECONCILIATION  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
    print(f"  Window: last {args.window_min} min")
    print('=' * 72)

    ngrok_reqs = fetch_ngrok_webhooks(args.window_min)
    log_entries = fetch_signals_log(args.window_min + 5)  # tolerance for clock skew

    print(f"  ngrok /webhook POSTs in window: {len(ngrok_reqs)}")
    print(f"  signals_log entries in window: {len(log_entries)}")
    print()

    state = load_state()
    alerted_ids = set(state.get("alerted_ids", []))

    anomalies = []
    for req in ngrok_reqs:
        kind = None
        if req["status"] != 200:
            kind = f"non-200 ({req['status']})"
        elif not match_ngrok_to_log(req, log_entries):
            kind = "delivered but not in signals_log"
        if kind:
            already = req["id"] in alerted_ids
            print(f"  ⚠️  {req['ts_utc'].strftime('%H:%M:%S UTC')}  "
                  f"{req['signal_type']:5} {req['symbol']:14} "
                  f"status={req['status']}  {kind}{' (already alerted)' if already else ''}")
            if not already:
                anomalies.append({**req, "kind": kind})
                alerted_ids.add(req["id"])

    if not anomalies:
        print("  ✓ all webhooks reconciled cleanly")

    # Send Telegram for new anomalies
    if anomalies and not args.no_telegram:
        asyncio.run(send_telegram_alert(anomalies))

    # Persist
    if not args.no_state:
        state["alerted_ids"] = list(alerted_ids)
        state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    # History line
    append_history(
        f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}  "
        f"window={args.window_min}m  ngrok={len(ngrok_reqs)}  "
        f"log={len(log_entries)}  anomalies={len(anomalies)}"
    )


if __name__ == "__main__":
    main()
