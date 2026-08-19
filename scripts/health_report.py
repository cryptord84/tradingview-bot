#!/usr/bin/env python3
"""One-shot health + performance report for a session-start check.

Built 2026-08-10. Reproduces, in one command, the investigation that otherwise
takes ~20 manual queries at the top of every session: liveness, loss-budget
headroom, realized P&L, signal dispositions, live-roster vs walk-forward
divergence, and a log error scan.

    venv/bin/python scripts/health_report.py            # default 7d window
    venv/bin/python scripts/health_report.py --days 1   # since last check
    venv/bin/python scripts/health_report.py --quiet    # findings only

Exit code is the number of FINDINGS, so a scheduler can alert only when the
report has something to say — the same silent-unless-notable rule the smoke
test follows. Read-only: never writes to the DB or touches the bot.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trades.db")
LOG = os.path.join(ROOT, "logs", "bot.log")
RESULTS = os.path.join(ROOT, "backtesting", "results")

FINDINGS: list[str] = []


def flag(msg: str) -> None:
    FINDINGS.append(msg)


def q(sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def hdr(title: str) -> None:
    print(f"\n{'─' * 66}\n{title}\n{'─' * 66}")


# ── sections ────────────────────────────────────────────────────────────────
def section_liveness() -> None:
    hdr("LIVENESS")
    try:
        import urllib.request, json
        with urllib.request.urlopen("http://localhost:8000/health", timeout=8) as r:
            data = json.loads(r.read().decode())
        print(f"  /health          {r.status} {data.get('status')}")
        for name, svc in (data.get("services") or {}).items():
            if svc.get("enabled") is False and not svc.get("running"):
                continue
            state = "running" if svc.get("running") else "STOPPED"
            if svc.get("running") is False and svc.get("enabled"):
                flag(f"service '{name}' is enabled but not running")
            print(f"    {name:18} {state}")
    except Exception as e:
        print(f"  /health          UNREACHABLE ({e})")
        flag(f"bot /health unreachable: {e}")


def section_budget() -> None:
    hdr("LOSS BUDGET")
    try:
        sys.path.insert(0, ROOT)
        from app.services.loss_budget import get_loss_budget
        s = get_loss_budget().status()
        bar = "TRIPPED" if s["tripped"] else "ok"
        print(f"  window           {s['window_days']}d, {s['trades']} closed trades")
        print(f"  realized         ${s['realized_usd']:.2f}")
        print(f"  budget           -${s['budget_usd']:.2f}   remaining ${s['remaining_usd']:.2f}   [{bar}]")
        if s["tripped"]:
            flag(f"LOSS BUDGET TRIPPED — new entries halted (${s['realized_usd']:.2f} vs -${s['budget_usd']:.2f})")
        elif s["remaining_usd"] < 0.25 * s["budget_usd"]:
            flag(f"loss budget under 25% headroom (${s['remaining_usd']:.2f} left)")
    except Exception as e:
        print(f"  unavailable ({e})")


def section_pnl(days: int) -> None:
    hdr(f"REALIZED P&L")
    rows = q(
        """SELECT substr(closed_at,1,7), COUNT(*), ROUND(SUM(pnl_usdc),2),
                  SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END)
             FROM positions WHERE status!='open' AND closed_at IS NOT NULL
            GROUP BY 1 ORDER BY 1"""
    )
    print(f"  {'month':9} {'n':>4} {'pnl':>9} {'win%':>7}")
    for m, n, pnl, wins in rows:
        wr = (100.0 * wins / n) if n else 0.0
        print(f"  {m:9} {n:>4} {pnl:>9.2f} {wr:>6.1f}%")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = q(
        """SELECT COUNT(*), ROUND(COALESCE(SUM(pnl_usdc),0),2),
                  SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END)
             FROM positions WHERE status!='open' AND closed_at>=?""", (cutoff,))
    n, pnl, wins = r[0]
    print(f"\n  last {days}d: {n} closed, ${pnl:.2f}, {wins or 0} wins")

    hdr("EXIT QUALITY (all time)")
    for st, n, pnl, avg in q(
        """SELECT status, COUNT(*), ROUND(SUM(pnl_usdc),2), ROUND(AVG(pnl_percent),2)
             FROM positions WHERE status!='open' GROUP BY status ORDER BY COUNT(*) DESC"""):
        print(f"  {st:15} {n:>4}  ${pnl:>8.2f}  avg {avg if avg is not None else 0:>6.2f}%")
        if st == "closed_trail" and pnl is not None and pnl < 0 and n >= 10:
            flag(f"{st}: {n} exits, ${pnl:.2f} cumulative — exit logic is net-negative")


def section_open() -> None:
    hdr("OPEN POSITIONS")
    rows = q(
        """SELECT id, created_at, symbol, strategy, ROUND(entry_price,6),
                  ROUND(amount_usdc,2), ROUND(tp_price,6), ROUND(sl_price,6)
             FROM positions WHERE status='open' ORDER BY created_at""")
    if not rows:
        print("  none")
        return
    for pid, created, sym, strat, entry, usd, tp, sl in rows:
        age = ""
        try:
            opened = datetime.fromisoformat(created.replace("Z", ""))
            age = f"{(datetime.utcnow() - opened).days}d"
        except Exception:
            pass
        print(f"  #{pid:<4} {sym:16} {strat:24} ${usd:>7.2f}  entry {entry}  tp {tp}  sl {sl}  {age}")
        if age and age.rstrip("d").isdigit() and int(age.rstrip("d")) > 21:
            flag(f"position #{pid} ({sym}) open {age} — check for a stuck/orphaned record")


def section_signals(days: int) -> None:
    hdr(f"SIGNAL DISPOSITIONS (last {days}d, real TV signals)")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = q(
        """SELECT COALESCE(disposition,'(null)'), COUNT(*)
             FROM signals_log
            WHERE source_ip != '127.0.0.1' AND timestamp >= ?
            GROUP BY 1 ORDER BY 2 DESC""", (cutoff,))
    if not rows:
        print("  no signals in window")
        return
    total = sum(n for _, n in rows)
    for disp, n in rows:
        print(f"  {disp:28} {n:>4}  ({100.0*n/total:.0f}%)")

    # Failure modes worth waking someone for
    bad = {d: n for d, n in rows}
    stuck = bad.get("received", 0)
    if stuck:
        flag(f"{stuck} signals stuck at 'received' — engine never recorded a terminal disposition")
    for key in ("failed_swap", "failed_engine_error", "rejected_loss_budget"):
        if bad.get(key):
            flag(f"{bad[key]} signals hit {key} in the last {days}d")
    if bad.get("dropped_restart"):
        flag(f"{bad['dropped_restart']} signals dropped to restarts in the last {days}d")

    # Claude gate: infra failure vs genuine risk decision
    rows = q(
        """SELECT CASE WHEN claude_reasoning LIKE '%cli error%'
                         OR claude_reasoning LIKE '%timed out%'
                         OR claude_reasoning LIKE '%exited%' THEN 'infra' ELSE 'genuine' END,
                  COUNT(*)
             FROM trades WHERE reason='claude_reject' AND timestamp >= ?
            GROUP BY 1""", (cutoff,))
    if rows:
        d = dict(rows)
        print(f"\n  claude rejects: {d.get('genuine',0)} genuine / {d.get('infra',0)} infra-failure")
        # Needs a minimum sample: at ~1 reject/week a single timeout would flag
        # every run, which is how a check turns back into noise.
        if d.get("infra", 0) >= 3 and d.get("infra", 0) > d.get("genuine", 0):
            flag(f"Claude infra failures ({d.get('infra')}) exceed genuine rejects ({d.get('genuine',0)}) — gate is misfiring")


def section_roster(days: int = 60) -> None:
    hdr("LIVE ROSTER vs WALK-FORWARD PASSERS")
    # Consider BOTH the main and HTF runs — a combo can pass in either, and
    # they are separate files. Sorting the glob alphabetically alone picks
    # `_0600_htf` over `_0403`, which silently reported 0 passers.
    def _newest(pattern: str) -> str | None:
        f = sorted(glob.glob(os.path.join(RESULTS, pattern)))
        return f[-1] if f else None

    htf = _newest("nightly_*_htf_summary.txt")
    mains = [f for f in sorted(glob.glob(os.path.join(RESULTS, "nightly_*_summary.txt")))
             if not f.endswith("_htf_summary.txt")]
    sources = [f for f in (mains[-1] if mains else None, htf) if f]

    pass_syms: set[str] = set()
    if not sources:
        print("  no nightly summary found")
    for path in sources:
        with open(path) as fh:
            text = fh.read()
        m = re.search(r"Passing \(WF-validated\):\s*(\d+)", text)
        print(f"  {os.path.basename(path):42} passers={m.group(1) if m else '?'}")
        # The passing block sits between the "Tests:" header and "Top 15".
        block = text.split("Top 15")[0]
        for line in block.splitlines():
            if "PF=" not in line:
                continue
            parts = line.split()
            # Layout: <strategy words...> <SYMBOL> <TIMEFRAME> PF=...
            tf_i = next((i for i, p in enumerate(parts)
                         if re.fullmatch(r"\d+[DHMdhm]|1D|4H|1H", p)), None)
            if tf_i and tf_i >= 1:
                pass_syms.add(parts[tf_i - 1])
    print(f"  passing symbols: {', '.join(sorted(pass_syms)) or '(none)'}")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    live = q(
        """SELECT strategy, symbol, COUNT(*), ROUND(COALESCE(SUM(pnl_usdc),0),2)
             FROM positions WHERE created_at >= ? GROUP BY 1,2 ORDER BY 4""", (cutoff,))
    print(f"\n  live-traded combos (last {days}d):")
    overlap = 0
    for strat, sym, n, pnl in live:
        base = re.sub(r"(USDT|USDC)(\.P)?$", "", sym or "")
        mark = "WF" if base in pass_syms else "  "
        overlap += base in pass_syms
        print(f"    [{mark}] {strat:24} {sym:16} n={n:<3} ${pnl:>7.2f}")
    if live and overlap == 0:
        flag(f"none of the {len(live)} live-traded combos are WF passers — live roster has diverged from validation")


def section_errors(hours: int = 24) -> None:
    hdr(f"LOG ERRORS (last {hours}h)")
    if not os.path.exists(LOG):
        print("  no bot.log")
        return
    size = os.path.getsize(LOG)
    # Read a bounded tail; bot.log runs to tens of MB.
    with open(LOG, "rb") as fh:
        fh.seek(max(0, size - 8_000_000))
        text = fh.read().decode("utf-8", "replace")
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if "[ERROR]" not in line and "[CRITICAL]" not in line:
            continue
        if line[:19] < cutoff:
            continue
        key = re.sub(r"\d+", "N", line.split("] ", 2)[-1])[:70]
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        print("  none")
        return
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {n:>4}x  {key}")
    total = sum(counts.values())
    if total > 50:
        flag(f"{total} ERROR/CRITICAL log lines in the last {hours}h")


def section_jobs() -> None:
    """Scheduled-job health: exit codes, output freshness, bot CPU.

    Added 2026-08-19 after the user asked why the nightly checks had not caught
    two live problems. They hadn't because nothing here looked at the machine:
      * kalshi whale_tracker had the bot pinned at 76% CPU scanning for a lane
        that has not traded in 30+ days — no resource check existed.
      * com.tradingbot.price-source-audit had exited 1 (4-6 failing checks)
        every day for 12+ days — nothing read launchd exit codes.
    Both were invisible because the report only ever inspected trading DATA,
    never the jobs producing it.
    """
    hdr("SCHEDULED JOBS")

    # launchd exit codes
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=30).stdout
        bad = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2].startswith(("com.tradingbot", "com.clawbot")):
                status = parts[1]
                if status not in ("0", "-"):
                    bad.append((parts[2], status))
        if bad:
            for name, status in bad:
                print(f"  FAILING  {name}  (last exit {status})")
                flag(f"scheduled job {name} last exited {status}")
        else:
            print("  all tradingbot jobs last exited 0")
    except Exception as e:
        print(f"  launchctl unavailable ({type(e).__name__})")

    # Output freshness — a job can be "registered" and silently not producing.
    expected = {
        "integration_smoke_*.log": 36,
        "nightly_*_summary.txt": 36,
        "kalshi_nightly_*_summary.txt": 36,
    }
    now = datetime.now().timestamp()
    for pattern, max_age_h in expected.items():
        files = sorted(glob.glob(os.path.join(RESULTS, pattern)), key=os.path.getmtime)
        if not files:
            print(f"  {pattern:34} NO OUTPUT EVER")
            flag(f"no output ever produced for {pattern}")
            continue
        age_h = (now - os.path.getmtime(files[-1])) / 3600
        state = "ok" if age_h <= max_age_h else "STALE"
        print(f"  {pattern:34} {age_h:5.1f}h old  [{state}]")
        if age_h > max_age_h:
            flag(f"{pattern} output is {age_h:.0f}h old (expected within {max_age_h}h)")

    # Bot CPU — cheap proxy for a runaway background loop.
    try:
        pid = subprocess.run(["pgrep", "-f", "venv/bin/uvicorn main:app"],
                             capture_output=True, text=True, timeout=15).stdout.split()
        if pid:
            ps = subprocess.run(["ps", "-o", "%cpu=,rss=,etime=", "-p", pid[0]],
                                capture_output=True, text=True, timeout=15).stdout.split()
            cpu, rss, et = float(ps[0]), int(ps[1]) / 1024, ps[2]
            print(f"  bot pid {pid[0]}: {cpu:.0f}% CPU, {rss:.0f}MB RSS, up {et}")
            if cpu > 50:
                flag(f"bot at {cpu:.0f}% CPU — check for a runaway background loop")
        else:
            print("  bot process NOT RUNNING")
            flag("bot process not running")
    except Exception as e:
        print(f"  cpu check unavailable ({type(e).__name__})")


def section_capital() -> None:
    hdr("CAPITAL")
    rows = q("SELECT timestamp, sol_usd, usdc_usd, tokens_usd, kamino_usd, kalshi_usd, total_usd "
             "FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
    if not rows:
        print("  no snapshots")
        return
    ts, sol, usdc, tok, kam, kal, tot = rows[0]
    print(f"  as of {ts[:19]}")
    for label, v in (("SOL", sol), ("USDC", usdc), ("tokens", tok), ("kamino", kam), ("kalshi", kal)):
        print(f"    {label:8} ${v or 0:>9.2f}")
    print(f"    {'TOTAL':8} ${tot or 0:>9.2f}")
    week = q("SELECT total_usd FROM portfolio_snapshots WHERE timestamp <= ? ORDER BY id DESC LIMIT 1",
             ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
    if week and week[0][0]:
        delta = (tot or 0) - week[0][0]
        print(f"    7d change  ${delta:+.2f}")
    if kal and kal > 50:
        idle = q("SELECT COUNT(*) FROM kalshi_trades WHERE timestamp >= ?",
                 ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
        if idle and idle[0][0] == 0:
            flag(f"${kal:.2f} sitting in Kalshi with zero trades in 30d — idle capital")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="window for P&L and signal stats")
    ap.add_argument("--hours", type=int, default=24, help="window for the log error scan")
    ap.add_argument("--quiet", action="store_true", help="print findings only")
    a = ap.parse_args()

    out = sys.stdout
    if a.quiet:
        sys.stdout = open(os.devnull, "w")

    print(f"\n{'=' * 66}\n  TRADING BOT HEALTH REPORT — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'=' * 66}")
    for fn in (section_liveness, section_budget):
        fn()
    section_pnl(a.days)
    section_open()
    section_signals(a.days)
    section_roster()
    section_jobs()
    section_errors(a.hours)
    section_capital()

    if a.quiet:
        sys.stdout.close()
        sys.stdout = out

    print(f"\n{'=' * 66}")
    if FINDINGS:
        print(f"  FINDINGS ({len(FINDINGS)})")
        for f in FINDINGS:
            print(f"    • {f}")
    else:
        print("  No findings — nothing needs attention.")
    print(f"{'=' * 66}\n")
    return len(FINDINGS)


if __name__ == "__main__":
    sys.exit(main())
