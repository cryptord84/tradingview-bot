"""Paraswap v6 API client — quote, swap, and ERC20 approval transaction building.

Replaced the original 1inch client because 1inch's developer portal requires
KYC verification (added 2026). Paraswap's public API is fully open: no key,
no registration, no KYC.

Like 1inch, Paraswap is a multi-DEX aggregator: it routes across Uniswap V3,
Sushiswap, Curve, Balancer, Camelot, etc. on each chain to find best execution.
On Arbitrum specifically, it includes Dexalot (CLOB) which often beats AMMs
for stable pairs.

Two-call flow (different from 1inch's single-call /swap):
  1. GET /prices              → returns priceRoute object with the route
  2. POST /transactions/{net} → submit priceRoute + user address → unsigned tx

This client returns the raw unsigned tx dict; signing + broadcasting happens
in `evm_wallet_service.py`.

ERC20 approval target: TokenTransferProxy (NOT the Augustus swapper itself).
This is a Paraswap-specific quirk — Augustus pulls funds via the proxy,
so users approve the proxy.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("bot.paraswap")

PARASWAP_API_BASE = "https://api.paraswap.io"

# Native ETH "address" used by Paraswap for native token swaps.
NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Per-chain contract addresses (verified 2026-05-02).
# AugustusSwapper = the contract that executes swaps.
# TokenTransferProxy = the contract ERC20 approvals must target.
CHAIN_CONTRACTS = {
    1: {  # Ethereum
        "augustus":             "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57",
        "token_transfer_proxy": "0x216B4B4Ba9F3e719726886d34a177484278Bfcae",
    },
    42161: {  # Arbitrum One
        "augustus":             "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57",
        "token_transfer_proxy": "0x216B4B4Ba9F3e719726886d34a177484278Bfcae",
    },
    8453: {  # Base
        "augustus":             "0x59C7C832e96D2568bea6db468C1aAdcbbDa08A52",
        "token_transfer_proxy": "0x93aAAe79a53759cD164340E4C8766E4Db5331cD7",
    },
    10: {  # Optimism
        "augustus":             "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57",
        "token_transfer_proxy": "0x216B4B4Ba9F3e719726886d34a177484278Bfcae",
    },
    137: {  # Polygon
        "augustus":             "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57",
        "token_transfer_proxy": "0x216B4B4Ba9F3e719726886d34a177484278Bfcae",
    },
}

# ERC20 allowance(owner, spender) selector
ERC20_ALLOWANCE_SELECTOR = "0xdd62ed3e"
# ERC20 approve(spender, amount) selector
ERC20_APPROVE_SELECTOR = "0x095ea7b3"


class ParaswapError(Exception):
    """Raised when Paraswap returns a structured error."""
    pass


class ParaswapClient:
    """Paraswap public API client. Async; reuse across calls.

    Construct with chain_id matching your EVMWalletService config (default
    Arbitrum 42161).
    """

    def __init__(self, chain_id: int = 42161):
        if chain_id not in CHAIN_CONTRACTS:
            raise ValueError(
                f"chain_id {chain_id} not in CHAIN_CONTRACTS — add the "
                f"AugustusSwapper + TokenTransferProxy addresses for it"
            )
        self.chain_id = chain_id
        self.contracts = CHAIN_CONTRACTS[chain_id]
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={"Accept": "application/json"},
        )

    # ── Core HTTP helper ─────────────────────────────────────────────────────

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = await self._client.get(f"{PARASWAP_API_BASE}{path}", params=params or {})
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text[:200]}
            raise ParaswapError(f"GET {path} HTTP {resp.status_code}: {err}")
        return resp.json()

    async def _post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        resp = await self._client.post(
            f"{PARASWAP_API_BASE}{path}", json=body, params=params or {},
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text[:200]}
            raise ParaswapError(f"POST {path} HTTP {resp.status_code}: {err}")
        return resp.json()

    # ── Approval target ──────────────────────────────────────────────────────

    @property
    def approval_target(self) -> str:
        """The contract that ERC20 approvals must be made to."""
        return self.contracts["token_transfer_proxy"]

    # ── Quote ────────────────────────────────────────────────────────────────

    async def get_quote(
        self,
        src_token: str,
        src_decimals: int,
        dst_token: str,
        dst_decimals: int,
        amount_wei: int,
        side: str = "SELL",
    ) -> dict:
        """Get a price quote with route. Returns the full Paraswap response
        (use response['priceRoute'] to feed into get_swap_tx).

        Args:
            src_token: input token contract (NATIVE_TOKEN_ADDRESS for native ETH)
            dst_token: output token contract
            amount_wei: input amount in token's smallest unit (or output for BUY side)
            side: SELL (default — sell exact src amount) or BUY (buy exact dst amount)
        """
        return await self._get("/prices", {
            "srcToken": src_token,
            "destToken": dst_token,
            "srcDecimals": src_decimals,
            "destDecimals": dst_decimals,
            "amount": str(amount_wei),
            "side": side,
            "network": self.chain_id,
        })

    # ── Swap (build unsigned tx from a priceRoute) ───────────────────────────

    async def get_swap_tx(
        self,
        price_route: dict,
        user_address: str,
        slippage_bps: int = 100,  # 1% default (matches Solana side)
        ignore_checks: bool = False,
    ) -> dict:
        """Build an unsigned swap transaction from a priceRoute.

        Returns: {from, to, value, data, gas, gasPrice, chainId}
        Sign with EVMWalletService.sign_tx and broadcast with send_raw_tx.
        """
        # Use slippage to compute min destination amount
        # priceRoute already contains srcAmount and destAmount strings
        body = {
            "srcToken":     price_route["srcToken"],
            "srcDecimals":  price_route["srcDecimals"],
            "destToken":    price_route["destToken"],
            "destDecimals": price_route["destDecimals"],
            "srcAmount":    price_route["srcAmount"],
            "slippage":     slippage_bps,  # in basis points
            "userAddress":  user_address,
            "priceRoute":   price_route,
        }
        params = {}
        if ignore_checks:
            params["ignoreChecks"] = "true"
        return await self._post(f"/transactions/{self.chain_id}", body, params)

    async def close(self) -> None:
        await self._client.aclose()
