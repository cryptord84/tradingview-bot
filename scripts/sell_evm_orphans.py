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
  * REFUSES to sell any token that has an open position row — that would be
    selling live inventory out from under position_monitor.
  * Sells the EXACT wei balance (get_erc20_balance_wei), never a float
    round-trip, which over-requests and reverts. See position_monitor's
    _close_position_evm comment.

    venv/bin/python scripts/sell_evm_orphans.py            # preview
    venv/bin/python scripts/sell_evm_orphans.py --execute  # broadcast
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
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


def open_position_symbols() -> set[str]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT symbol FROM positions WHERE status='open'").fetchall()
    finally:
        conn.close()
    out = set()
    for (sym,) in rows:
        s = (sym or "").upper()
        for suffix in ("USDT.P", "USDT", "USDC"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        out.add(s)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="broadcast (default is dry-run)")
    ap.add_argument("--slippage-bps", type=int, default=300)
    ap.add_argument("--tokens", default="LDO,AAVE", help="comma-separated symbols")
    a = ap.parse_args()

    protected = open_position_symbols()
    print(f"Open positions (protected from sale): {sorted(protected) or 'none'}")

    wallet = EVMWalletService()
    executor = EVMSwapExecutor(wallet)
    rc = 0
    try:
        for sym in [t.strip().upper() for t in a.tokens.split(",") if t.strip()]:
            if sym not in TradeEngine.EVM_TOKENS:
                print(f"\n{sym}: not an EVM token, skipping")
                continue
            if sym in protected:
                print(f"\n{sym}: HAS AN OPEN POSITION — refusing to sell")
                continue

            addr, dec = TradeEngine.EVM_TOKENS[sym]
            raw = await wallet.get_erc20_balance_wei(addr)
            human = raw / (10 ** dec)
            print(f"\n{sym}: balance {human:.8f}  ({raw} wei)")
            if raw <= 0:
                print("  nothing to sell")
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
                qt = res.get("quote") or {}
                print(f"  quote: {qt.get('src_amount')} {sym} -> "
                      f"{qt.get('dest_amount')} USDC (~${qt.get('dest_usd')})")
                swap = res.get("swap_tx") or {}
                if a.execute:
                    receipt = res.get("receipt") or {}
                    status = receipt.get("status")
                    ok = status in ("0x1", 1)
                    print(f"  tx {swap.get('hash')}  status={status}  {'OK' if ok else 'FAILED'}")
                    if not ok:
                        rc += 1
                else:
                    print("  DRY-RUN — not broadcast")
            except Exception as e:
                print(f"  swap failed: {describe(e)}")
                rc += 1
    finally:
        await executor.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
