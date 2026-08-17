#!/usr/bin/env python3
"""Sell untracked ("orphan") ERC20 balances on Arbitrum back to USDC.

An orphan is a token the wallet holds with no corresponding open row in
`positions` — so position_monitor cannot see it, and it has no TP, no SL and no
exit path. Two were found 2026-08-11 (LDO, AAVE), both left behind by the
2026-05-30 DB-corruption recovery: the rebuild marked positions #4 and #8
`abandoned` but never sold the tokens, so the database forgot them while the
wallet kept them.

Safety properties:
  * --dry-run by default; --execute is required to broadcast.
  * Sells only the SURPLUS over what open positions account for, computed in
    wei. Orphans can be partial — on 2026-08-16 the Binance wallet held 502.58
    ARB against an open position's 107.5 — so neither "skip the whole symbol"
    nor "sell the whole balance" is correct.
  * Sells the EXACT wei balance (get_erc20_balance_wei), never a float
    round-trip, which over-requests and reverts. See position_monitor's
    _close_position_evm comment.
  * Distinguishes a broadcast-but-unconfirmed swap (PENDING) from one that
    never left (FAILED). A receipt timeout is NOT a failure — the tx is live.

    venv/bin/python scripts/sell_evm_orphans.py            # preview
    venv/bin/python scripts/sell_evm_orphans.py --execute  # broadcast
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
from decimal import Decimal
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import load_config  # noqa: E402

load_config()

from app.services.evm_wallet_service import EVMWalletService  # noqa: E402
from app.services.evm_swap_executor import EVMSwapExecutor  # noqa: E402
from app.services.trade_engine import TradeEngine  # noqa: E402
from app.utils.errors import describe  # noqa: E402

DB = os.path.join(ROOT, "data", "trades.db")


TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")


def classify_swap_result(res: dict, *, executed: bool) -> tuple[str, str]:
    """Turn an execute_swap return into (verdict, detail).

    execute_swap returns {quote, approve_tx, swap_tx, receipt}; it has no
    "success" key and signals failure by raising. Reading a key that does not
    exist is how the Solana sweep reported two completed swaps as
    "FAILED: None" on 2026-08-16, so the shape here is asserted against the
    real contract in evm_swap_executor.execute_swap.
    """
    qt = res.get("quote") or {}
    swap = res.get("swap_tx") or {}
    q = (f"{qt.get('src_amount')} -> {qt.get('dest_amount')} USDC "
         f"(~${qt.get('dest_usd')})")

    if not executed:
        return "DRY-RUN", f"{q} — not broadcast"

    receipt = res.get("receipt")
    if receipt is None:
        # Broadcast with no receipt awaited: outcome genuinely unknown.
        return "PENDING", (f"{q} — tx {swap.get('hash')} broadcast, no receipt "
                           f"awaited; verify on-chain")
    status = receipt.get("status")
    if status in ("0x1", 1):
        return "OK", f"{q} — tx {swap.get('hash')} mined"
    return "FAILED", f"{q} — tx {swap.get('hash')} reverted (status={status})"


def classify_swap_exception(exc: BaseException) -> tuple[str, str]:
    """Separate 'never broadcast' from 'broadcast, outcome unknown'.

    wait_for_receipt raises TimeoutError("tx 0x… not mined within Ns") AFTER
    send_raw_tx has already succeeded — the transaction is live and may confirm
    a moment later. Calling that a failure is precisely the mistake made on the
    Solana side: a swap that had landed was reported as failed, and the retry
    that followed then hit an empty source account. Surface it as PENDING with
    the hash so it gets verified rather than retried blindly.
    """
    text = str(exc)
    m = TX_HASH_RE.search(text)
    if isinstance(exc, TimeoutError) and m:
        return "PENDING", (f"receipt timed out but tx {m.group(0)} WAS broadcast — "
                           f"verify on-chain before re-running; it may still confirm")
    return "FAILED", describe(exc)


def tracked_qty() -> dict[str, float]:
    """Token quantity accounted for by OPEN positions, per base asset.

    Quantities, not just symbols: an orphan can be PARTIAL. On 2026-08-16 the
    Binance wallet held 502.58 ARB while open position #84 accounted for only
    107.5, so "this symbol has a position, skip it" would have left ~$29
    unmanaged. Selling the whole balance would have closed a live position.
    Only the surplus is sellable.
    """
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol, COALESCE(amount_sol,0) FROM positions WHERE status='open'"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, float] = {}
    for sym, qty in rows:
        s = (sym or "").upper()
        for suffix in ("USDT.P", "USDT", "USDC"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        out[s] = out.get(s, 0.0) + float(qty or 0)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="broadcast (default is dry-run)")
    ap.add_argument("--slippage-bps", type=int, default=300)
    ap.add_argument("--tokens", default="LDO,AAVE", help="comma-separated symbols")
    a = ap.parse_args()

    tracked = tracked_qty()
    print(f"Tracked by open positions: { {k: round(v, 4) for k, v in tracked.items()} or 'none'}")

    wallet = EVMWalletService()
    executor = EVMSwapExecutor(wallet)
    rc = 0
    try:
        for sym in [t.strip().upper() for t in a.tokens.split(",") if t.strip()]:
            if sym not in TradeEngine.EVM_TOKENS:
                print(f"\n{sym}: not an EVM token, skipping")
                continue
            addr, dec = TradeEngine.EVM_TOKENS[sym]
            raw = await wallet.get_erc20_balance_wei(addr)
            human = raw / (10 ** dec)
            # Surplus in WEI, so a live position's share is subtracted exactly.
            # Converting the tracked float to wei can round UP and eat into the
            # position's own tokens — the same float->wei trap fixed in
            # position_monitor on 2026-08-11.
            held_wei = int(Decimal(str(tracked.get(sym, 0.0))) * (Decimal(10) ** dec))
            raw = max(0, raw - held_wei)
            print(f"\n{sym}: balance {human:.8f} ({raw + held_wei} wei), "
                  f"tracked {tracked.get(sym, 0.0)} -> surplus {raw} wei")
            if raw <= 0:
                print("  nothing to sell (fully tracked or empty)")
                continue

            try:
                res = await executor.execute_swap(
                    src_token=addr, src_decimals=dec,
                    dst_token=TradeEngine.EVM_USDC_CONTRACT,
                    dst_decimals=TradeEngine.EVM_USDC_DECIMALS,
                    amount_wei=raw,                    # EXACT wei — never float
                    slippage_bps=a.slippage_bps,
                    dry_run=not a.execute,
                    wait_for_swap_receipt=a.execute,
                    receipt_timeout_s=180,
                )
                verdict, detail = classify_swap_result(res, executed=a.execute)
                print(f"  {verdict}: {detail}")
                if verdict == "FAILED":
                    rc += 1
            except Exception as e:
                verdict, detail = classify_swap_exception(e)
                print(f"  {verdict}: {detail}")
                if verdict == "FAILED":
                    rc += 1
    finally:
        await executor.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
