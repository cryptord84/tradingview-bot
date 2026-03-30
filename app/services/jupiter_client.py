"""Jupiter v6 API client for Solana DEX trading."""

import base64
import logging
from typing import Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from app.config import get
from app.database import log_wallet_tx

logger = logging.getLogger("bot.jupiter")


class JupiterClient:
    """Client for Jupiter v6 swap API."""

    def __init__(self):
        cfg = get("jupiter")
        self.api_base = cfg.get("api_base", "https://lite-api.jup.ag/swap/v1")
        self.sol_mint = cfg.get("sol_mint", "So11111111111111111111111111111111111111112")
        self.usdc_mint = cfg.get("usdc_mint", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        self.slippage_bps = cfg.get("slippage_bps", 100)
        self.priority_fee = cfg.get("priority_fee_lamports", 50000)
        self._client = httpx.AsyncClient(timeout=30)

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: Optional[int] = None,
    ) -> dict:
        """Get swap quote from Jupiter."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps or self.slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        resp = await self._client.get(f"{self.api_base}/quote", params=params)
        resp.raise_for_status()
        quote = resp.json()
        logger.info(
            f"Quote: {amount} {input_mint[:8]}.. -> {quote.get('outAmount', '?')} {output_mint[:8]}.."
        )
        return quote

    async def get_swap_transaction(
        self,
        quote: dict,
        user_public_key: str,
        wrap_unwrap_sol: bool = True,
    ) -> str:
        """Get serialized swap transaction from Jupiter."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_unwrap_sol,
            "prioritizationFeeLamports": self.priority_fee,
            "dynamicComputeUnitLimit": True,
        }
        resp = await self._client.post(f"{self.api_base}/swap", json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["swapTransaction"]

    async def execute_swap(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: Optional[int] = None,
    ) -> dict:
        """Full swap flow: quote -> transaction -> sign -> send."""
        # 1. Get quote
        quote = await self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)

        # 2. Get swap transaction
        pub_key = str(keypair.pubkey())
        swap_tx_b64 = await self.get_swap_transaction(quote, pub_key)

        # 3. Deserialize and sign
        raw_tx = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])

        # 4. Send to RPC
        rpc_url = get("wallet", "rpc_url", "https://api.mainnet-beta.solana.com")
        signed_bytes = bytes(signed_tx)
        tx_b64 = base64.b64encode(signed_bytes).decode()

        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_b64,
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 3},
            ],
        }
        resp = await self._client.post(rpc_url, json=rpc_body)
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            logger.error(f"RPC error: {result['error']}")
            raise RuntimeError(f"Transaction failed: {result['error']}")

        tx_sig = result.get("result", "")
        logger.info(f"Transaction sent: {tx_sig}")

        # Determine swap direction and amounts for logging
        out_amount = int(quote.get("outAmount", 0))
        swap_fee_sol = (5000 + self.priority_fee) / 1_000_000_000  # base + priority fee
        if input_mint == self.usdc_mint:
            log_wallet_tx(
                tx_type="swap", direction="out", amount=amount_lamports / 1_000_000,
                token="USDC", fee_sol=swap_fee_sol, tx_signature=tx_sig,
                notes=f"Buy SOL (got {out_amount / 1e9:.4f} SOL)",
            )
        else:
            log_wallet_tx(
                tx_type="swap", direction="out", amount=amount_lamports / 1_000_000_000,
                token="SOL", fee_sol=swap_fee_sol, tx_signature=tx_sig,
                notes=f"Sell SOL (got {out_amount / 1e6:.2f} USDC)",
            )

        return {
            "tx_signature": tx_sig,
            "input_amount": amount_lamports,
            "output_amount": out_amount,
            "price_impact": quote.get("priceImpactPct", "0"),
            "route_plan": quote.get("routePlan", []),
        }

    async def get_sol_price(self) -> float:
        """Get SOL/USD price. Tries Binance first (no key, no rate limit), then CoinGecko."""
        try:
            return await self._get_sol_price_binance()
        except Exception as e:
            logger.warning(f"Binance price failed, trying CoinGecko: {e}")
            return await self._get_sol_price_coingecko()

    async def _get_sol_price_binance(self) -> float:
        """Get SOL price from Binance US public API (no key required)."""
        resp = await self._client.get(
            "https://api.binance.us/api/v3/ticker/price",
            params={"symbol": "SOLUSDT"},
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    async def _get_sol_price_coingecko(self) -> float:
        """Get SOL price from CoinGecko (rate-limited on free tier)."""
        resp = await self._client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        return float(resp.json()["solana"]["usd"])

    async def get_market_data(self) -> dict:
        """Get SOL market data from CoinGecko."""
        try:
            resp = await self._client.get(
                "https://api.coingecko.com/api/v3/coins/solana",
                params={"localization": "false", "tickers": "false", "community_data": "false"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "price_change_24h": data["market_data"]["price_change_percentage_24h"],
                "volume_24h": data["market_data"]["total_volume"]["usd"],
                "market_cap_rank": data["market_cap_rank"],
                "high_24h": data["market_data"]["high_24h"]["usd"],
                "low_24h": data["market_data"]["low_24h"]["usd"],
            }
        except Exception as e:
            logger.warning(f"Failed to get market data: {e}")
            return {}

    async def close(self):
        await self._client.aclose()
