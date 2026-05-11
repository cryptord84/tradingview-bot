"""Phase 1 smoke test — verifies Binance.US API connectivity + read access.

Run after putting BINANCE_US_API_KEY / BINANCE_US_API_SECRET in `.env`:

    venv/bin/python -m backtesting.binance_us_smoke_test

Checks:
  1. Public ping — basic connectivity
  2. Server time skew — must be < 2000ms or signed requests will fail
  3. Auth probe — fetches account info (fails if key revoked or IP not whitelisted)
  4. Balance enumeration — shows free balance per asset
  5. USDT-equivalent total — what would be added to tradeable_usd
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.binance_us_client import BinanceUSClient


async def main():
    print("=" * 70)
    print("  BINANCE.US PHASE 1 SMOKE TEST")
    print("=" * 70)

    c = BinanceUSClient()
    print(f"\n  Enabled in config:  {c.enabled}")
    print(f"  API key set in env: {bool(c.api_key)}")
    print(f"  Secret set in env:  {bool(c.api_secret)}")

    if not c.enabled:
        print("\n  ✗ client disabled — check config.yaml binance_us.enabled + .env vars")
        await c.close()
        return

    print("\n--- Health check ---")
    h = await c.health_check()
    ping_status = "✓" if h.get("ping_ok") else "✗"
    print(f"  {ping_status} ping: {h.get('ping_ok')}")
    skew = h.get("time_skew_ms")
    if skew is not None:
        skew_status = "✓" if abs(skew) < 2000 else "✗"
        print(f"  {skew_status} clock skew: {skew}ms (must be < ±2000ms)")
    auth_status = "✓" if h.get("auth_ok") else "✗"
    print(f"  {auth_status} auth: {h.get('auth_ok')}")
    if not h.get("auth_ok"):
        err = h.get("auth_error", "unknown")
        print(f"      error: {err}")
        if "-2015" in err or "Invalid API-key" in err:
            print(f"      → API key invalid OR IP not whitelisted (most common cause)")
        elif "-1021" in err:
            print(f"      → Clock skew too large; sync system time")

    if not h.get("success"):
        print("\n  Stopping — health check failed.")
        await c.close()
        return

    print("\n--- Balances (non-zero only) ---")
    b = await c.get_balances()
    if b.get("success"):
        bal = b["data"]
        if not bal:
            print("  (no balances — fund the account with some USDT to trade)")
        for asset, amts in sorted(bal.items()):
            print(f"  {asset:8} free={amts['free']:>10.4f}  locked={amts['locked']:>10.4f}")

    print("\n--- USDT-equivalent for tradeable_usd ---")
    total = await c.get_total_usdt_value()
    print(f"  Sum of free USDT/USD/USDC: ${total:.2f}")
    print()
    print("  Once Phase 2 is enabled, this will be added to:")
    print("    tradeable_usd = wallet_USDC + kamino_USDC + binance_USDT - reserve")

    print("\n" + "=" * 70)
    print("  ✓ Phase 1 smoke test complete. Ready for Phase 2 (trading).")
    print("=" * 70)
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
