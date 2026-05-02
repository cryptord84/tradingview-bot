"""Jupiter v6 API client for Solana DEX trading."""

import base64
import json
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

    def _is_slippage_error(self, error: dict) -> bool:
        """Check if an RPC error is a Jupiter slippage tolerance exceeded (0x1771 / 6001)."""
        err_data = error.get("data", {})
        if isinstance(err_data, dict):
            instr_err = err_data.get("err", {})
            if isinstance(instr_err, dict):
                ie = instr_err.get("InstructionError", [])
                if len(ie) == 2 and isinstance(ie[1], dict):
                    custom = ie[1].get("Custom", 0)
                    return custom == 6001  # SlippageToleranceExceeded
        return False

    async def _send_swap(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int,
    ) -> dict:
        """Internal: quote → build tx → sign → send. Returns (result_dict, quote)."""
        quote = await self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
        pub_key = str(keypair.pubkey())
        swap_tx_b64 = await self.get_swap_transaction(quote, pub_key)

        raw_tx = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])

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
        return result, quote

    async def execute_swap(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: Optional[int] = None,
    ) -> dict:
        """Full swap flow: quote -> transaction -> sign -> send.

        Retries once with 2x slippage on SlippageToleranceExceeded (0x1771),
        capped at 300 bps (3%).
        """
        initial_slippage = slippage_bps or self.slippage_bps
        max_slippage = get("jupiter", "max_slippage_bps", 300)

        result, quote = await self._send_swap(
            keypair, input_mint, output_mint, amount_lamports, initial_slippage,
        )

        if "error" in result and self._is_slippage_error(result["error"]):
            retry_slippage = min(initial_slippage * 2, max_slippage)
            if retry_slippage > initial_slippage:
                logger.warning(
                    f"Slippage exceeded at {initial_slippage} bps, retrying with {retry_slippage} bps"
                )
                result, quote = await self._send_swap(
                    keypair, input_mint, output_mint, amount_lamports, retry_slippage,
                )

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

    # Binance trading pair symbols for tracked tokens
    # Only tokens actually listed on Binance.us (RAY, PYTH not available)
    # Must stay aligned with app/services/price_feed.py (BINANCE_TOKENS + COINGECKO_ONLY).
    BINANCE_SYMBOLS = {
        "SOL": "SOLUSDT",
        "JTO": "JTOUSDT",
        "BONK": "BONKUSDT",
        "ETH": "ETHUSDT",
        "ORCA": "ORCAUSDT",
        "JUP": "JUPUSDT",
        "PENGU": "PENGUUSDT",
        "FARTCOIN": "FARTCOINUSDT",
        "POPCAT": "POPCATUSDT",
        "MEW": "MEWUSDT",
        "PNUT": "PNUTUSDT",
        "MOODENG": "MOODENGUSDT",
        # Tier 3 additions (2026-05-02)
        "ME":   "MEUSDT",       # Binance.US
        "KMNO": "KMNO-USD",     # Coinbase
        "DBR":  "DBR-USD",      # Coinbase
        "ACT":  "ACT-USDT",     # OKX
        "GOAT": "GOAT-USDT",    # OKX
        "ZEUS": "ZEUS-USDT",    # OKX
    }

    # CoinGecko IDs as fallback (also covers tokens not on Binance.us)
    COINGECKO_IDS = {
        "SOL": "solana",
        "JTO": "jito-governance-token",
        "BONK": "bonk",
        "ETH": "ethereum-wormhole",
        "ORCA": "orca",
        "JUP": "jupiter-exchange-solana",
        "PENGU": "pudgy-penguins",
        "FARTCOIN": "fartcoin",
        "POPCAT": "popcat",
        "MEW": "cat-in-a-dogs-world",
        "PNUT": "peanut-the-squirrel",
        "MOODENG": "moo-deng",
        "PYTH": "pyth-network",
        "RAY": "raydium",
        "WIF": "dogwifcoin",
        "RENDER": "render-token",
        "W": "wormhole",
        "DOG": "dog-go-to-the-moon-rune",
        # Tier 3 additions (2026-05-02)
        "ME":   "magic-eden",
        "KMNO": "kamino",
        "DBR":  "debridge",
        "ACT":  "act-i-the-prophecy",
        "GOAT": "goatseus-maximus",
        "ZEUS": "zeus-network",
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

    async def get_token_price(self, symbol: str) -> float:
        """Get price for any tracked token. Binance first, CoinGecko fallback."""
        symbol = symbol.upper()
        # Try Binance
        binance_pair = self.BINANCE_SYMBOLS.get(symbol)
        if binance_pair:
            try:
                resp = await self._client.get(
                    "https://api.binance.us/api/v3/ticker/price",
                    params={"symbol": binance_pair},
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
            except Exception:
                pass
        # Fallback to CoinGecko
        cg_id = self.COINGECKO_IDS.get(symbol)
        if cg_id:
            resp = await self._client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            return float(resp.json()[cg_id]["usd"])
        raise ValueError(f"Unknown token: {symbol}")

    async def get_multi_token_prices(self) -> dict:
        """Get prices + 24h change for all tracked tokens. Returns dict of symbol -> {price, change_24h}."""
        result = {}
        # Try Binance 24h ticker for all tokens at once
        try:
            symbols = list(self.BINANCE_SYMBOLS.values())
            resp = await self._client.get(
                "https://api.binance.us/api/v3/ticker/24hr",
                params={"symbols": json.dumps(symbols, separators=(",", ":"))},
            )
            resp.raise_for_status()
            data = resp.json()
            binance_to_token = {v: k for k, v in self.BINANCE_SYMBOLS.items()}
            for ticker in data:
                sym = binance_to_token.get(ticker["symbol"])
                if sym:
                    result[sym] = {
                        "price": float(ticker["lastPrice"]),
                        "change_24h": float(ticker["priceChangePercent"]),
                        "high_24h": float(ticker["highPrice"]),
                        "low_24h": float(ticker["lowPrice"]),
                        "volume_24h": float(ticker["quoteVolume"]),
                    }
        except Exception as e:
            logger.warning(f"Binance multi-price failed: {e}")

        # Fill missing tokens from CoinGecko
        missing = [s for s in self.COINGECKO_IDS if s not in result]
        if missing:
            try:
                cg_ids = ",".join(self.COINGECKO_IDS[s] for s in missing if s in self.COINGECKO_IDS)
                resp = await self._client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": cg_ids, "vs_currencies": "usd", "include_24hr_change": "true"},
                )
                resp.raise_for_status()
                cg_data = resp.json()
                cg_id_to_token = {v: k for k, v in self.COINGECKO_IDS.items()}
                for cg_id, vals in cg_data.items():
                    sym = cg_id_to_token.get(cg_id)
                    if sym:
                        result[sym] = {
                            "price": vals.get("usd", 0),
                            "change_24h": vals.get("usd_24h_change", 0),
                            "high_24h": None,
                            "low_24h": None,
                            "volume_24h": None,
                        }
            except Exception as e:
                logger.warning(f"CoinGecko fallback failed: {e}")

        return result

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
