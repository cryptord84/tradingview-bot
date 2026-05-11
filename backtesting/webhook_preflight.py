"""Webhook pre-flight validator — synthetic BUYs to verify the bot's signal
pipeline doesn't crash on each execution lane.

WHY THIS EXISTS:
The 2026-05-10 FLOKI BUY was lost to an UnboundLocalError introduced when
the Binance.US lane was added — pre-execution code referenced `token_symbol`
before it was defined. This kind of bug ONLY surfaces when a real signal
hits production. This validator catches that class of bug ahead of time.

WHAT IT DOES:
POSTs synthetic BUY signals (one per execution lane) to /webhook with
`dry_run: true`. The bot processes each through the full pre-execution
pipeline (duplicate check, correlation, sizing, risk gates, lane routing)
but bails BEFORE swap broadcast. Claude decision is also bypassed in
dry-run for speed.

Result: green = pipeline works for that lane; red = bug to fix.

USAGE:
    venv/bin/python -m backtesting.webhook_preflight

Run after any change to trade_engine, position_monitor, or routing code.
Could also wire into pre-commit hook or launchd cron.
"""
from __future__ import annotations

import sys, os, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

# Config — webhook secret pulled from config.yaml
from app.config import get

API_BASE = "http://localhost:8000"
SECRET = (get("webhook", "secret", "") or "")

# Synthetic signals per lane — symbols chosen so the routing logic resolves
# correctly. Each represents the bot detecting a token on its native chain.
TESTS = [
    {
        "lane":   "solana",
        "symbol": "SOLUSDT",   # Solana SPL → Jupiter
        "atr":    2.39,
    },
    {
        "lane":   "solana_spl",
        "symbol": "PENGUUSDT", # Solana SPL (memecoin) → Jupiter
        "atr":    0.0005,
    },
    {
        "lane":   "evm",
        "symbol": "AAVEUSDT",  # EVM → OpenOcean (Arbitrum)
        "atr":    2.5,
    },
    {
        "lane":   "evm",
        "symbol": "UNIUSDT",
        "atr":    0.1,
    },
    {
        "lane":   "binance_us",
        "symbol": "FLOKIUSDT", # Binance.US CEX
        "atr":    0.0000008,
    },
]


def _build_signal(t: dict) -> dict:
    return {
        "secret": SECRET,
        "signal_type": "BUY",
        "symbol": t["symbol"],
        "entry_price_estimate": 1.0,  # placeholder; bot will fetch real price
        "confidence_score": 50,
        "suggested_leverage": 1,
        "suggested_position_size_percent": 9.0,
        "atr": t["atr"],
        "timeframe": "240",
        "strategy": "Preflight Validator (synthetic)",
        "dry_run": True,
    }


async def _wait_for_log_marker(marker: str, since_ts: float, timeout_s: int = 90) -> dict | None:
    """Tail bot.log looking for a DRY-RUN result line matching `marker` (symbol).
    Returns the parsed line or None on timeout.
    """
    log_path = "logs/bot.log"
    deadline = time.time() + timeout_s
    last_size = 0
    seen = b""
    while time.time() < deadline:
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size > last_size:
                    f.seek(max(last_size, size - 8192))
                    seen += f.read()
                    last_size = size
        except FileNotFoundError:
            pass
        for line in reversed(seen.splitlines()):
            txt = line.decode(errors="replace")
            if "DRY-RUN pass:" in txt and marker in txt:
                return {"line": txt, "match": True}
            if "Trade engine error" in txt and marker in txt:
                return {"line": txt, "match": False}
        await asyncio.sleep(0.5)
    return None


async def main():
    if not SECRET:
        print("ERROR: webhook.secret missing from config.yaml")
        return 1

    print(f"\nWebhook pre-flight validator @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {API_BASE}/webhook  (dry_run=True)\n")

    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for t in TESTS:
            payload = _build_signal(t)
            sym = t["symbol"]
            ts = time.time()
            try:
                r = await client.post(f"{API_BASE}/webhook", json=payload)
                if r.status_code != 200:
                    results.append({**t, "ok": False, "stage": "webhook_post",
                                    "error": f"HTTP {r.status_code}: {r.text[:160]}"})
                    print(f"  ✗ {sym:<12} POST failed: HTTP {r.status_code}")
                    continue
            except Exception as e:
                results.append({**t, "ok": False, "stage": "webhook_post", "error": str(e)[:160]})
                print(f"  ✗ {sym:<12} POST exception: {e}")
                continue

            # Wait for bot.log to confirm DRY-RUN pass for this symbol
            log_res = await _wait_for_log_marker(sym, ts, timeout_s=90)
            if log_res is None:
                results.append({**t, "ok": False, "stage": "wait_timeout",
                                "error": "no DRY-RUN log line within 90s"})
                print(f"  ⚠ {sym:<12} timeout — pipeline didn't reach dry_run gate")
            elif not log_res["match"]:
                results.append({**t, "ok": False, "stage": "trade_engine_error",
                                "error": log_res["line"][-180:]})
                print(f"  ✗ {sym:<12} pipeline error:\n        {log_res['line'][-180:]}")
            else:
                results.append({**t, "ok": True, "stage": "dry_run_pass",
                                "line": log_res["line"]})
                # Extract lane from log line
                lane = "?"
                for token in log_res["line"].split():
                    if token.startswith("lane="):
                        lane = token.split("=", 1)[1]
                print(f"  ✓ {sym:<12} PASS (lane={lane})")

            # Small pause so signals don't queue-collide with dedup window
            await asyncio.sleep(2)

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    print(f"\nResult: {passed}/{total} lanes passed pre-flight")
    if passed < total:
        print("FAILURES:")
        for r in results:
            if not r["ok"]:
                print(f"  - {r['symbol']:<12} {r['stage']}: {r.get('error','?')[:160]}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
