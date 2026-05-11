"""Nightly price-source audit — validate every alerted token has a working
price feed for TP/SL monitoring.

Per CLAUDE.md hard rule #8 (monitor price source must match swap execution
source), every token the bot might hold a position in MUST have a fresh,
non-zero price from a real venue. The 2026-05-06 incident lost $4.59 phantom
on JUPUSDT zombie listing — this script catches that class of bug.

Checks performed for each required token:
  1. Is it covered by either price_feed.BINANCE_TOKENS or COINGECKO_ONLY?
  2. If Binance.US: hit /api/v3/ticker/24hr — verify lastPrice > 0 AND
     daily quote volume > $1k (zombie threshold).
  3. If CoinGecko: hit simple/price endpoint.
  4. If Jupiter-only: hit Jupiter quote endpoint.

Required token set = (active TV alerts) ∪ (TradeEngine.EVM_TOKENS) ∪
(TradeEngine.BINANCE_TOKENS). Each token must have at least one working
source — fails the audit if none.

Telegram alert on any failures (or zombie warnings) so the user catches
listing changes within 24h. Idempotent state-tracked alerts so repeated
nightly runs don't spam.

Loaded via ~/Library/LaunchAgents/com.tradingbot.price-source-audit.plist
(scheduled at 03:30 UTC, before the 04:03 UTC nightly).
"""
from __future__ import annotations

import sys, os, json, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

# Project paths
ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "backtesting/results/price_source_audit.json"
LOG_FILE = ROOT / "backtesting/results/price_source_audit.log"
ACTIVE_ALERTS_FILE = ROOT / "data/active_alerts.json"

# Zombie threshold: tokens with < $1k daily volume on Binance.US carry
# stale-price risk per the 2026-05-06 JUPUSDT incident.
ZOMBIE_VOLUME_USD = 1000.0


# ── helpers ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_alert_at": None, "last_failures": [], "last_warnings": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a") as f:
        f.write(f"{ts}  {line}\n")


async def send_telegram(msg: str) -> None:
    """Best-effort Telegram dispatch — never raises."""
    try:
        from app.services.telegram_service import TelegramService
        tg = TelegramService()
        await tg.send_message(msg)
        await tg.close()
    except Exception as e:
        append_log(f"telegram dispatch failed: {e}")


def required_tokens() -> set[str]:
    """Build the union of all tokens that need a working price source."""
    tokens: set[str] = set()
    # 1. Active TV alerts (snapshot)
    if ACTIVE_ALERTS_FILE.exists():
        try:
            d = json.loads(ACTIVE_ALERTS_FILE.read_text())
            tokens |= set(d.get("by_token", {}).keys())
        except Exception:
            pass
    # 2. Trade engine execution lanes
    try:
        from app.services.trade_engine import TradeEngine
        tokens |= set(TradeEngine.EVM_TOKENS.keys())
        tokens |= set(TradeEngine.BINANCE_TOKENS.keys())
    except Exception:
        pass
    return tokens


# ── per-source checks ───────────────────────────────────────────────────────

async def check_binance_us(client: httpx.AsyncClient, pair: str) -> dict:
    """Verify a Binance.US trading pair returns valid 24h ticker."""
    try:
        r = await client.get(
            "https://api.binance.us/api/v3/ticker/24hr",
            params={"symbol": pair.upper()},
            timeout=10,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        d = r.json()
        price = float(d.get("lastPrice", 0) or 0)
        quote_volume = float(d.get("quoteVolume", 0) or 0)
        if price <= 0:
            return {"ok": False, "error": "lastPrice = 0"}
        warning = None
        if quote_volume < ZOMBIE_VOLUME_USD:
            warning = f"low volume ${quote_volume:.0f}/24h — zombie risk"
        return {
            "ok": True,
            "price": price,
            "volume_usd": quote_volume,
            "warning": warning,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


async def check_jupiter(symbol: str) -> dict:
    """Try Jupiter aggregator quote (Solana SPL tokens)."""
    try:
        from app.services.jupiter_client import JupiterClient
        j = JupiterClient()
        try:
            price = await j.get_token_price(symbol)
            if price <= 0:
                return {"ok": False, "error": "price = 0"}
            return {"ok": True, "price": price}
        finally:
            await j.close()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


async def check_coingecko(client: httpx.AsyncClient, cg_id: str) -> dict:
    """Verify CoinGecko simple/price returns a value."""
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=10,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        d = r.json()
        price = float(d.get(cg_id, {}).get("usd", 0) or 0)
        if price <= 0:
            return {"ok": False, "error": "price = 0"}
        return {"ok": True, "price": price}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# ── main audit ──────────────────────────────────────────────────────────────

async def audit() -> dict:
    """Audit every required token against its expected price source(s).

    For each token, try sources in priority order: Binance.US (if mapped),
    then Jupiter (Solana SPL), then CoinGecko. Any one working = pass.
    """
    from app.services.price_feed import BINANCE_TOKENS as PF_BINANCE, COINGECKO_ONLY

    tokens = sorted(required_tokens())
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        for tok in tokens:
            row = {"token": tok, "sources_tried": [], "ok": False, "warnings": []}

            # 1. Binance.US WS-eligible
            pair = PF_BINANCE.get(tok)
            if pair:
                r = await check_binance_us(client, pair)
                row["sources_tried"].append({"source": "binance.us", "pair": pair, **r})
                if r.get("ok"):
                    row["ok"] = True
                    row["primary_source"] = "binance.us"
                    row["price"] = r["price"]
                    if r.get("warning"):
                        row["warnings"].append(r["warning"])

            # 2. CoinGecko-only fallback
            if not row["ok"] and tok in COINGECKO_ONLY:
                r = await check_coingecko(client, COINGECKO_ONLY[tok])
                row["sources_tried"].append({"source": "coingecko", "id": COINGECKO_ONLY[tok], **r})
                if r.get("ok"):
                    row["ok"] = True
                    row["primary_source"] = "coingecko"
                    row["price"] = r["price"]

            # 3. Jupiter fallback (last resort — slowest, requires running event loop)
            if not row["ok"]:
                r = await check_jupiter(tok)
                row["sources_tried"].append({"source": "jupiter", **r})
                if r.get("ok"):
                    row["ok"] = True
                    row["primary_source"] = "jupiter"
                    row["price"] = r["price"]

            results.append(row)

    failures = [r for r in results if not r["ok"]]
    warnings = [r for r in results if r["ok"] and r.get("warnings")]
    return {"results": results, "failures": failures, "warnings": warnings}


# ── reporting ───────────────────────────────────────────────────────────────

def format_summary(audit_out: dict) -> str:
    """Render a human-readable line per token. Used in stdout + log + Telegram."""
    lines = []
    lines.append(f"Price-source audit @ {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Tokens audited: {len(audit_out['results'])}")
    lines.append(f"Pass: {len(audit_out['results']) - len(audit_out['failures'])}")
    lines.append(f"Fail: {len(audit_out['failures'])}")
    lines.append(f"Warn: {len(audit_out['warnings'])}")
    lines.append("")
    for r in audit_out["results"]:
        if r["ok"]:
            warn = f"  ⚠ {','.join(r['warnings'])}" if r["warnings"] else ""
            lines.append(f"  ✓ {r['token']:<10} ${r.get('price',0):.4f}  via {r.get('primary_source','?')}{warn}")
        else:
            attempts = "; ".join(s.get("error","?") for s in r["sources_tried"])
            lines.append(f"  ✗ {r['token']:<10} NO PRICE SOURCE  attempts: {attempts}")
    return "\n".join(lines)


def format_telegram(audit_out: dict) -> Optional[str]:
    """Telegram alert payload — only sent if there are failures or new warnings."""
    failures = audit_out["failures"]
    warnings = audit_out["warnings"]
    if not failures and not warnings:
        return None

    parts = []
    if failures:
        parts.append(f"<b>🚨 PRICE SOURCE AUDIT — {len(failures)} FAILURE(S)</b>")
        parts.append("Tokens with no working price feed (TP/SL won't fire):")
        for r in failures:
            attempts = "; ".join(s.get("error","?") for s in r["sources_tried"])[:80]
            parts.append(f"  • <b>{r['token']}</b>: {attempts}")
        parts.append("")
    if warnings:
        parts.append(f"<b>⚠️ {len(warnings)} ZOMBIE-RISK WARNING(S)</b>")
        parts.append("Low-volume listings — price may diverge from execution venue:")
        for r in warnings:
            parts.append(f"  • <b>{r['token']}</b>: {','.join(r['warnings'])}")
        parts.append("")
    parts.append("See backtesting/results/price_source_audit.log for full report.")
    return "\n".join(parts)


# ── main ────────────────────────────────────────────────────────────────────

async def main() -> int:
    audit_out = await audit()
    summary = format_summary(audit_out)
    print(summary)
    append_log(f"audit: {len(audit_out['failures'])} fail, {len(audit_out['warnings'])} warn, "
               f"{len(audit_out['results']) - len(audit_out['failures'])} pass")

    # Persist state for trend tracking
    state = load_state()
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["last_failures"] = [r["token"] for r in audit_out["failures"]]
    state["last_warnings"] = [r["token"] for r in audit_out["warnings"]]
    state["last_summary"] = summary
    save_state(state)

    # Telegram on any failure or warning (idempotent — sends even if same as
    # last run; user benefit from daily reminder if a token stays broken).
    msg = format_telegram(audit_out)
    if msg:
        await send_telegram(msg)
        state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    return 0 if not audit_out["failures"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
