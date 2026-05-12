"""Integration smoke test — runs nightly + on-demand.

Catches the class of bugs that hit production 2026-05-10 → 2026-05-12:
- Per-lane sizing returning $0 when funds are available (LDO bug)
- Price-source returning wildly off values (PENGU fake-TP $39.75)
- DB write-through paths being broken (sync_kalshi_positions DELETE-all)
- Calibrator file missing/malformed (BTCD calibrator regression)

Hard rules:
- No real swaps. Webhook dry_run=true bails before chain broadcast.
- Tolerant of soft failures (CG 429, single token glitches) — flags hard
  failures (lane sizing $0, calibrator missing, DB unreadable).
- Exit non-zero on FAIL so launchd can detect + Telegram.

Usage:
    venv/bin/python -m backtesting.integration_smoke_test
    venv/bin/python -m backtesting.integration_smoke_test --skip-webhook  # offline only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import httpx
from app.config import get

API_BASE = "http://127.0.0.1:8000"
SECRET = (get("webhook", "secret", "") or "")
LOG_PATH = os.path.join(ROOT, "logs", "bot.log")

# CoinGecko reference IDs for the price-source cross-check
_CG_IDS = {
    "SOL": "solana", "BTC": "bitcoin", "ETH": "ethereum",
    "JTO": "jito-governance-token", "BONK": "bonk",
    "JUP": "jupiter-exchange-solana", "PENGU": "pudgy-penguins",
    "FARTCOIN": "fartcoin", "POPCAT": "popcat",
    "AAVE": "aave", "ARB": "arbitrum",
    "UNI": "uniswap", "LDO": "lido-dao",
    "COMP": "compound-governance-token", "LINK": "chainlink",
    "FLOKI": "floki", "DOGE": "dogecoin",
}

# Per-lane sizing test inputs. ATR chosen so position_monitor TP/SL is sane.
LANE_TESTS = [
    {"lane": "solana",     "symbol": "SOLUSDT",   "atr": 2.39},
    {"lane": "solana",     "symbol": "PENGUUSDT", "atr": 0.0005},
    {"lane": "evm",        "symbol": "LDOUSDT",   "atr": 0.0143},
    {"lane": "evm",        "symbol": "UNIUSDT",   "atr": 0.1},
    {"lane": "binance_us", "symbol": "FLOKIUSDT", "atr": 0.0000008},
]

# Min trade_usd we expect for any lane. If the lane has any funds and the bot
# returns trade_usd < this floor, sizing is broken. Picked low enough to not
# false-positive on legitimately small positions.
MIN_TRADE_USD = 0.50


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Per-lane sizing
# ─────────────────────────────────────────────────────────────────────────────
async def test_lane_sizing(client: httpx.AsyncClient) -> list[TestResult]:
    """For each lane, POST a synthetic dry_run BUY and tail bot.log for the
    DRY-RUN pass line. Assert tradeable_usd > 0 AND trade_usd >= MIN_TRADE_USD.

    Would have caught 2026-05-12 LDO ($0 sizing) immediately.
    """
    results: list[TestResult] = []
    for t in LANE_TESTS:
        sym = t["symbol"]
        ts0 = time.time()
        payload = {
            "secret": SECRET,
            "signal_type": "BUY",
            "symbol": sym,
            "entry_price_estimate": 1.0,
            "confidence_score": 50,
            "suggested_leverage": 1,
            "suggested_position_size_percent": 9.0,
            "atr": t["atr"],
            "timeframe": "240",
            "strategy": "Integration smoke test (synthetic)",
            "dry_run": True,
        }
        try:
            r = await client.post(f"{API_BASE}/webhook", json=payload, timeout=15)
            if r.status_code != 200:
                results.append(TestResult(
                    f"lane_sizing[{sym}]", False,
                    f"webhook returned HTTP {r.status_code}: {r.text[:120]}",
                ))
                continue
        except Exception as e:
            results.append(TestResult(f"lane_sizing[{sym}]", False, f"POST failed: {e}"))
            continue

        # Tail bot.log for the DRY-RUN pass line for this symbol
        match = await _wait_for_dryrun_line(sym, since_ts=ts0, timeout_s=120)
        if match is None:
            results.append(TestResult(
                f"lane_sizing[{sym}]", False,
                "no DRY-RUN pass line within 120s — pipeline may have stalled",
            ))
            continue

        ok, tradeable, trade_usd, lane = match
        if not ok:
            results.append(TestResult(
                f"lane_sizing[{sym}]", False,
                f"pipeline error: {tradeable}",
            ))
            continue

        # Sizing assertions
        if trade_usd < MIN_TRADE_USD:
            results.append(TestResult(
                f"lane_sizing[{sym}]", False,
                f"trade_usd=${trade_usd:.2f} below floor ${MIN_TRADE_USD:.2f} on "
                f"lane={lane} (tradeable=${tradeable:.2f}) — "
                f"lane funds missing from sizing pool?",
            ))
            continue

        results.append(TestResult(
            f"lane_sizing[{sym}]", True,
            f"lane={lane} tradeable=${tradeable:.2f} trade=${trade_usd:.2f}",
        ))
        await asyncio.sleep(1)  # dedup window breathing room

    return results


async def _wait_for_dryrun_line(symbol: str, since_ts: float, timeout_s: int = 120):
    """Tail bot.log for a 'DRY-RUN pass: ... <symbol> ...' line written after
    since_ts. Returns (ok, tradeable_usd, trade_usd, lane) or None on timeout.
    Returns (False, error, 0, '') if a 'Trade engine error' line for the symbol
    is seen first.
    """
    deadline = time.time() + timeout_s
    last_size = 0
    seen = b""
    base_short = symbol.replace("USDT", "").replace("USD", "").replace(".P", "")

    while time.time() < deadline:
        try:
            with open(LOG_PATH, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size > last_size:
                    f.seek(max(last_size, size - 16384))
                    seen += f.read()
                    last_size = size
        except FileNotFoundError:
            await asyncio.sleep(1)
            continue

        # Scan tail-first
        for line in reversed(seen.splitlines()):
            txt = line.decode(errors="replace")
            # Match either USDT-suffixed or bare-base form in the log line
            if "DRY-RUN pass:" in txt and (symbol in txt or base_short in txt):
                # Parse: "DRY-RUN pass: BUY PENGU lane=solana tradeable=$X.XX trade=$Y.YY"
                tradeable = trade_usd = 0.0
                lane = ""
                for tok in txt.split():
                    if tok.startswith("tradeable=$"):
                        try: tradeable = float(tok.split("$", 1)[1])
                        except Exception: pass
                    elif tok.startswith("trade=$"):
                        try: trade_usd = float(tok.split("$", 1)[1])
                        except Exception: pass
                    elif tok.startswith("lane="):
                        lane = tok.split("=", 1)[1]
                return (True, tradeable, trade_usd, lane)
            if "Trade engine error" in txt and (symbol in txt or base_short in txt):
                return (False, txt[-200:], 0.0, "")
        await asyncio.sleep(1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Per-lane price source quality
# ─────────────────────────────────────────────────────────────────────────────
async def test_price_source_quality() -> list[TestResult]:
    """Call get_monitor_price() for each tracked token + CoinGecko reference.
    Assert venue price within ±50% of CG. Catches feed corruption (PENGU fake-
    $39.75) and zombie listings (JUPUSDT $0.10 vs Solana $0.19, 2026-05-06).
    """
    from app.services.price_router import get_monitor_price

    results: list[TestResult] = []

    # Fetch CG reference in one batched call
    cg_ref: dict[str, float] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as cg:
            ids = ",".join(_CG_IDS.values())
            r = await cg.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"},
            )
            if r.status_code == 200:
                cg_data = r.json()
                inv = {v: k for k, v in _CG_IDS.items()}
                for cg_id, vals in cg_data.items():
                    sym = inv.get(cg_id)
                    p = (vals or {}).get("usd")
                    if sym and p:
                        cg_ref[sym] = float(p)
            elif r.status_code == 429:
                # Rate-limited — skip the whole test rather than false-flag
                results.append(TestResult(
                    "price_source_quality", True,
                    "skipped — CoinGecko 429 rate-limited (not a venue bug)",
                ))
                return results
    except Exception as e:
        results.append(TestResult(
            "price_source_quality", False,
            f"CoinGecko reference fetch failed: {e}",
        ))
        return results

    # Probe each tracked token
    drifts = []
    for sym, cg_price in cg_ref.items():
        try:
            venue_price = await get_monitor_price(sym)
        except Exception as e:
            results.append(TestResult(
                f"price_source[{sym}]", False, f"get_monitor_price raised: {e}",
            ))
            continue
        if venue_price is None or venue_price <= 0:
            results.append(TestResult(
                f"price_source[{sym}]", False,
                f"venue returned {venue_price} (CG: ${cg_price:.4f})",
            ))
            continue
        ratio = venue_price / cg_price
        drift_pct = (ratio - 1.0) * 100
        if ratio > 1.5 or ratio < 0.5:
            results.append(TestResult(
                f"price_source[{sym}]", False,
                f"venue=${venue_price:.4f} vs CG=${cg_price:.4f} ({drift_pct:+.0f}%) — out-of-band",
            ))
        else:
            drifts.append(abs(drift_pct))

    if drifts:
        avg_drift = sum(drifts) / len(drifts)
        results.append(TestResult(
            "price_source_quality", True,
            f"{len(drifts)} tokens within ±50% (avg drift {avg_drift:.1f}%)",
        ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: DB round-trip
# ─────────────────────────────────────────────────────────────────────────────
async def test_db_roundtrip() -> list[TestResult]:
    """Insert a synthetic open + closed kalshi position via sync, verify both
    are queryable, then clean up. Would have caught the DELETE-all sync bug.
    """
    from app.database import (
        sync_kalshi_positions, sync_kalshi_settlements,
        get_kalshi_positions, get_db,
    )
    results: list[TestResult] = []

    test_open = "KXSMOKE-26MAY12-T1"
    test_closed = "KXSMOKE-26MAY12-T2"

    # Cleanup any prior test rows first
    conn = get_db()
    conn.execute("DELETE FROM kalshi_positions WHERE ticker LIKE 'KXSMOKE-%'")
    conn.commit()
    conn.close()

    # Synthetic Kalshi-API position list
    fake_open = {
        "ticker": test_open, "position": 1, "market_exposure": 50,
        "total_traded_dollars": 0.50, "realized_pnl_dollars": 0.0,
        "last_updated_ts": datetime.utcnow().isoformat(),
        "event_ticker": "KXSMOKE", "title": "Smoke test open",
    }
    fake_closed = {
        "ticker": test_closed, "position": 0, "market_exposure": 0,
        "total_traded_dollars": 1.00, "realized_pnl_dollars": -0.25,
        "last_updated_ts": datetime.utcnow().isoformat(),
        "event_ticker": "KXSMOKE", "title": "Smoke test closed",
    }

    try:
        res = sync_kalshi_positions([fake_open, fake_closed])
    except Exception as e:
        results.append(TestResult("db_roundtrip[sync]", False, f"sync raised: {e}"))
        return results

    if res.get("inserted", 0) < 1:
        results.append(TestResult(
            "db_roundtrip[insert_open]", False,
            f"expected ≥1 open inserted, got {res.get('inserted')}: {res}",
        ))
    else:
        results.append(TestResult(
            "db_roundtrip[insert_open]", True, f"inserted {res.get('inserted')} open row(s)",
        ))

    if res.get("closed_new", 0) < 1:
        results.append(TestResult(
            "db_roundtrip[insert_closed]", False,
            f"expected closed_new ≥1, got {res.get('closed_new')} — DELETE-all bug class?",
        ))
    else:
        results.append(TestResult(
            "db_roundtrip[insert_closed]", True, f"persisted {res.get('closed_new')} closed row(s)",
        ))

    # Verify queryable
    opens = [r for r in get_kalshi_positions(status="open") if r["ticker"] == test_open]
    closed = [r for r in get_kalshi_positions(status="closed") if r["ticker"] == test_closed]
    if not opens:
        results.append(TestResult("db_roundtrip[query_open]", False, f"can't find {test_open}"))
    else:
        results.append(TestResult("db_roundtrip[query_open]", True, ""))
    if not closed:
        results.append(TestResult("db_roundtrip[query_closed]", False, f"can't find {test_closed}"))
    else:
        results.append(TestResult(
            "db_roundtrip[query_closed]", True,
            f"realized P&L stored: ${closed[0].get('pnl_cents', 0)/100:.2f}",
        ))

    # Second sync run — closed row should NOT be re-inserted (idempotency)
    res2 = sync_kalshi_positions([fake_open, fake_closed])
    if res2.get("closed_new", 0) > 0:
        results.append(TestResult(
            "db_roundtrip[idempotent]", False,
            f"closed row re-inserted on second sync — idempotency broken",
        ))
    else:
        results.append(TestResult("db_roundtrip[idempotent]", True, "closed-row dedupe works"))

    # Cleanup
    conn = get_db()
    conn.execute("DELETE FROM kalshi_positions WHERE ticker LIKE 'KXSMOKE-%'")
    conn.commit()
    conn.close()

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Calibrator integrity
# ─────────────────────────────────────────────────────────────────────────────
async def test_calibrator_integrity() -> list[TestResult]:
    """Verify data/btcd_calibration.json: file exists, breakpoints are valid,
    isotonic (monotonic non-decreasing y), apply() returns reasonable outputs.
    """
    from app.services.btcd_calibrator import get_btcd_calibrator
    results: list[TestResult] = []

    path = os.path.join(ROOT, "data", "btcd_calibration.json")
    if not os.path.exists(path):
        results.append(TestResult(
            "calibrator[file]", False,
            f"missing {path} — nightly rebuild may have failed",
        ))
        return results
    results.append(TestResult("calibrator[file]", True, ""))

    try:
        with open(path) as f:
            tbl = json.load(f)
    except Exception as e:
        results.append(TestResult("calibrator[parse]", False, f"JSON error: {e}"))
        return results

    bps = tbl.get("breakpoints", [])
    if len(bps) < 10:
        results.append(TestResult(
            "calibrator[breakpoints]", False,
            f"only {len(bps)} breakpoints — too sparse, training data may be missing",
        ))
        return results

    # Monotonicity check (y should be non-decreasing in x)
    bad_pairs = 0
    for i in range(len(bps) - 1):
        if bps[i + 1][1] < bps[i][1] - 0.001:
            bad_pairs += 1
    if bad_pairs > 0:
        results.append(TestResult(
            "calibrator[monotonic]", False,
            f"{bad_pairs} non-monotonic adjacent breakpoints — PAV may have failed",
        ))
    else:
        results.append(TestResult("calibrator[monotonic]", True, f"{len(bps)} breakpoints, all monotonic"))

    # apply() sanity — endpoints in (0, 1), 0.5 stays around 0.5
    cal = get_btcd_calibrator()
    out_05 = cal.apply(0.5)
    out_low = cal.apply(0.01)
    out_high = cal.apply(0.99)
    issues = []
    if not (0 < out_low < 0.1):
        issues.append(f"apply(0.01)={out_low:.4f} not in (0, 0.1)")
    if not (0.3 < out_05 < 0.7):
        issues.append(f"apply(0.50)={out_05:.4f} not in (0.3, 0.7)")
    if not (0.9 < out_high < 1.0):
        issues.append(f"apply(0.99)={out_high:.4f} not in (0.9, 1.0)")
    if issues:
        results.append(TestResult("calibrator[apply]", False, "; ".join(issues)))
    else:
        results.append(TestResult(
            "calibrator[apply]", True,
            f"0.01→{out_low:.3f}  0.50→{out_05:.3f}  0.99→{out_high:.3f}",
        ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
def _print_results(group: str, results: list[TestResult]):
    print(f"\n── {group} ──")
    for r in results:
        mark = "✓" if r.passed else "✗"
        line = f"  {mark} {r.name}"
        if r.detail:
            line += f"  ({r.detail})"
        print(line)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-webhook", action="store_true",
                        help="Skip the lane-sizing test (which requires the bot running)")
    args = parser.parse_args()

    print(f"Integration smoke test @ {datetime.now(timezone.utc).isoformat()}")
    print(f"Target: {API_BASE}")

    all_results: list[TestResult] = []

    # Test 1 — needs bot running
    if not args.skip_webhook:
        if not SECRET:
            all_results.append(TestResult(
                "lane_sizing", False, "webhook.secret missing from config",
            ))
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r1 = await test_lane_sizing(client)
                _print_results("Lane sizing (webhook dry-run)", r1)
                all_results.extend(r1)
            except Exception as e:
                msg = f"lane_sizing harness raised: {e}"
                all_results.append(TestResult("lane_sizing", False, msg))
                print(f"  ✗ {msg}")

    # Test 2 — price source quality
    try:
        r2 = await test_price_source_quality()
        _print_results("Price source quality vs CoinGecko", r2)
        all_results.extend(r2)
    except Exception as e:
        msg = f"price_source harness raised: {e}"
        all_results.append(TestResult("price_source", False, msg))
        print(f"  ✗ {msg}")

    # Test 3 — DB round-trip
    try:
        r3 = await test_db_roundtrip()
        _print_results("DB round-trip (sync_kalshi_positions)", r3)
        all_results.extend(r3)
    except Exception as e:
        msg = f"db_roundtrip harness raised: {e}"
        all_results.append(TestResult("db_roundtrip", False, msg))
        print(f"  ✗ {msg}")

    # Test 4 — calibrator
    try:
        r4 = await test_calibrator_integrity()
        _print_results("BTCD calibrator integrity", r4)
        all_results.extend(r4)
    except Exception as e:
        msg = f"calibrator harness raised: {e}"
        all_results.append(TestResult("calibrator", False, msg))
        print(f"  ✗ {msg}")

    # Summary
    passed = sum(1 for r in all_results if r.passed)
    total = len(all_results)
    print(f"\n=== Summary: {passed}/{total} passed ===")
    if passed < total:
        print("Failures:")
        for r in all_results:
            if not r.passed:
                print(f"  - {r.name}: {r.detail}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
