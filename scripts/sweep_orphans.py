#!/usr/bin/env python3
"""Find and liquidate untracked holdings across the Binance.US and Solana lanes.

An orphan is wallet inventory with no matching open `positions` row, so
position_monitor cannot see it: no TP, no SL, no exit. They accumulate from
closes that under-sell (Binance LOT_SIZE rounds down) and from positions marked
`abandoned` without ever being liquidated — e.g. BONK #60, whose recorded
1,139,641 tokens were still sitting in the wallet weeks later.

The EVM lane has its own script (sell_evm_orphans.py); this covers the other two.

CRITICAL — orphans can be PARTIAL. On 2026-08-16 the wallet held 502.58 ARB
while open position #84 accounted for only 107.5 of it. Treating "has an open
position" as "don't touch" would have missed ~$29, and selling the whole balance
would have liquidated a live position. So the untracked amount is computed as
(on-chain − sum of open-position quantities) per asset, and only that surplus is
sold.

Safety:
  * --dry-run by default; --execute required to trade.
  * Never sells more than the computed surplus.
  * Skips anything below --min-usd, and anything under the venue's min-notional
    (unsellable dust — trying just burns fees on a rejected order).
  * Binance quantities are quantized DOWN to stepSize.

    venv/bin/python scripts/sweep_orphans.py                 # preview
    venv/bin/python scripts/sweep_orphans.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import load_config  # noqa: E402

load_config()

from app.services.price_router import get_monitor_price  # noqa: E402
from app.utils.errors import describe  # noqa: E402

DB = os.path.join(ROOT, "data", "trades.db")
STABLE = {"USDT", "USDC", "USD", "BUSD", "SOL"}  # SOL is gas, never sweep it


def base(sym: str) -> str:
    return re.sub(r"(USDT|USDC)(\.P)?$", "", (sym or "").upper())


def tracked_qty() -> dict[str, float]:
    """Token quantity accounted for by OPEN positions, per base asset."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol, COALESCE(amount_sol,0) FROM positions WHERE status='open'"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, float] = {}
    for sym, qty in rows:
        out[base(sym)] = out.get(base(sym), 0.0) + float(qty or 0)
    return out


async def price_of(sym: str) -> float | None:
    try:
        return await asyncio.wait_for(get_monitor_price(sym), timeout=25)
    except Exception:
        return None


async def sweep_binance(tracked, min_usd, execute) -> list[str]:
    from app.services.binance_us_client import BinanceUSClient
    out = []
    b = BinanceUSClient()
    r = await asyncio.wait_for(b.get_balances(), timeout=45)
    if not r.get("success"):
        return [f"  binance: balance fetch failed: {r.get('error')}"]

    for asset, v in sorted(r["data"].items()):
        if asset in STABLE:
            continue
        held = float(v["free"])
        surplus = held - tracked.get(asset, 0.0)
        px = await price_of(asset)
        usd = surplus * px if px else 0.0
        note = f"  {asset:8} held={held:<14.6f} tracked={tracked.get(asset,0):<10.4f} surplus={surplus:<12.6f} ~${usd:,.2f}"

        if surplus <= 0:
            out.append(note + "   [fully tracked]")
            continue
        if usd < min_usd:
            out.append(note + f"   [skip: under ${min_usd}]")
            continue

        symbol = f"{asset}USDT"
        info = await b.get_symbol_info(symbol)
        if not info.get("success"):
            out.append(note + f"   [skip: {info.get('error')}]")
            continue
        qty = BinanceUSClient.quantize_qty(surplus, info["step_size"])
        notional = qty * (px or 0)
        if notional < info["min_notional"]:
            out.append(note + f"   [skip: ${notional:,.2f} < min_notional ${info['min_notional']}]")
            continue

        if not execute:
            out.append(note + f"   [WOULD SELL {qty} -> ~${notional:,.2f}]")
            continue
        try:
            res = await b.place_market_sell_base(symbol, qty)
            ok = res.get("success")
            oid = (res.get("data") or {}).get("orderId")
            out.append(note + f"   [SOLD {qty} order={oid} {'OK' if ok else 'FAILED: ' + str(res.get('error'))}]")
        except Exception as e:
            out.append(note + f"   [SELL FAILED: {describe(e)}]")
    return out


async def sweep_solana(tracked, min_usd, execute) -> list[str]:
    from app.services.wallet_service import WalletService
    from app.services.jupiter_client import JupiterClient
    from app.services.trade_engine import TradeEngine
    out = []
    w = WalletService()
    j = JupiterClient()
    balances = await asyncio.wait_for(w.get_spl_token_balances(), timeout=60)

    for asset, held in sorted(balances.items()):
        if asset in STABLE or not held or held <= 0:
            continue
        surplus = held - tracked.get(asset, 0.0)
        px = await price_of(asset)
        usd = surplus * px if px else 0.0
        note = f"  {asset:8} held={held:<18.6f} tracked={tracked.get(asset,0):<10.4f} surplus={surplus:<16.6f} ~${usd:,.2f}"

        if surplus <= 0:
            out.append(note + "   [fully tracked]")
            continue
        if usd < min_usd:
            out.append(note + f"   [skip: under ${min_usd}]")
            continue

        mint = TradeEngine._KNOWN_MINTS.get(asset)
        dec = TradeEngine._TOKEN_DECIMALS.get(asset)
        if not mint or dec is None:
            out.append(note + "   [skip: unknown mint/decimals]")
            continue

        lamports = int(surplus * (10 ** dec))
        if not execute:
            out.append(note + f"   [WOULD SWAP {lamports} lamports -> USDC]")
            continue
        try:
            res = await j.execute_swap(
                keypair=w.get_keypair(), input_mint=mint,
                output_mint=j.usdc_mint, amount_lamports=lamports,
            )
            ok = res.get("success")
            out.append(note + f"   [SWAPPED tx={res.get('tx_id') or res.get('signature')} "
                              f"{'OK' if ok else 'FAILED: ' + str(res.get('error'))}]")
        except Exception as e:
            out.append(note + f"   [SWAP FAILED: {describe(e)}]")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--min-usd", type=float, default=1.00,
                    help="ignore surplus worth less than this (unsellable dust)")
    ap.add_argument("--lane", choices=["binance", "solana", "both"], default="both")
    a = ap.parse_args()

    tracked = tracked_qty()
    print(f"Tracked by open positions: { {k: round(v,4) for k,v in tracked.items()} or 'none'}")
    print(f"Mode: {'EXECUTE' if a.execute else 'DRY-RUN'}   min-usd=${a.min_usd}\n")

    if a.lane in ("binance", "both"):
        print("=== BINANCE.US ===")
        for line in await sweep_binance(tracked, a.min_usd, a.execute):
            print(line)
    if a.lane in ("solana", "both"):
        print("\n=== SOLANA ===")
        for line in await sweep_solana(tracked, a.min_usd, a.execute):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
