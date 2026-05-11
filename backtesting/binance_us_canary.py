"""Canary smoke test — $5 INJ market buy + immediate sell.

Validates the Binance.US trading lane end-to-end without going through the
full bot signal flow. Reports:
  - Pre-trade: USDT balance, INJ price, projected qty
  - Buy fill: actual qty + cum_quote (USDT spent) + avg fill price
  - Sell fill: qty sold + cum_quote (USDT received) + avg fill price
  - Net cost: USDT spent − USDT received (will be small: fees + spread)

Cost expected: ~$0.05–0.10 (0.4% taker × 2 trades + bid/ask spread).

Run: venv/bin/python -m backtesting.binance_us_canary
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.binance_us_client import BinanceUSClient

CANARY_USDT = 5.00       # Buy $5 of FLOKI. Plenty of headroom ($5171 max market order).
SYMBOL = "FLOKIUSDT"     # FLOKI was deferred for BSC; turns out it's on Binance.US
TOKEN = "FLOKI"


async def main():
    print("=" * 72)
    print(f"  BINANCE.US CANARY  — $5 {TOKEN} buy + immediate sell")
    print("=" * 72)

    bn = BinanceUSClient()
    if not bn.enabled:
        print("  ✗ client disabled — aborting")
        return

    # ── Pre-trade state ──────────────────────────────────────────────────
    print("\n--- PRE-TRADE ---")
    pre_usdt = await bn.get_balance("USDT")
    pre_inj = await bn.get_balance(TOKEN)
    inj_price = await bn.get_ticker_price(SYMBOL)
    print(f"  USDT free:      ${pre_usdt:.4f}")
    print(f"  {TOKEN} free:       {pre_inj:.6f}")
    print(f"  {TOKEN} ticker:     ${inj_price:.4f}")

    sym_info = await bn.get_symbol_info(SYMBOL)
    if not sym_info.get("success"):
        print(f"  ✗ symbol_info failed: {sym_info.get('error')}")
        return
    print(f"  symbol status:  {sym_info.get('status')}")
    print(f"  min_notional:   ${sym_info.get('min_notional'):.2f}")
    print(f"  step_size:      {sym_info.get('step_size')}")
    if pre_usdt < CANARY_USDT + 1:
        print(f"  ✗ insufficient USDT (${pre_usdt:.2f} < ${CANARY_USDT + 1:.2f}) — aborting")
        return
    if CANARY_USDT < sym_info.get("min_notional", 0):
        print(f"  ✗ ${CANARY_USDT} below min notional — aborting")
        return

    expected_qty = CANARY_USDT / inj_price if inj_price > 0 else 0
    print(f"  projected qty:  ~{expected_qty:.4f} {TOKEN}")

    # ── BUY ──────────────────────────────────────────────────────────────
    print(f"\n--- BUY ${CANARY_USDT} of {TOKEN} via market order ---")
    buy = await bn.place_market_buy_quote(SYMBOL, CANARY_USDT)
    if not buy.get("success"):
        print(f"  ✗ BUY FAILED: {buy.get('error')}")
        return
    bd = buy["data"]
    buy_order_id = bd.get("orderId")
    buy_qty = float(bd.get("executedQty", 0))
    buy_quote = float(bd.get("cummulativeQuoteQty", 0))
    buy_avg = (buy_quote / buy_qty) if buy_qty > 0 else 0
    print(f"  ✓ FILLED  order_id={buy_order_id}")
    print(f"    qty:        {buy_qty:.6f} {TOKEN}")
    print(f"    cum_quote:  ${buy_quote:.4f}")
    print(f"    avg price:  ${buy_avg:.4f}")
    fees = bd.get("fills", [])
    total_fee = sum(float(f.get("commission", 0)) for f in fees)
    fee_asset = fees[0].get("commissionAsset") if fees else "?"
    print(f"    fees:       {total_fee:.6f} {fee_asset}")

    # Brief pause for balance to settle
    await asyncio.sleep(1)

    # ── SELL ─────────────────────────────────────────────────────────────
    # Refetch the ACTUAL free balance — buy fees may be deducted from the asset
    # (e.g. 27 FLOKI fee on a 138k FLOKI buy → free balance is 138573, not 138600).
    actual_free = await bn.get_balance(TOKEN)
    print(f"\n--- SELL {actual_free:.6f} {TOKEN} (free balance after fees) ---")
    sell_qty = bn.quantize_qty(actual_free, sym_info.get("step_size", 0))
    print(f"  sell qty (quantized): {sell_qty:.6f} {TOKEN}")
    if sell_qty <= 0:
        print(f"  ✗ sell qty zero after step-size quantization — aborting")
        return

    sell = await bn.place_market_sell_base(SYMBOL, sell_qty)
    if not sell.get("success"):
        print(f"  ✗ SELL FAILED: {sell.get('error')}")
        print(f"    NOTE: you now hold {buy_qty:.6f} {TOKEN} on Binance.US.")
        print(f"    Sell manually via the Binance.US web/app, or rerun this script.")
        return
    sd = sell["data"]
    sell_order_id = sd.get("orderId")
    sell_qty_filled = float(sd.get("executedQty", 0))
    sell_quote = float(sd.get("cummulativeQuoteQty", 0))
    sell_avg = (sell_quote / sell_qty_filled) if sell_qty_filled > 0 else 0
    print(f"  ✓ FILLED  order_id={sell_order_id}")
    print(f"    qty:        {sell_qty_filled:.6f} {TOKEN}")
    print(f"    cum_quote:  ${sell_quote:.4f}")
    print(f"    avg price:  ${sell_avg:.4f}")

    # ── POST-TRADE summary ────────────────────────────────────────────────
    await asyncio.sleep(1)
    post_usdt = await bn.get_balance("USDT")
    post_inj = await bn.get_balance(TOKEN)
    net_cost = buy_quote - sell_quote  # what we paid in (USDT spent − USDT received)
    print(f"\n--- POST-TRADE ---")
    print(f"  USDT free:    ${post_usdt:.4f}  (Δ ${post_usdt - pre_usdt:+.4f})")
    print(f"  {TOKEN} free:     {post_inj:.6f}   (Δ {post_inj - pre_inj:+.6f})")
    print(f"  Net round-trip cost: ${net_cost:.4f}")
    print(f"  Spread + fees impact: {(net_cost / CANARY_USDT) * 100:.2f}%")

    if abs(post_inj - pre_inj) < 0.001 and post_usdt > pre_usdt - 0.5:
        print(f"\n  ✓ ✓ ✓  Lane validated. Round-trip clean, no stuck position.")
    else:
        print(f"\n  ⚠️  Residual position detected — investigate before trusting the lane.")

    print("=" * 72)
    await bn.close()


if __name__ == "__main__":
    asyncio.run(main())
