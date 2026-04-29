"""Kamino Lend client for USDC yield on idle funds."""

import asyncio
import base64
import logging
import time
from typing import Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from app.config import get
from app.database import log_wallet_tx

logger = logging.getLogger("bot.kamino")

# Module-level cache for Kamino balance — survives across KaminoClient instances
_kamino_balance_cache: dict = {"value": 0.0, "timestamp": 0.0}
_KAMINO_CACHE_TTL = 120  # 2 minutes — Kamino positions don't change often


class KaminoClient:
    """Client for Kamino Lend — deposit/withdraw USDC to earn yield."""

    def __init__(self):
        cfg = get("kamino")
        self.enabled = cfg.get("enabled", False)
        self.api_base = cfg.get("api_base", "https://api.kamino.finance")
        self.market = cfg.get("market", "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF")
        self.usdc_reserve = cfg.get("usdc_reserve", "D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59")
        self.min_deposit = cfg.get("min_deposit_usdc", 5.0)
        self.reserve_usdc = cfg.get("reserve_usdc", 2.0)
        self.auto_deposit = cfg.get("auto_deposit", True)
        self.auto_withdraw = cfg.get("auto_withdraw", True)
        self._client = httpx.AsyncClient(timeout=30)

    async def get_reserve_metrics(self) -> dict:
        """Get USDC reserve metrics including APY and utilization."""
        try:
            resp = await self._client.get(
                f"{self.api_base}/kamino-market/{self.market}/reserves/metrics"
            )
            resp.raise_for_status()
            reserves = resp.json()

            # Find USDC reserve
            for reserve in reserves:
                if reserve.get("reserve") == self.usdc_reserve:
                    total_supply = float(reserve.get("totalSupply", 0))
                    total_borrow = float(reserve.get("totalBorrow", 0))
                    utilization = (total_borrow / total_supply * 100) if total_supply > 0 else 0
                    return {
                        "supply_apy": float(reserve.get("supplyApy", 0)) * 100,
                        "borrow_apy": float(reserve.get("borrowApy", 0)) * 100,
                        "total_deposited": total_supply,
                        "total_borrowed": total_borrow,
                        "utilization": utilization,
                        "available": True,
                    }

            logger.warning("USDC reserve not found in Kamino market data")
            return {"available": False, "error": "USDC reserve not found"}
        except Exception as e:
            logger.error(f"Failed to get Kamino metrics: {e}")
            return {"available": False, "error": str(e)}

    async def get_user_position(self, wallet_address: str) -> dict:
        """Get user's deposited USDC balance in Kamino.

        Uses a module-level cache (2 min TTL) and falls back to the last known
        balance if the API is slow or errors out — prevents undercounting
        purchasing power during trade decisions.
        """
        global _kamino_balance_cache

        # Return cached value if fresh
        now = time.time()
        if now - _kamino_balance_cache["timestamp"] < _KAMINO_CACHE_TTL:
            cached = _kamino_balance_cache["value"]
            return {"deposited_usdc": cached, "has_position": cached > 0, "cached": True}

        # Try up to 2 attempts with a short retry
        last_error = None
        for attempt in range(2):
            try:
                resp = await self._client.get(
                    f"{self.api_base}/kamino-market/{self.market}/users/{wallet_address}/obligations",
                    timeout=10,
                )
                resp.raise_for_status()
                obligations = resp.json()

                deposited_usdc = 0.0
                for obligation in obligations:
                    stats = obligation.get("refreshedStats", {})
                    total_deposit = float(stats.get("userTotalDeposit", 0))
                    if total_deposit > 0:
                        deposited_usdc += total_deposit

                # Update cache on success
                _kamino_balance_cache["value"] = deposited_usdc
                _kamino_balance_cache["timestamp"] = now

                return {
                    "deposited_usdc": deposited_usdc,
                    "has_position": deposited_usdc > 0,
                }
            except Exception as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(1)

        # API failed — fall back to last known balance rather than reporting $0
        cached = _kamino_balance_cache["value"]
        if cached > 0:
            logger.warning(
                f"Kamino API failed ({last_error}), using last known balance: ${cached:.2f}"
            )
            return {"deposited_usdc": cached, "has_position": True, "stale": True}

        logger.error(f"Failed to get Kamino position (no cached fallback): {last_error}")
        return {"deposited_usdc": 0.0, "has_position": False, "error": str(last_error)}

    async def deposit(self, keypair: Keypair, amount_usdc: float) -> dict:
        """Deposit USDC into Kamino Lend."""
        if amount_usdc < self.min_deposit:
            return {"success": False, "error": f"Amount {amount_usdc} below minimum {self.min_deposit}"}

        wallet = str(keypair.pubkey())
        logger.info(f"Depositing {amount_usdc} USDC into Kamino Lend")

        return await self._execute_kamino_tx(
            keypair, "deposit", f"{self.api_base}/ktx/klend/deposit",
            {"wallet": wallet, "market": self.market, "reserve": self.usdc_reserve, "amount": str(amount_usdc)},
            amount_usdc,
        )

    async def withdraw(self, keypair: Keypair, amount_usdc: float) -> dict:
        """Withdraw USDC from Kamino Lend."""
        wallet = str(keypair.pubkey())
        logger.info(f"Withdrawing {amount_usdc} USDC from Kamino Lend")

        return await self._execute_kamino_tx(
            keypair, "withdraw", f"{self.api_base}/ktx/klend/withdraw",
            {"wallet": wallet, "market": self.market, "reserve": self.usdc_reserve, "amount": str(amount_usdc)},
            amount_usdc,
        )

    async def withdraw_all(self, keypair: Keypair) -> dict:
        """Withdraw all USDC from Kamino Lend."""
        wallet = str(keypair.pubkey())
        position = await self.get_user_position(wallet)
        deposited = position.get("deposited_usdc", 0)

        if deposited <= 0:
            return {"success": True, "amount_usdc": 0, "action": "withdraw_all", "note": "nothing deposited"}

        return await self.withdraw(keypair, deposited)

    async def deposit_idle(self, keypair: Keypair, wallet_usdc_balance: float) -> dict:
        """Deposit idle USDC, keeping reserve for fees."""
        if not self.auto_deposit or not self.enabled:
            return {"success": False, "skipped": True, "reason": "auto-deposit disabled"}

        deposit_amount = wallet_usdc_balance - self.reserve_usdc
        if deposit_amount < self.min_deposit:
            return {"success": False, "skipped": True, "reason": f"only {deposit_amount:.2f} available after reserve"}

        return await self.deposit(keypair, deposit_amount)

    async def _execute_kamino_tx(
        self, keypair: Keypair, action: str, url: str, body: dict, amount_usdc: float, max_retries: int = 3,
    ) -> dict:
        """Get transaction from Kamino API, sign, send, and confirm on-chain."""
        import asyncio

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # Get fresh transaction (fresh blockhash each time)
                resp = await self._client.post(url, json=body)
                resp.raise_for_status()
                tx_data = resp.json()
                tx_b64 = tx_data.get("transaction")
                if not tx_b64:
                    return {"success": False, "error": "No transaction returned from Kamino API"}

                # Sign and send immediately (minimize blockhash staleness)
                tx_sig = await self._sign_and_send(keypair, tx_b64)
                logger.info(f"Kamino {action} sent: {tx_sig}, awaiting confirmation...")

                # Confirm on-chain before logging as success — skipPreflight lets bad txs
                # through, so we must verify. Poll getSignatureStatuses up to 45s.
                confirmed = await self._confirm_signature(tx_sig, timeout_s=45)
                if not confirmed["ok"]:
                    last_error = RuntimeError(confirmed.get("error", "tx not confirmed"))
                    if attempt < max_retries:
                        logger.warning(f"Kamino {action} tx {tx_sig} unconfirmed: {last_error}, retrying...")
                        await asyncio.sleep(attempt)
                        continue
                    # Log as failed so DB stays accurate
                    log_wallet_tx(
                        tx_type=f"kamino_{action}",
                        direction=("out" if action == "deposit" else "in"),
                        amount=amount_usdc,
                        token="USDC",
                        tx_signature=tx_sig,
                        status="failed",
                        notes=f"unconfirmed: {last_error}",
                    )
                    return {"success": False, "error": str(last_error), "tx_signature": tx_sig}

                direction = "out" if action == "deposit" else "in"
                log_wallet_tx(
                    tx_type=f"kamino_{action}",
                    direction=direction,
                    amount=amount_usdc,
                    token="USDC",
                    tx_signature=tx_sig,
                    status="success",
                    notes=f"Kamino Lend {action}",
                )

                # Invalidate cache — balance just changed
                _kamino_balance_cache["timestamp"] = 0

                return {
                    "success": True,
                    "tx_signature": tx_sig,
                    "amount_usdc": amount_usdc,
                    "action": action,
                }
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(f"Kamino {action} attempt {attempt} failed: {e}, retrying in {attempt}s...")
                    await asyncio.sleep(attempt)

        logger.error(f"Kamino {action} failed after {max_retries} attempts: {last_error}")
        direction = "out" if action == "deposit" else "in"
        log_wallet_tx(
            tx_type=f"kamino_{action}",
            direction=direction,
            amount=amount_usdc,
            token="USDC",
            status="failed",
            notes=str(last_error)[:200],
        )
        return {"success": False, "error": str(last_error)}

    async def _sign_and_send(self, keypair: Keypair, tx_b64: str) -> str:
        """Deserialize, sign, and send a transaction."""
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])

        rpc_url = get("wallet", "rpc_url", "https://api.mainnet-beta.solana.com")
        signed_bytes = bytes(signed_tx)
        encoded = base64.b64encode(signed_bytes).decode()

        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "skipPreflight": True,  # Skip simulation — stale slot in lookup table creation causes false failures
                    "maxRetries": 3,
                },
            ],
        }
        resp = await self._client.post(rpc_url, json=rpc_body)
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            raise RuntimeError(f"Transaction failed: {result['error']}")

        return result.get("result", "")

    async def _confirm_signature(self, tx_sig: str, timeout_s: int = 45) -> dict:
        """Poll getSignatureStatuses until confirmed/finalized or timeout.

        Returns {"ok": True, "status": "..."} on success,
        {"ok": False, "error": "..."} on failure/timeout.
        """
        import asyncio

        rpc_url = get("wallet", "rpc_url", "https://api.mainnet-beta.solana.com")
        deadline = time.time() + timeout_s
        poll = 0
        while time.time() < deadline:
            try:
                resp = await self._client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[tx_sig], {"searchTransactionHistory": True}],
                    },
                )
                resp.raise_for_status()
                result = resp.json().get("result", {}).get("value", [None])[0]
                if result:
                    if result.get("err"):
                        return {"ok": False, "error": f"on-chain error: {result['err']}"}
                    conf = result.get("confirmationStatus")
                    if conf in ("confirmed", "finalized"):
                        return {"ok": True, "status": conf}
            except Exception as e:
                logger.debug(f"confirm poll {poll}: {e}")
            poll += 1
            await asyncio.sleep(min(2 + poll * 0.5, 5))
        return {"ok": False, "error": "timeout"}

    async def close(self):
        await self._client.aclose()
