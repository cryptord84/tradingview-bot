"""Two-stage limit-order execution for the Binance.US lane.

Stage 1 rests at the mid (maker — earns the half-spread the market path used
to pay); stage 2 cancels and re-places as a marketable limit at touch +
cross_ticks (bounded taker — fills like a market order but can't chase a
broken book). SELLs may fall back to a true MARKET order for any remainder
because exits must complete; BUYs never do — an unfilled entry is a skipped
signal, not a stranded position.

Returns the same {"success", "data": {orderId, executedQty,
cummulativeQuoteQty, fills}} shape as the market-order client calls, so
trade_engine and position_monitor consume it unchanged.

Measured motivation (2026-06-10 bookTicker spot-check): ZEC 0.66%, DOT 0.53%,
TRX 1.1% round-trip spreads. At $5-17 trade sizes the maker stage turns that
cost into capture on every resting fill, and bounded stage-2 pricing removes
the market-order failure mode on thin books (the KAVA $2.56 market-order cap
that blocked a WF passer).
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get

logger = logging.getLogger("bot.binance.limit")

DEFAULTS = {
    "enabled": False,
    "stage1_timeout_s": 45.0,    # rest at mid before escalating
    "stage2_timeout_s": 20.0,    # marketable limit at touch
    "cross_ticks": 2,            # ticks past touch on stage 2
    "poll_interval_s": 2.0,
    "market_fallback_sells": True,
}

_TERMINAL = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")


def limit_cfg() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((get("binance_us") or {}).get("limit_orders") or {})
    return cfg


def limit_orders_enabled() -> bool:
    return bool(limit_cfg().get("enabled"))


async def _await_fill(bn, symbol: str, order_id: int, timeout_s: float, poll_s: float) -> dict:
    """Poll an order until terminal status or timeout; returns last seen state."""
    waited = 0.0
    last: dict = {}
    while waited < timeout_s:
        await asyncio.sleep(poll_s)
        waited += poll_s
        r = await bn.get_order(symbol, order_id)
        if r.get("success"):
            last = r["data"]
            if last.get("status") in _TERMINAL:
                break
    return last


async def _cancel_and_final(bn, symbol: str, order_id: int) -> dict:
    """Cancel an order, tolerating the filled-while-cancelling race, then
    re-fetch so fills that landed in that race window are counted."""
    await bn.cancel_order(symbol, order_id)
    r = await bn.get_order(symbol, order_id)
    return r["data"] if r.get("success") else {}


async def execute_limit(bn, symbol: str, side: str,
                        base_qty: float | None = None,
                        quote_usd: float | None = None) -> dict:
    """Run the two-stage limit flow. BUY sizes from `quote_usd` (mirrors
    place_market_buy_quote); SELL from `base_qty` (mirrors
    place_market_sell_base)."""
    assert side in ("BUY", "SELL")
    assert (base_qty is not None) != (quote_usd is not None), "exactly one of base_qty/quote_usd"
    cfg = limit_cfg()

    sym = await bn.get_symbol_info(symbol)
    if not sym.get("success"):
        return {"success": False, "error": f"symbol info: {sym.get('error')}"}
    tick = sym.get("tick_size", 0) or 0
    step = sym.get("step_size", 0) or 0
    min_qty = sym.get("min_qty", 0) or 0
    min_notional = sym.get("min_notional", 0) or 0

    total_qty = 0.0
    total_quote = 0.0
    order_ids: list = []
    maker_qty = 0.0

    def _remaining(price: float) -> float:
        if base_qty is not None:
            return bn.quantize_qty(base_qty - total_qty, step)
        return bn.quantize_qty(max(quote_usd - total_quote, 0.0) / price, step)

    for stage in ("mid", "cross"):
        book = await bn.get_book_ticker(symbol)
        if not book.get("success") or book["bid"] <= 0 or book["ask"] < book["bid"]:
            logger.warning(f"{symbol}: book unusable at stage {stage}: {book.get('error', book)}")
            break
        bid, ask = book["bid"], book["ask"]
        if stage == "mid":
            # Round toward our own side so the quantized mid never crosses.
            price = bn.quantize_price((bid + ask) / 2.0, tick, up=(side == "SELL"))
            timeout = float(cfg["stage1_timeout_s"])
        else:
            if side == "BUY":
                price = bn.quantize_price(ask, tick, up=True) + cfg["cross_ticks"] * tick
            else:
                price = max(bn.quantize_price(bid, tick) - cfg["cross_ticks"] * tick, tick)
            timeout = float(cfg["stage2_timeout_s"])
        price = round(price, 8)

        qty = _remaining(price)
        if qty < max(min_qty, step) or qty * price < min_notional:
            break  # remainder too small to place — keep what we have

        r = await bn.place_limit_order(symbol, side, qty, price)
        if not r.get("success"):
            logger.warning(f"{symbol} {side} LIMIT ({stage} @ {price}) rejected: {r.get('error')}")
            break
        od = r["data"]
        oid = od.get("orderId")
        order_ids.append(oid)
        logger.info(f"{symbol} {side} LIMIT {stage}: {qty} @ {price} (order {oid})")

        od = await _await_fill(bn, symbol, oid, timeout, float(cfg["poll_interval_s"]))
        if od.get("status") != "FILLED":
            od = await _cancel_and_final(bn, symbol, oid) or od

        got_qty = float(od.get("executedQty", 0) or 0)
        got_quote = float(od.get("cummulativeQuoteQty", 0) or 0)
        total_qty += got_qty
        total_quote += got_quote
        if stage == "mid":
            maker_qty += got_qty
        if od.get("status") == "FILLED":
            break

    # Exits must complete: market-out any meaningful SELL remainder.
    if side == "SELL" and cfg["market_fallback_sells"] and base_qty is not None:
        rem = bn.quantize_qty(base_qty - total_qty, step)
        book = await bn.get_book_ticker(symbol)
        ref_px = book.get("bid", 0) if book.get("success") else 0
        if rem >= max(min_qty, step) and rem * ref_px >= min_notional:
            logger.warning(f"{symbol} SELL remainder {rem} → MARKET fallback")
            r = await bn.place_market_sell_base(symbol, rem)
            if r.get("success"):
                od = r["data"]
                total_qty += float(od.get("executedQty", 0) or 0)
                total_quote += float(od.get("cummulativeQuoteQty", 0) or 0)
                order_ids.append(od.get("orderId"))
            else:
                logger.error(f"{symbol} SELL market fallback failed: {r.get('error')}")

    if total_qty <= 0:
        return {"success": False,
                "error": f"limit {side} unfilled after both stages (book moved away)"}

    maker_pct = (maker_qty / total_qty * 100) if total_qty > 0 else 0
    logger.info(
        f"{symbol} {side} limit flow done: qty={total_qty:.8f} "
        f"quote=${total_quote:.2f} maker={maker_pct:.0f}% orders={len(order_ids)}"
    )
    return {
        "success": True,
        "data": {
            "orderId": order_ids[-1] if order_ids else None,
            "executedQty": f"{total_qty:.8f}",
            "cummulativeQuoteQty": f"{total_quote:.8f}",
            "fills": [],
            "limit_orders_used": len(order_ids),
            "maker_fill_pct": round(maker_pct, 1),
        },
    }
