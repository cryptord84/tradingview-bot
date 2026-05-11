"""Chain-aware price routing for TP/SL monitoring.

Per CLAUDE.md hard rule #8: monitor price source must match swap execution
source. This module routes price lookups by chain so position_monitor never
fake-fires SLs on disconnected zombie listings (the 2026-05-06 incident:
JUPUSDT on Binance.US showed $0.10 vs Solana spot $0.19 → 4 phantom SLs).

Lane priority chains (each falls through on failure):
    Solana SPL: Jupiter aggregator (matches venue) → price_feed WS → CoinGecko
    EVM (Arb):  OpenOcean quote (matches venue)    → price_feed WS → CoinGecko
    Binance.US: price_feed WS (matches venue)      → Binance.US REST → CoinGecko

Routing decision is by execution lane, not by token name. We use TradeEngine's
EVM_TOKENS / BINANCE_TOKENS dicts as the source of truth for what trades where.
Anything not in those dicts defaults to the Solana lane.

CoinGecko is last-resort only — it's an aggregated price, lagged ~1-5 min.
Used when both the venue API and the cross-check WS feed are unreachable.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("bot.price_router")

# Hardcoded CoinGecko IDs for tokens not in JupiterClient.COINGECKO_IDS
# (mostly EVM-native tokens that don't have Solana SPL wrappers tracked).
_COINGECKO_FALLBACK_IDS = {
    "BTC":   "bitcoin",
    "DOGE":  "dogecoin",
    "OP":    "optimism",
    "UNI":   "uniswap",
    "AAVE":  "aave",
    "ARB":   "arbitrum",
    "LDO":   "lido-dao",
    "COMP":  "compound-governance-token",
    "LINK":  "chainlink",
    "FLOKI": "floki",
    "MKR":   "maker",
    "AVAX":  "avalanche-2",
    "ATOM":  "cosmos",
    "NEAR":  "near",
    "POL":   "polygon-ecosystem-token",
    "SHIB":  "shiba-inu",
    "PEPE":  "pepe",
    "TIA":   "celestia",
    "KAVA":  "kava",
    "XRP":   "ripple",
    "SUI":   "sui",
    "ADA":   "cardano",
    "HYPE":  "hyperliquid",
    "ONDO":  "ondo-finance",
    "ASTER": "aster-2",  # may need verification
}


async def get_monitor_price(token_symbol: str) -> Optional[float]:
    """Resolve current price for TP/SL monitoring, routed by execution lane.

    Returns the price in USDC. Returns None when ALL sources for the lane
    fail — caller (position_monitor) should skip the position check rather
    than risk acting on stale data.
    """
    from app.services.trade_engine import TradeEngine

    sym = (token_symbol or "").upper()
    if not sym:
        return None

    # EVM lane takes precedence over Binance (matches trade_engine routing)
    if sym in TradeEngine.EVM_TOKENS:
        return await _evm_price(sym)
    if sym in TradeEngine.BINANCE_TOKENS:
        return await _binance_price(sym)
    return await _solana_price(sym)


# ── per-lane chains ─────────────────────────────────────────────────────────

async def _solana_price(sym: str) -> Optional[float]:
    """Solana lane: Jupiter (venue) → price_feed WS → CoinGecko."""
    # 1. Jupiter aggregator — matches execution venue exactly
    try:
        from app.services.jupiter_client import JupiterClient
        j = JupiterClient()
        try:
            if sym == "SOL":
                price = await j.get_sol_price()
            else:
                price = await j.get_token_price(sym)
            if price and price > 0:
                return float(price)
        finally:
            await j.close()
    except ValueError:
        # JupiterClient raises ValueError("Unknown token: X") for symbols
        # without a registered Solana mint. That's expected for non-SPL tokens
        # (BTC, OP, DOGE, etc.) — fall through to next source.
        pass
    except Exception as e:
        logger.debug(f"Solana primary (Jupiter) failed for {sym}: {e}")

    # 2. price_feed WS as cross-check (only when Jupiter unavailable)
    try:
        from app.services.price_feed import get_price_feed
        feed = get_price_feed()
        if feed.is_running:
            pd = feed.get_price(sym)
            if pd and pd.price > 0:
                logger.info(f"Solana price fallback (price_feed WS) for {sym}: ${pd.price}")
                return pd.price
    except Exception as e:
        logger.debug(f"Solana secondary (price_feed) failed for {sym}: {e}")

    # 3. CoinGecko last resort
    return await _coingecko_price(sym)


async def _evm_price(sym: str) -> Optional[float]:
    """EVM lane: OpenOcean quote (venue) → price_feed WS → CoinGecko."""
    # 1. OpenOcean quote on Arbitrum — matches execution venue exactly
    try:
        from app.services.evm_wallet_service import ARBITRUM_TOKENS
        from app.services.openocean_client import OpenOceanClient

        token_addr = ARBITRUM_TOKENS.get(sym)
        usdc_addr = ARBITRUM_TOKENS.get("USDC")
        if token_addr and usdc_addr:
            client = OpenOceanClient()
            try:
                quote = await client.get_quote(
                    src_token=token_addr,
                    dst_token=usdc_addr,
                    amount_human="1",
                    slippage_pct=1.0,
                )
                # outAmount is in USDC atomic units (Arbitrum native USDC = 6 decimals)
                out_amount = int(quote.get("outAmount", 0) or 0)
                if out_amount > 0:
                    return out_amount / 1e6
            finally:
                await client.close()
    except Exception as e:
        logger.debug(f"EVM primary (OpenOcean) failed for {sym}: {e}")

    # 2. price_feed WS as cross-check
    try:
        from app.services.price_feed import get_price_feed
        feed = get_price_feed()
        if feed.is_running:
            pd = feed.get_price(sym)
            if pd and pd.price > 0:
                logger.info(f"EVM price fallback (price_feed WS) for {sym}: ${pd.price}")
                return pd.price
    except Exception as e:
        logger.debug(f"EVM secondary (price_feed) failed for {sym}: {e}")

    return await _coingecko_price(sym)


async def _binance_price(sym: str) -> Optional[float]:
    """Binance.US lane: price_feed WS (venue) → Binance.US REST → CoinGecko.

    The WS feed IS the execution venue's stream — perfect primary. REST ticker
    is the same venue if WS disconnects (slower but accurate).
    """
    # 1. price_feed WS (venue stream)
    try:
        from app.services.price_feed import get_price_feed
        feed = get_price_feed()
        if feed.is_running:
            pd = feed.get_price(sym)
            if pd and pd.price > 0:
                return pd.price
    except Exception as e:
        logger.debug(f"Binance primary (price_feed WS) failed for {sym}: {e}")

    # 2. Binance.US REST ticker (same venue, polling)
    try:
        from app.services.trade_engine import TradeEngine
        from app.services.binance_us_client import BinanceUSClient

        bn = BinanceUSClient()
        if bn.enabled:
            try:
                pair = TradeEngine.BINANCE_TOKENS.get(sym, f"{sym}USDT")
                price = await bn.get_ticker_price(pair)
                if price and price > 0:
                    logger.info(f"Binance price fallback (REST) for {sym}: ${price}")
                    return float(price)
            finally:
                await bn.close()
    except Exception as e:
        logger.debug(f"Binance secondary (REST) failed for {sym}: {e}")

    return await _coingecko_price(sym)


# ── CoinGecko last-resort ───────────────────────────────────────────────────

async def _coingecko_price(sym: str) -> Optional[float]:
    """Aggregated price as last resort. Lagged ~1-5 min — use only when all
    venue/cross-check sources have failed."""
    cg_id = None

    # Try JupiterClient.COINGECKO_IDS (covers most Solana tokens)
    try:
        from app.services.jupiter_client import JupiterClient
        cg_id = JupiterClient.COINGECKO_IDS.get(sym)
    except Exception:
        pass

    # Try price_feed.COINGECKO_ONLY (smaller set)
    if not cg_id:
        try:
            from app.services.price_feed import COINGECKO_ONLY
            cg_id = COINGECKO_ONLY.get(sym)
        except Exception:
            pass

    # Hardcoded fallbacks for EVM-native tokens
    if not cg_id:
        cg_id = _COINGECKO_FALLBACK_IDS.get(sym)

    if not cg_id:
        logger.warning(f"No CoinGecko ID known for {sym} — all price sources exhausted")
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
            )
            r.raise_for_status()
            d = r.json()
            price = (d.get(cg_id) or {}).get("usd")
            if price and float(price) > 0:
                logger.warning(
                    f"Using CoinGecko last-resort price for {sym}: ${price} "
                    f"(venue + WS feeds both failed)"
                )
                return float(price)
    except Exception as e:
        logger.debug(f"CoinGecko fallback failed for {sym}: {e}")

    return None
