"""Funding rate scanner — monitors perp funding rates across venues for arb opportunities.

Checks Gate.io (public, US-accessible) for perp funding rates and Jupiter Perps
for borrow costs. When the spread (funding income − borrow cost) exceeds a
configurable threshold, emits a Telegram alert with the opportunity details.

The scanner doesn't execute trades — it surfaces opportunities for manual
review or future automation via Drift/Jupiter Perps.

USAGE:
    # One-shot scan (CLI)
    venv/bin/python -m app.services.funding_rate_scanner

    # As a cron (every 4 hours, aligned with funding intervals)
    Wired into main.py startup loop or launchd plist.

VENUES:
    - Gate.io:       Traditional 8h funding rate (public API, no auth, US-ok)
    - Jupiter Perps: Hourly borrow rate model (short pays borrow, no funding income)
    - Drift:         Traditional funding rate on Solana (DLOB API, intermittent)

ARB STRUCTURE:
    Long spot (Jupiter DEX) + Short perps (Drift or external venue).
    Net yield = funding rate received as short − borrow cost − entry/exit fees.
    Only profitable when funding rates spike (>0.02%/8h sustained).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("bot.funding")

GATE_IO_BASE = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
JUP_PERPS_BASE = "https://perps-api.jup.ag/v1"

TOKENS = {
    "SOL":      {"gate": "SOL_USDT",      "jup_mint": "So11111111111111111111111111111111111111112"},
    "ETH":      {"gate": "ETH_USDT",      "jup_mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"},
    "BTC":      {"gate": "BTC_USDT",      "jup_mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"},
    "DOGE":     {"gate": "DOGE_USDT",     "jup_mint": None},
    "ARB":      {"gate": "ARB_USDT",      "jup_mint": None},
    "UNI":      {"gate": "UNI_USDT",      "jup_mint": None},
    "COMP":     {"gate": "COMP_USDT",     "jup_mint": None},
    "LDO":      {"gate": "LDO_USDT",      "jup_mint": None},
    "AAVE":     {"gate": "AAVE_USDT",     "jup_mint": None},
    "RENDER":   {"gate": "RENDER_USDT",   "jup_mint": None},
    "BONK":     {"gate": "BONK_USDT",     "jup_mint": None},
    "PENGU":    {"gate": "PENGU_USDT",    "jup_mint": None},
    "FLOKI":    {"gate": "FLOKI_USDT",    "jup_mint": None},
    "FARTCOIN": {"gate": "FARTCOIN_USDT", "jup_mint": None},
    "PNUT":     {"gate": "PNUT_USDT",     "jup_mint": None},
    "MOODENG":  {"gate": "MOODENG_USDT",  "jup_mint": None},
    "JUP":      {"gate": "JUP_USDT",      "jup_mint": None},
    "WIF":      {"gate": "WIF_USDT",      "jup_mint": None},
    "OP":       {"gate": "OP_USDT",       "jup_mint": None},
    "LINK":     {"gate": "LINK_USDT",     "jup_mint": None},
}

MIN_NET_APR_ALERT = 15.0      # only alert if net APR > this
MIN_FUNDING_RATE_8H = 0.02    # 0.02% per 8h minimum to consider


@dataclass
class FundingSnapshot:
    token: str
    timestamp: str
    gate_rate_8h: Optional[float] = None
    gate_next_rate_8h: Optional[float] = None
    gate_mark_price: Optional[float] = None
    gate_interval_hours: int = 8
    jup_short_borrow_hourly: Optional[float] = None
    jup_open_fee_pct: Optional[float] = None
    net_apr: Optional[float] = None
    viable: bool = False
    error: Optional[str] = None


async def fetch_gate_rates(client: httpx.AsyncClient) -> dict[str, dict]:
    """Fetch funding rates from Gate.io for all tokens."""
    results = {}
    for token, cfg in TOKENS.items():
        contract = cfg["gate"]
        try:
            r = await client.get(f"{GATE_IO_BASE}/{contract}", timeout=8)
            if r.status_code == 200:
                d = r.json()
                rate = float(d.get("funding_rate", 0))
                next_rate = float(d.get("funding_rate_indicative", 0))
                mark = float(d.get("mark_price", 0))
                interval = int(d.get("funding_interval", 28800))
                results[token] = {
                    "rate_8h": rate * 100,  # convert to percentage
                    "next_rate_8h": next_rate * 100,
                    "mark_price": mark,
                    "interval_hours": interval // 3600,
                }
            else:
                results[token] = {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            results[token] = {"error": str(e)[:80]}
    return results


async def fetch_jup_borrow_rates(client: httpx.AsyncClient) -> dict[str, dict]:
    """Fetch borrow rates from Jupiter Perps (SOL/ETH/BTC only)."""
    results = {}
    for token, cfg in TOKENS.items():
        mint = cfg.get("jup_mint")
        if not mint:
            continue
        try:
            r = await client.get(f"{JUP_PERPS_BASE}/pool-info?mint={mint}", timeout=8)
            if r.status_code == 200:
                d = r.json()
                results[token] = {
                    "short_borrow_hourly": float(d["shortBorrowRatePercent"]),
                    "long_borrow_hourly": float(d["longBorrowRatePercent"]),
                    "short_utilization": float(d["shortUtilizationPercent"]),
                    "open_fee_pct": float(d["openFeePercent"]),
                }
        except Exception as e:
            results[token] = {"error": str(e)[:80]}
    return results


def compute_snapshots(
    gate: dict[str, dict],
    jup: dict[str, dict],
) -> list[FundingSnapshot]:
    """Combine venue data into unified snapshots with net APR calculation."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    snapshots = []

    for token in TOKENS:
        snap = FundingSnapshot(token=token, timestamp=ts)
        g = gate.get(token, {})
        j = jup.get(token, {})

        if "error" in g:
            snap.error = g["error"]
            snapshots.append(snap)
            continue

        snap.gate_rate_8h = g.get("rate_8h")
        snap.gate_next_rate_8h = g.get("next_rate_8h")
        snap.gate_mark_price = g.get("mark_price")
        snap.gate_interval_hours = g.get("interval_hours", 8)

        if j and "error" not in j:
            snap.jup_short_borrow_hourly = j.get("short_borrow_hourly")
            snap.jup_open_fee_pct = j.get("open_fee_pct")

        if snap.gate_rate_8h is not None:
            periods_per_day = 24 / snap.gate_interval_hours
            funding_apr = snap.gate_rate_8h * periods_per_day * 365

            borrow_apr = 0.0
            if snap.jup_short_borrow_hourly is not None:
                borrow_apr = snap.jup_short_borrow_hourly * 24 * 365

            entry_exit_cost = 0.0
            if snap.jup_open_fee_pct is not None:
                entry_exit_cost = snap.jup_open_fee_pct * 2  # open + close

            snap.net_apr = funding_apr - borrow_apr
            snap.viable = (
                snap.gate_rate_8h >= MIN_FUNDING_RATE_8H
                and snap.net_apr >= MIN_NET_APR_ALERT
            )

        snapshots.append(snap)

    snapshots.sort(key=lambda s: abs(s.gate_rate_8h or 0), reverse=True)
    return snapshots


def format_report(snapshots: list[FundingSnapshot]) -> str:
    """Format snapshots into a readable report."""
    lines = [
        f"Funding Rate Scan — {snapshots[0].timestamp if snapshots else 'N/A'}",
        f"{'Token':<10} {'Rate/8h':>9} {'Next':>9} {'APR':>8} {'Net APR':>9} {'Mark':>12} {'':>6}",
        "─" * 70,
    ]

    viable_count = 0
    for s in snapshots:
        if s.error:
            lines.append(f"{s.token:<10} {'err':>9}")
            continue

        rate_str = f"{s.gate_rate_8h:+.4f}%" if s.gate_rate_8h is not None else "N/A"
        next_str = f"{s.gate_next_rate_8h:+.4f}%" if s.gate_next_rate_8h is not None else "N/A"

        periods = 24 / s.gate_interval_hours
        gross_apr = (s.gate_rate_8h or 0) * periods * 365
        apr_str = f"{gross_apr:+.0f}%"

        net_str = f"{s.net_apr:+.0f}%" if s.net_apr is not None else "—"
        mark_str = f"${s.gate_mark_price:,.4f}" if s.gate_mark_price else "—"
        flag = " ** ARB" if s.viable else ""
        if s.viable:
            viable_count += 1

        lines.append(f"{s.token:<10} {rate_str:>9} {next_str:>9} {apr_str:>8} {net_str:>9} {mark_str:>12}{flag}")

    lines.append("─" * 70)
    if viable_count:
        lines.append(f"{viable_count} viable arb(s) found (net APR > {MIN_NET_APR_ALERT}%, rate > {MIN_FUNDING_RATE_8H}%/8h)")
    else:
        lines.append(f"No viable arbs (need rate > {MIN_FUNDING_RATE_8H}%/8h AND net APR > {MIN_NET_APR_ALERT}%)")

    return "\n".join(lines)


def format_telegram_alert(viable: list[FundingSnapshot]) -> str:
    """Format viable opportunities for Telegram notification."""
    lines = ["🔄 Funding Rate Arb Alert\n"]
    for s in viable:
        periods = 24 / s.gate_interval_hours
        gross_apr = (s.gate_rate_8h or 0) * periods * 365
        lines.append(
            f"  {s.token}: {s.gate_rate_8h:+.4f}%/{s.gate_interval_hours}h "
            f"(gross {gross_apr:+.0f}% APR, net {s.net_apr:+.0f}% APR) "
            f"@ ${s.gate_mark_price:,.2f}"
        )
    lines.append(f"\nAction: long spot + short perps on high-rate tokens")
    return "\n".join(lines)


async def scan(notify: bool = True) -> list[FundingSnapshot]:
    """Run a full funding rate scan across all venues and tokens."""
    async with httpx.AsyncClient() as client:
        gate_data, jup_data = await asyncio.gather(
            fetch_gate_rates(client),
            fetch_jup_borrow_rates(client),
        )

    snapshots = compute_snapshots(gate_data, jup_data)
    report = format_report(snapshots)
    logger.info(f"\n{report}")

    viable = [s for s in snapshots if s.viable]
    if viable and notify:
        try:
            from app.services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(format_telegram_alert(viable))
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")

    return snapshots


async def run_loop(interval_seconds: int = 14400):
    """Run scanner in a loop (default every 4 hours)."""
    while True:
        try:
            await scan(notify=True)
        except Exception as e:
            logger.error(f"Funding rate scan error: {e}", exc_info=True)
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    import sys

    async def main():
        snapshots = await scan(notify="--notify" in sys.argv)
        print(format_report(snapshots))
        viable = [s for s in snapshots if s.viable]
        return 0 if not viable else 0

    sys.exit(asyncio.run(main()))
