"""OpenOcean v3 API client — quote + swap transaction building.

Replacement for paraswap_client.py. Pivoted 2026-05-02 after Paraswap's
deployed v5 contracts (Augustus 0xDEF1...) repeatedly failed live swaps
with "External call failed" reverts on Arbitrum (likely deprecated routes).

OpenOcean v3 has a much simpler API:
- Single-call /swap_quote returns both the quote AND the unsigned tx
- No priceRoute object to pass between calls (no staleness issues)
- Public API, no key, no KYC
- Single ERC20 approval target per chain (Exchange contract)

Routes through PancakeSwap V3, Uniswap V3, Sushiswap, Curve, Balancer,
and others. AMM-only by default — no CLOB venues that have stale-quote issues.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("bot.openocean")

OPENOCEAN_API_BASE = "https://open-api.openocean.finance/v3"

# Native ETH "address" for native gas-token swaps.
NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Chain slug used by OpenOcean's URL path
CHAIN_SLUGS = {
    1:     "eth",
    42161: "arbitrum",
    8453:  "base",
    10:    "optimism",
    137:   "polygon",
    43114: "avax",
    56:    "bsc",
}

# OpenOcean Exchange contract addresses by chain (the ERC20 approval target).
# Verified against OpenOcean docs as of 2026-05-02.
EXCHANGE_CONTRACTS = {
    1:     "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    42161: "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    8453:  "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    10:    "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    137:   "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    43114: "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
    56:    "0x6352a56caadC4F1E25CD6c75970Fa768A3304E64",
}


class OpenOceanError(Exception):
    """Raised when OpenOcean returns an error or non-200 status."""
    pass


class OpenOceanClient:
    """OpenOcean v3 public API client. Async; reuse across calls."""

    def __init__(self, chain_id: int = 42161):
        if chain_id not in CHAIN_SLUGS:
            raise ValueError(
                f"chain_id {chain_id} not in CHAIN_SLUGS — add slug + Exchange "
                f"contract for it"
            )
        self.chain_id = chain_id
        self.chain_slug = CHAIN_SLUGS[chain_id]
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={"Accept": "application/json"},
        )

    @property
    def base(self) -> str:
        return f"{OPENOCEAN_API_BASE}/{self.chain_slug}"

    @property
    def approval_target(self) -> str:
        """The Exchange contract — ERC20 approvals must target this."""
        return EXCHANGE_CONTRACTS[self.chain_id]

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self._client.get(f"{self.base}{path}", params=params)
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text[:200]}
            raise OpenOceanError(f"GET {path} HTTP {resp.status_code}: {err}")
        body = resp.json()
        if body.get("code") and body.get("code") != 200:
            raise OpenOceanError(f"GET {path} API code {body.get('code')}: {body.get('error', body)}")
        return body

    async def get_quote(
        self,
        src_token: str,
        dst_token: str,
        amount_human: str,
        gas_price_gwei: float = 0.05,
        slippage_pct: float = 1.0,
    ) -> dict:
        """Quote-only (no tx data). Use for inspection; for trading use get_swap_quote.

        Args:
            amount_human: input amount as human-readable string (e.g. "3" for 3 USDC)
            gas_price_gwei: hint for gas-aware routing
            slippage_pct: 1 = 1%, supports decimals (e.g. 0.5 = 0.5%)
        """
        body = await self._get("/quote", {
            "inTokenAddress":  src_token,
            "outTokenAddress": dst_token,
            "amount":          amount_human,
            "gasPrice":        str(gas_price_gwei),
            "slippage":        str(slippage_pct),
        })
        return body["data"]

    async def get_swap_quote(
        self,
        src_token: str,
        dst_token: str,
        amount_human: str,
        from_address: str,
        gas_price_gwei: float = 0.05,
        slippage_pct: float = 1.0,
    ) -> dict:
        """Quote + unsigned swap transaction in a single call.

        Returns: {
            inAmount, outAmount, ...,
            data, to, value, estimatedGas, gasPrice (in wei or gwei depending on chain)
        }
        Sign + broadcast via EVMWalletService.
        """
        body = await self._get("/swap_quote", {
            "inTokenAddress":  src_token,
            "outTokenAddress": dst_token,
            "amount":          amount_human,
            "account":         from_address,
            "gasPrice":        str(gas_price_gwei),
            "slippage":        str(slippage_pct),
        })
        return body["data"]

    async def close(self) -> None:
        await self._client.aclose()
