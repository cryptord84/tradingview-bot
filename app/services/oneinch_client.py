"""1inch v6 API client — quote, swap, and ERC20 approval transaction building.

Mirrors the structure of `jupiter_client.py` (Solana). Phase 2 of the EVM
integration.

The 1inch API itself is HTTP-only — it returns transaction *data* that the
caller signs and broadcasts via their own RPC. This client returns those
unsigned transaction dicts; signing and broadcasting happens in
`evm_wallet_service.py`.

Endpoints (v6.0):
- /quote                     — rate quote (no transaction)
- /swap                      — unsigned swap transaction
- /approve/spender           — current 1inch router address for the chain
- /approve/allowance         — wallet's current ERC20 allowance to the router
- /approve/transaction       — unsigned ERC20 approve() transaction

API key is required on all endpoints (Bearer token). Free tier: 1 RPS,
~100k requests/month — ample for a trading bot at our cadence.
"""

import logging
from typing import Optional

import httpx

from app.config import get, get_env

logger = logging.getLogger("bot.oneinch")

# 1inch API base — v6.0 supports Arbitrum (42161), Base (8453), Ethereum (1),
# Optimism (10), Polygon (137), and many others.
ONEINCH_API_BASE = "https://api.1inch.dev/swap/v6.0"

# Native ETH "address" used by 1inch for native gas token swaps (this is a
# convention — there is no real ERC20 contract at this address).
NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


class OneInchError(Exception):
    """Raised when 1inch returns a structured error."""
    pass


class OneInchClient:
    """1inch v6 swap API client. Async; reuse a single instance across calls."""

    def __init__(self, chain_id: Optional[int] = None):
        cfg = get("oneinch") or {}
        self.api_key = get_env("ONEINCH_API_KEY") or cfg.get("api_key", "")
        if not self.api_key:
            raise ValueError(
                "No 1inch API key. Set ONEINCH_API_KEY env var, or "
                "add `oneinch.api_key` to config.yaml. Get a free key at "
                "https://portal.1inch.dev/"
            )

        # Default to evm_wallet's chain_id; allow override for multi-chain support.
        if chain_id is None:
            chain_id = int((get("evm_wallet") or {}).get("chain_id", 42161))
        self.chain_id = chain_id

        self.referrer_addr: Optional[str] = cfg.get("referrer_address")  # optional
        self.fee_pct: float = float(cfg.get("fee_pct", 0))               # 0 = no fee

        self._client = httpx.AsyncClient(
            timeout=20,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        self._cached_router: Optional[str] = None

    @property
    def base(self) -> str:
        return f"{ONEINCH_API_BASE}/{self.chain_id}"

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET helper with consistent error handling."""
        resp = await self._client.get(f"{self.base}{path}", params=params or {})
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text[:200]}
            raise OneInchError(f"{path} HTTP {resp.status_code}: {err}")
        return resp.json()

    # ── Router discovery ─────────────────────────────────────────────────────

    async def get_router_address(self) -> str:
        """Get the 1inch router address for this chain (the spender ERC20s
        must approve before swaps). Cached for the lifetime of this client.
        """
        if self._cached_router is None:
            data = await self._get("/approve/spender")
            self._cached_router = data["address"]
        return self._cached_router

    # ── Quote ────────────────────────────────────────────────────────────────

    async def get_quote(
        self,
        src: str,
        dst: str,
        amount_wei: int,
        include_gas: bool = True,
    ) -> dict:
        """Get a rate quote (no transaction).

        Args:
            src: input token contract (use NATIVE_TOKEN_ADDRESS for native ETH)
            dst: output token contract
            amount_wei: input amount in token's smallest unit
            include_gas: include gas estimate in response

        Returns: {dstAmount, gas, srcToken, dstToken, ...}
        """
        params = {
            "src": src,
            "dst": dst,
            "amount": str(amount_wei),
            "includeGas": str(include_gas).lower(),
        }
        return await self._get("/quote", params)

    # ── Swap (unsigned tx) ───────────────────────────────────────────────────

    async def get_swap_tx(
        self,
        src: str,
        dst: str,
        amount_wei: int,
        from_addr: str,
        slippage_pct: float = 1.0,
        disable_estimate: bool = False,
    ) -> dict:
        """Get an unsigned swap transaction ready to sign and broadcast.

        Returns: {dstAmount, tx: {from, to, data, value, gas, gasPrice}}
        """
        params = {
            "src": src,
            "dst": dst,
            "amount": str(amount_wei),
            "from": from_addr,
            "slippage": str(slippage_pct),
            "disableEstimate": str(disable_estimate).lower(),
        }
        if self.referrer_addr and self.fee_pct > 0:
            params["referrer"] = self.referrer_addr
            params["fee"] = str(self.fee_pct)
        return await self._get("/swap", params)

    # ── ERC20 approval flow ──────────────────────────────────────────────────

    async def get_allowance(self, token_addr: str, wallet_addr: str) -> int:
        """Read current ERC20 allowance from wallet to the 1inch router."""
        data = await self._get("/approve/allowance", {
            "tokenAddress": token_addr,
            "walletAddress": wallet_addr,
        })
        return int(data["allowance"])

    async def get_approve_tx(self, token_addr: str, amount_wei: Optional[int] = None) -> dict:
        """Get an unsigned ERC20 approve() transaction.

        Args:
            token_addr: ERC20 contract to approve
            amount_wei: amount to approve, or None for unlimited (NOT recommended)

        Returns: {to, data, value, gasPrice}
        """
        params = {"tokenAddress": token_addr}
        if amount_wei is not None:
            params["amount"] = str(amount_wei)
        return await self._get("/approve/transaction", params)

    async def close(self) -> None:
        await self._client.aclose()
