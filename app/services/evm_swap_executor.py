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
from app.services.paraswap_client import ParaswapClient, NATIVE_TOKEN_ADDRESS

logger = logging.getLogger("bot.evm_swap")


class EVMSwapExecutor:
    """Orchestrates Paraswap swaps for an EVMWalletService."""

    # Approve a generous buffer (1.5x) over the requested amount so we don't
    # need to re-approve for every trade. Don't use unlimited (security).
    DEFAULT_APPROVAL_MULTIPLIER = 1.5

    def __init__(
        self,
        wallet: EVMWalletService,
        paraswap: Optional[ParaswapClient] = None,
    ):
        self.wallet = wallet
        self.paraswap = paraswap or ParaswapClient(chain_id=wallet.chain_id)

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
        spender = self.paraswap.approval_target

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
              "quote": {srcAmount, destAmount, srcUSD, destUSD, route},
              "approve_tx": {hash, broadcast, dry_run} or None,
              "swap_tx":    {hash, broadcast, dry_run, gas_limit, gas_price_wei},
              "receipt":    receipt dict or None,
            }
        """
        # 1. Get quote
        quote_resp = await self.paraswap.get_quote(
            src_token, src_decimals, dst_token, dst_decimals, amount_wei,
        )
        pr = quote_resp["priceRoute"]
        quote_summary = {
            "src_amount": int(pr["srcAmount"]) / (10 ** src_decimals),
            "dest_amount": int(pr["destAmount"]) / (10 ** dst_decimals),
            "src_usd": float(pr.get("srcUSD", 0)),
            "dest_usd": float(pr.get("destUSD", 0)),
            "route": [
                s.get("swapExchanges", [{}])[0].get("exchange", "?")
                for s in pr.get("bestRoute", [{}])[0].get("swaps", [])
            ],
            "gas_cost_units": int(pr.get("gasCost", 0)),
        }
        logger.info(
            f"quote: {quote_summary['src_amount']} src → {quote_summary['dest_amount']} dst "
            f"via {quote_summary['route']}"
        )

        # 2. Ensure ERC20 approval (skipped for native ETH input)
        approve_result = await self.ensure_allowance(src_token, amount_wei, dry_run=dry_run)

        # 3. Build the unsigned swap tx via Paraswap
        swap_tx = await self.paraswap.get_swap_tx(
            pr, self.wallet.address, slippage_bps=slippage_bps,
            ignore_checks=dry_run,  # skip Paraswap's balance pre-flight in dry-run
        )

        # 4. Compose the signing payload
        gas_with_buffer = int(quote_summary["gas_cost_units"] * 1.20)
        sign_input = {
            "from":     self.wallet.address,
            "to":       swap_tx["to"],
            "data":     swap_tx["data"],
            "value":    int(swap_tx["value"]),
            "gas":      gas_with_buffer,
            "gasPrice": int(swap_tx["gasPrice"]),
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
            "to": swap_tx["to"],
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
        await self.paraswap.close()
        # Don't close the wallet — it may be reused elsewhere
