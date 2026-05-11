"""Dry-run EVM swap validation for the bull-roster.

Goals:
  1. Verify the bot's EVMSwapExecutor can quote UNI/ARB swaps (Phase 4 readiness).
  2. Confirm INJ is broken at the registry level (latent bug discovered 2026-05-08).
  3. Report which bull-roster combos are executable_now vs deferred.

Does NOT broadcast — uses dry_run=True throughout. No gas or capital spent.
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.evm_wallet_service import EVMWalletService
from app.services.evm_swap_executor import EVMSwapExecutor
from app.services.trade_engine import TradeEngine

USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDC_DECIMALS = 6
TEST_USD = 5  # $5 worth of USDC

async def validate_token(executor, symbol, addr, decimals):
    """Get a dry-run quote. Returns (ok, info)."""
    amount_wei = int(TEST_USD * (10 ** USDC_DECIMALS))
    try:
        result = await executor.execute_swap(
            src_token=USDC_ARB,
            src_decimals=USDC_DECIMALS,
            dst_token=addr,
            dst_decimals=decimals,
            amount_wei=amount_wei,
            slippage_bps=100,
            dry_run=True,
            wait_for_swap_receipt=False,
        )
        q = result.get("quote", {})
        out_amount = q.get("dest_amount")
        return True, f"OK  ${TEST_USD} → {out_amount/(10**decimals):,.4f} {symbol} (dry run)"
    except Exception as e:
        return False, f"FAIL  {type(e).__name__}: {str(e)[:120]}"


async def main():
    wallet = EVMWalletService()
    executor = EVMSwapExecutor(wallet)

    print("=" * 78)
    print("  PHASE 4 EVM LANE VALIDATION  (dry-run, no broadcast)")
    print("=" * 78)
    print(f"  Wallet: {wallet.address}")
    print(f"  Chain:  {wallet.chain_id} (Arbitrum)")
    print()

    # Use the registry from the trade engine
    registry = TradeEngine.EVM_TOKENS
    bull_roster_evm = ["UNI", "ARB", "INJ"]  # FLOKI not in registry, no Arbitrum liquidity

    print(f"{'Symbol':<8} {'Address':<46} {'Result'}")
    print("-" * 78)
    results = {}
    for sym in bull_roster_evm:
        if sym not in registry:
            print(f"{sym:<8} {'(not in registry)':<46} SKIP")
            results[sym] = (False, "not in registry")
            continue
        addr, dec = registry[sym]
        ok, info = await validate_token(executor, sym, addr, dec)
        print(f"{sym:<8} {addr:<46} {info}")
        results[sym] = (ok, info)

    print()
    print("=" * 78)
    print("  BULL-ROSTER EXECUTION-READINESS")
    print("=" * 78)
    bull_roster = [
        ("Donch+ADX / FLOKI / 4H",   "FLOKI",  "deferred — no Arbitrum liquidity"),
        ("Donch+ADX / SOL / 4H",     "SOL",    "Solana via Jupiter (not tested here)"),
        ("EMA+ADX / SOL / 4H",       "SOL",    "Solana via Jupiter"),
        ("EMA+ADX / UNI / 4H",       "UNI",    None),
        ("EMA+ADX / ARB / 4H",       "ARB",    None),
        ("Liq Sweep / UNI / 4H",     "UNI",    None),
        ("Liq Sweep / FLOKI / 4H",   "FLOKI",  "deferred — no Arbitrum liquidity"),
    ]
    for combo, sym, note in bull_roster:
        if note:
            status = note
        elif sym in results:
            ok, info = results[sym]
            status = "✓ EVM ready" if ok else f"✗ broken: {info}"
        else:
            status = "?"
        print(f"  {combo:<32} → {status}")

    print()
    await wallet.close()


if __name__ == "__main__":
    asyncio.run(main())
