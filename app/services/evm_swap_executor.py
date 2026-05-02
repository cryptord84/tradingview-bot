"""End-to-end EVM swap orchestrator: quote → check allowance → approve if needed → swap.

The EVM equivalent of `jupiter_client._send_swap`. Composes ParaswapClient
(quote + tx-build) with EVMWalletService (signing + broadcasting).

Two flows:
- ensure_allowance(): one-shot ERC20 approval if not already granted
- execute_swap(): full atomic flow with auto-approval

Both support dry_run=True (build + sign but don't broadcast — for testing
without spending gas or capital).
"""

import logging
from typing import Optional

from eth_utils import keccak

from app.services.evm_wallet_service import EVMWalletService
from app.services.openocean_client import OpenOceanClient, NATIVE_TOKEN_ADDRESS

logger = logging.getLogger("bot.evm_swap")


class EVMSwapExecutor:
    """Orchestrates Paraswap swaps for an EVMWalletService."""

    # Approve a generous buffer (1.5x) over the requested amount so we don't
    # need to re-approve for every trade. Don't use unlimited (security).
    DEFAULT_APPROVAL_MULTIPLIER = 1.5

    def __init__(
        self,
        wallet: EVMWalletService,
        aggregator: Optional[OpenOceanClient] = None,
    ):
        self.wallet = wallet
        self.aggregator = aggregator or OpenOceanClient(chain_id=wallet.chain_id)

    async def ensure_allowance(
        self,
        token_addr: str,
        amount_wei: int,
        dry_run: bool = False,
    ) -> Optional[dict]:
        """Check current allowance and approve if insufficient.

        Returns:
            None if allowance was already sufficient (no tx sent)
            dict with tx details if an approve was sent (or would be in dry-run)
        """
        spender = self.aggregator.approval_target

        # Skip allowance for native ETH swaps (no approval needed)
        if token_addr.lower() == NATIVE_TOKEN_ADDRESS.lower():
            return None

        current = await self.wallet.get_allowance(token_addr, spender)
        if current >= amount_wei:
            logger.info(f"allowance already sufficient: {current} >= {amount_wei}")
            return None

        # Approve a buffered amount so subsequent trades don't need re-approval
        approve_amount = int(amount_wei * self.DEFAULT_APPROVAL_MULTIPLIER)
        calldata = EVMWalletService.build_approve_calldata(spender, approve_amount)

        logger.info(
            f"approving {approve_amount} of {token_addr} to {spender} "
            f"(current allowance: {current})"
        )

        # ERC20 approve gas is well-known: ~46k for fresh slot, ~30k for existing
        return await self.wallet.sign_and_send(
            to=token_addr,
            data=calldata,
            value=0,
            gas_limit=80_000,  # generous for both fresh + existing slot writes
            dry_run=dry_run,
        )

    async def execute_swap(
        self,
        src_token: str,
        src_decimals: int,
        dst_token: str,
        dst_decimals: int,
        amount_wei: int,
        slippage_bps: int = 100,
        dry_run: bool = False,
        wait_for_swap_receipt: bool = True,
        receipt_timeout_s: int = 60,
    ) -> dict:
        """Quote → ensure approval → broadcast swap → wait for receipt.

        Returns:
            {
              "quote": {src_amount, dest_amount, src_usd, dest_usd, route},
              "approve_tx": {hash, broadcast, dry_run} or None,
              "swap_tx":    {hash, broadcast, dry_run, gas_limit, gas_price_wei},
              "receipt":    receipt dict or None,
            }
        """
        # 1. Ensure ERC20 approval first (OpenOcean's swap_quote uses fresh
        # allowance state; doing approval before quote avoids stale-state issues)
        approve_result = await self.ensure_allowance(src_token, amount_wei, dry_run=dry_run)

        # 2. Get quote + unsigned tx in one call (OpenOcean's swap_quote endpoint).
        # Convert wei amount → human-readable string for OpenOcean's API.
        amount_human = str(amount_wei / (10 ** src_decimals))
        # Convert slippage from bps to percent (slippage_bps=100 → 1.0%)
        slippage_pct = slippage_bps / 100.0
        # Estimate gas price in gwei for OpenOcean's gas-aware routing
        gas_price_wei = await self.wallet.get_gas_price_wei(with_buffer=False)
        gas_price_gwei = gas_price_wei / 1e9

        swap_data = await self.aggregator.get_swap_quote(
            src_token=src_token,
            dst_token=dst_token,
            amount_human=amount_human,
            from_address=self.wallet.address,
            gas_price_gwei=gas_price_gwei,
            slippage_pct=slippage_pct,
        )

        # OpenOcean returns: inAmount, outAmount, to, data, value, estimatedGas, gasPrice (wei)
        in_amount_wei  = int(swap_data["inAmount"])
        out_amount_wei = int(swap_data["outAmount"])
        # Some OpenOcean fields are strings, some integers — normalize
        oo_gas_price   = int(swap_data.get("gasPrice") or gas_price_wei)
        oo_gas_est     = int(swap_data.get("estimatedGas") or 200_000)

        # Extract route info (best-effort — schema varies)
        path = swap_data.get("path", {}).get("routes", [{}])[0]
        sub_routes = path.get("subRoutes", [{}])[0]
        route_dexes = [d.get("dex", "?") for d in sub_routes.get("dexes", [])]

        quote_summary = {
            "src_amount":  in_amount_wei / (10 ** src_decimals),
            "dest_amount": out_amount_wei / (10 ** dst_decimals),
            "src_usd":     float(swap_data.get("inToken", {}).get("usd", 0))
                              * (in_amount_wei / (10 ** src_decimals)),
            "dest_usd":    float(swap_data.get("outToken", {}).get("usd", 0))
                              * (out_amount_wei / (10 ** dst_decimals)),
            "route":       route_dexes,
            "gas_cost_units": oo_gas_est,
        }
        logger.info(
            f"quote: {quote_summary['src_amount']} src → {quote_summary['dest_amount']} dst "
            f"via {quote_summary['route']}"
        )

        # 3. Compose signing payload with gas-price buffer for base-fee creep.
        gas_with_buffer = int(oo_gas_est * 1.20)
        gas_price_buffered = int(oo_gas_price * self.wallet.GAS_PRICE_BUFFER)
        sign_input = {
            "from":     self.wallet.address,
            "to":       swap_data["to"],
            "data":     swap_data["data"],
            "value":    int(swap_data.get("value", 0)),
            "gas":      gas_with_buffer,
            "gasPrice": gas_price_buffered,
            "nonce":    await self.wallet.get_nonce(),
            "chainId":  self.wallet.chain_id,
        }

        # If we just sent an approve, bump the nonce by 1 (it's pending)
        if approve_result and approve_result.get("broadcast"):
            sign_input["nonce"] = approve_result["nonce"] + 1

        raw_signed = self.wallet.sign_tx(sign_input)
        local_hash = "0x" + keccak(bytes.fromhex(raw_signed.removeprefix("0x"))).hex()
        est_cost_eth = sign_input["gas"] * sign_input["gasPrice"] / 1e18

        swap_tx_result = {
            "hash": local_hash,
            "to": swap_data["to"],
            "gas_limit": sign_input["gas"],
            "gas_price_wei": sign_input["gasPrice"],
            "estimated_cost_eth": est_cost_eth,
            "broadcast": False,
            "dry_run": dry_run,
            "raw_signed": raw_signed,
        }

        receipt = None
        if not dry_run:
            broadcast_hash = await self.wallet.send_raw_tx(raw_signed)
            swap_tx_result["hash"] = broadcast_hash
            swap_tx_result["broadcast"] = True
            if wait_for_swap_receipt:
                receipt = await self.wallet.wait_for_receipt(
                    broadcast_hash, timeout_s=receipt_timeout_s,
                )

        return {
            "quote": quote_summary,
            "approve_tx": approve_result,
            "swap_tx": swap_tx_result,
            "receipt": receipt,
        }

    async def close(self) -> None:
        await self.aggregator.close()
        # Don't close the wallet — it may be reused elsewhere
