"""Periodic Binance.US connectivity check (cron-fired every 30 min).

Pings the API and runs a signed auth probe. Telegrams on failure — most
likely cause is IP whitelist break (ISP changed your home IP) or key
revocation.

Idempotent + stateless: alerts only on transitions (healthy→unhealthy or
extended downtime). State persisted to backtesting/results/binance_us_health.json.

Loaded via ~/Library/LaunchAgents/com.tradingbot.binance-us-health.plist.
"""
from __future__ import annotations

import sys, os, json, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from app.services.binance_us_client import BinanceUSClient

STATE_FILE = "backtesting/results/binance_us_health.json"
LOG_FILE = "backtesting/results/binance_us_health.log"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"healthy": True, "consecutive_failures": 0, "last_alert_utc": None}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"healthy": True, "consecutive_failures": 0, "last_alert_utc": None}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_log(line: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


async def send_telegram(msg: str) -> None:
    try:
        from app.services.telegram_service import TelegramService
        ts = TelegramService()
        await ts.send_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"  (telegram alert failed: {e})")


async def main():
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Binance.US health check ({now_iso}) ===")

    c = BinanceUSClient()
    h = await c.health_check()
    await c.close()

    healthy = bool(h.get("success"))
    state = load_state()
    was_healthy = state.get("healthy", True)
    consecutive = state.get("consecutive_failures", 0)

    if healthy:
        consecutive = 0
        recovered = not was_healthy
        print(f"  ✓ healthy (skew={h.get('time_skew_ms')}ms)")
        if recovered:
            await send_telegram(
                "<b>✅ Binance.US recovered</b>\n\n"
                "Auth probe passing again. Trading lane is back online."
            )
    else:
        consecutive += 1
        err = h.get("auth_error", "unknown")
        print(f"  ✗ FAILED (consecutive={consecutive}): {err}")
        # Alert on first failure transition + every 12 checks (~6h) if still down
        should_alert = was_healthy or (consecutive % 12 == 0)
        if should_alert:
            hint = ""
            if "-2015" in err or "Invalid API-key" in err:
                hint = "\n→ Most likely: ISP changed your IP (whitelist break). Check Binance.US API key page."
            elif "-1021" in err:
                hint = "\n→ Clock skew too large. Sync system time."
            await send_telegram(
                f"<b>⚠️ Binance.US health check failed</b>\n\n"
                f"Consecutive failures: {consecutive}\n"
                f"Error: <code>{err[:200]}</code>{hint}"
            )

    state = {
        "healthy": healthy,
        "consecutive_failures": consecutive,
        "last_check_utc": now_iso,
        "last_alert_utc": state.get("last_alert_utc"),
    }
    save_state(state)
    append_log(
        f"{now_iso}  healthy={healthy}  consecutive_failures={consecutive}  "
        f"skew_ms={h.get('time_skew_ms')}  ping={h.get('ping_ok')}  auth={h.get('auth_ok')}"
    )


if __name__ == "__main__":
    asyncio.run(main())
