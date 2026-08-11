#!/usr/bin/env python3
"""Nightly Telegram digest — what changed, and what needs a human eye.

Sends: decisions logged since the last digest (from DECISIONS.md), health-report
findings, the day's realized P&L, and loss-budget headroom.

Deliberately SKIPS the send when there is nothing to report — no new decisions
and no findings. The lesson from the integration smoke test is that a channel
which fires on a schedule regardless of content stops being read, and then a
real alert dies in the noise. --force overrides for testing.

    venv/bin/python scripts/nightly_digest.py            # send if notable
    venv/bin/python scripts/nightly_digest.py --dry-run  # print, never send
    venv/bin/python scripts/nightly_digest.py --force    # send regardless
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "trades.db")
DECISIONS = os.path.join(ROOT, "DECISIONS.md")
STATE = os.path.join(ROOT, "logs", "nightly_digest_state.json")

HEADER_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+)$")


def load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2)


def new_decisions(since_iso: str | None) -> list[tuple[str, str]]:
    """Decision headers newer than the last digest."""
    if not os.path.exists(DECISIONS):
        return []
    cutoff = (since_iso or "")[:10]
    out = []
    with open(DECISIONS) as fh:
        for line in fh:
            m = HEADER_RE.match(line.strip())
            if m and m.group(1) > cutoff:
                out.append((m.group(1), m.group(2)))
    return out


def q(sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def health_findings() -> list[str]:
    """Reuse health_report.py rather than reimplementing its checks."""
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "health_report.py"), "--quiet"],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
        )
        return [ln.strip().lstrip("• ").strip()
                for ln in r.stdout.splitlines() if ln.strip().startswith("•")]
    except Exception as e:
        return [f"(health report failed to run: {type(e).__name__})"]


def build_message(force: bool) -> tuple[str | None, dict]:
    state = load_state()
    since = state.get("last_sent")

    decisions = new_decisions(since)
    findings = health_findings()

    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = q("""SELECT COUNT(*), ROUND(COALESCE(SUM(pnl_usdc),0),2),
                       SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END)
                  FROM positions WHERE status!='open' AND closed_at>=?""", (day_ago,))
    n_closed, pnl, wins = rows[0] if rows else (0, 0.0, 0)

    opened = q("SELECT COUNT(*) FROM positions WHERE created_at>=?", (day_ago,))[0][0]
    open_now = q("SELECT COUNT(*) FROM positions WHERE status='open'")[0][0]

    try:
        from app.config import load_config
        load_config()
        from app.services.loss_budget import get_loss_budget
        b = get_loss_budget().status()
        budget_line = (f"Budget: ${b['realized_usd']:.2f} / -${b['budget_usd']:.2f} "
                       f"({b['window_days']}d)" + ("  ⛔ TRIPPED" if b["tripped"] else ""))
    except Exception:
        budget_line = "Budget: unavailable"

    if not decisions and not findings and not force:
        return None, state

    L = [f"🌙 <b>Nightly Digest — {datetime.now():%Y-%m-%d}</b>", ""]

    if decisions:
        L.append(f"<b>Decisions logged ({len(decisions)})</b>")
        for d, title in decisions:
            L.append(f"  • {d} — {title}")
        L.append("  <i>Review in DECISIONS.md — push back on anything.</i>")
        L.append("")

    L.append("<b>Trading (24h)</b>")
    L.append(f"  Opened {opened} · Closed {n_closed} · Open now {open_now}")
    L.append(f"  Realized: ${pnl:.2f} ({wins or 0} wins)")
    L.append(f"  {budget_line}")
    L.append("")

    if findings:
        L.append(f"<b>Needs attention ({len(findings)})</b>")
        for f in findings:
            L.append(f"  • {f}")
    else:
        L.append("<b>Needs attention</b>: none")

    return "\n".join(L), state


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    msg, state = build_message(a.force)
    if msg is None:
        print("Nothing notable — no digest sent.")
        return 0

    print(msg)
    if a.dry_run:
        print("\n[dry-run] not sent")
        return 0

    from app.services.telegram_service import TelegramService
    tg = TelegramService()
    await tg.send_message(msg)
    state["last_sent"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print("\nSent.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
