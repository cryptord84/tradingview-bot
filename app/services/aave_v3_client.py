"""Aave V3 USDC supply client — Arbitrum.

Phase 3.1 (read-only): balance + APY queries. Mirrors Kamino's role on Solana
for the EVM lane (idle USDC yield ~3-4% APY).

Pool:  0x794a61358D6845594F94dc1DB02A252b5b4814aD
USDC:  0xaf88d065e77c8cC2239327C5EDb3A432268e5831 (Circle native USDC)
aUSDC: 0x724dc807b04555b71ed48a6896b6F41593b8C637 (rebasing receipt token)

Write methods (supply, withdraw, approve) added in Phase 3.2.
"""

import logging
import time
from typing import Optional

from app.config import get
from app.services.evm_wallet_service import EVMWalletService

logger = logging.getLogger("bot.aave_v3")

# Aave V3 protocol addresses on Arbitrum One.
AAVE_V3_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
USDC_CONTRACT = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
A_USDC_CONTRACT = "0x724dc807b04555b71ed48a6896b6F41593b8C637"

# Function selectors (first 4 bytes of keccak256(signature))
GET_RESERVE_DATA_SELECTOR = "0x35ea6a75"  # getReserveData(address)
SUPPLY_SELECTOR = "0x617ba037"           # supply(address,uint256,address,uint16)
WITHDRAW_SELECTOR = "0x69328dec"         # withdraw(address,uint256,address)

# Aave V3 stores rates in RAY (1e27) per the WadRayMath library convention.
RAY = 10 ** 27
SECONDS_PER_YEAR = 31_536_000  # 365 * 86400
MAX_UINT256 = (1 << 256) - 1
USDC_DECIMALS = 6


def _pad32(value) -> str:
    """Right-align to 32 bytes (64 hex chars). Accepts hex string or int."""
    if isinstance(value, str):
        h = value.lower().removeprefix("0x")
    else:
        h = hex(int(value)).removeprefix("0x")
    return h.rjust(64, "0")


class AaveV3Client:
    """Read-only Aave V3 USDC supply client.

    Uses the existing EVMWalletService for RPC access + wallet identity. No
    state-changing operations in this phase — pure queries.
    """

    def __init__(self):
        cfg = get("aave_v3") or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.reserve_usdc = float(cfg.get("reserve_usdc", 25))
        self.auto_deposit = bool(cfg.get("auto_deposit", False))
        self.min_deposit_usd = float(cfg.get("min_deposit_usd", 10))
        self.cache_seconds = int(cfg.get("reserve_data_cache_seconds", 60))
        self.max_gas_gwei = float(cfg.get("max_gas_gwei", 0.5))
        self.approve_mode = str(cfg.get("approve_mode", "infinite"))

        self._cached_apy: Optional[float] = None
        self._cached_apy_ts: float = 0.0
        self._wallet: Optional[EVMWalletService] = None

        if not self.enabled:
            logger.info("AaveV3Client disabled via config")

    @property
    def wallet(self) -> Optional[EVMWalletService]:
        """Lazy-load EVM wallet on first access. None if config missing."""
        if self._wallet is None and self.enabled:
            try:
                self._wallet = EVMWalletService()
            except Exception as e:
                logger.warning(f"AaveV3Client: EVM wallet unavailable ({e})")
                self.enabled = False
        return self._wallet

    async def get_supply_balance(self) -> float:
        """USDC supplied to Aave (current aUSDC balance, USDC has 6 decimals).

        aUSDC is 1:1 redeemable for USDC. Balance accrues yield via rebasing
        (the underlying balance grows; aUSDC balance stays nominally at the
        deposit amount + accrued interest).
        """
        if not self.enabled or self.wallet is None:
            return 0.0
        try:
            return await self.wallet.get_erc20_balance(A_USDC_CONTRACT, decimals=6)
        except Exception as e:
            logger.warning(f"AaveV3Client.get_supply_balance failed: {e}")
            return 0.0

    async def get_supply_apy(self) -> float:
        """Current supply APY for USDC. Returns 0.035 for 3.5% APY.

        Reads `currentLiquidityRate` from getReserveData(USDC) — that's the
        per-second rate in RAY units. Converts to compounded annual yield
        matching what Aave's UI displays.
        """
        if not self.enabled or self.wallet is None:
            return 0.0

        now = time.time()
        if self._cached_apy is not None and now - self._cached_apy_ts < self.cache_seconds:
            return self._cached_apy

        try:
            usdc_padded = USDC_CONTRACT.lower().removeprefix("0x").rjust(64, "0")
            calldata = GET_RESERVE_DATA_SELECTOR + usdc_padded
            data = await self.wallet._rpc("eth_call", [
                {"to": AAVE_V3_POOL, "data": calldata}, "latest",
            ])
            result_hex = data.get("result", "")
            if not result_hex or result_hex == "0x":
                return 0.0
            # ReserveData layout (each field padded to 32 bytes / 64 hex chars):
            #   slot 0: configuration (ReserveConfigurationMap)
            #   slot 1: liquidityIndex (uint128)
            #   slot 2: currentLiquidityRate (uint128 in RAY)  <-- what we want
            #   slot 3+: variableBorrowIndex, currentVariableBorrowRate, ...
            body = result_hex.removeprefix("0x")
            if len(body) < 64 * 3:
                return 0.0
            liquidity_rate_ray = int(body[64 * 2:64 * 3], 16)
            # Aave APY formula (per official docs): compound the per-second rate
            rate_per_second = liquidity_rate_ray / RAY / SECONDS_PER_YEAR
            apy = (1 + rate_per_second) ** SECONDS_PER_YEAR - 1
            self._cached_apy = apy
            self._cached_apy_ts = now
            return apy
        except Exception as e:
            logger.warning(f"AaveV3Client.get_supply_apy failed: {e}")
            return 0.0

    # ── Phase 3.2: write methods (supply / withdraw / approve) ──────────────

    def _build_supply_calldata(self, amount_usdc_units: int) -> str:
        """Build calldata for supply(USDC, amount, this_wallet, referralCode=0)."""
        return (
            SUPPLY_SELECTOR
            + _pad32(USDC_CONTRACT)
            + _pad32(amount_usdc_units)
            + _pad32(self.wallet.address)
            + _pad32(0)  # referralCode
        )

    def _build_withdraw_calldata(self, amount_usdc_units: int) -> str:
        """Build calldata for withdraw(USDC, amount, this_wallet).

        amount = MAX_UINT256 means withdraw entire aUSDC balance.
        """
        return (
            WITHDRAW_SELECTOR
            + _pad32(USDC_CONTRACT)
            + _pad32(amount_usdc_units)
            + _pad32(self.wallet.address)
        )

    async def _check_gas_ok(self) -> tuple[bool, float, str]:
        """Verify Arbitrum gas price is below max_gas_gwei. Returns (ok, gwei, msg)."""
        gas_wei = await self.wallet.get_gas_price_wei(with_buffer=False)
        gwei = gas_wei / 1e9
        if gwei > self.max_gas_gwei:
            return False, gwei, f"gas {gwei:.4f} gwei > limit {self.max_gas_gwei}"
        return True, gwei, "ok"

    async def get_usdc_allowance(self) -> int:
        """Current USDC allowance from this wallet to the Aave V3 Pool."""
        if not self.enabled or self.wallet is None:
            return 0
        return await self.wallet.get_allowance(USDC_CONTRACT, AAVE_V3_POOL)

    async def ensure_approve(self, dry_run: bool = True) -> dict:
        """Approve USDC → Aave Pool for MAX_UINT256 if not already.

        - If existing allowance is "large enough" (≥ 2^100), no-op.
        - Otherwise build + sign an approve tx. Broadcasts iff dry_run is False.

        Returns:
            {
              "needed": bool,
              "current_allowance": int,
              "broadcast": bool,
              "tx_hash": str | None,
              "dry_run": bool,
            }
        """
        if not self.enabled or self.wallet is None:
            return {"needed": False, "error": "client disabled"}

        current = await self.get_usdc_allowance()
        # Heuristic: if allowance is already enormous, skip. Avoids re-approving
        # after an infinite approve (~115792089237316195423570985008687907853269984665640564039457...).
        if current >= (1 << 200):
            return {
                "needed": False,
                "current_allowance": current,
                "broadcast": False,
                "tx_hash": None,
                "dry_run": dry_run,
            }

        target = MAX_UINT256 if self.approve_mode == "infinite" else 0
        calldata = self.wallet.build_approve_calldata(AAVE_V3_POOL, target)

        try:
            tx_result = await self.wallet.sign_and_send(
                to=USDC_CONTRACT, data=calldata, dry_run=dry_run,
            )
        except Exception as e:
            return {"needed": True, "error": f"approve build failed: {e}", "dry_run": dry_run}

        return {
            "needed": True,
            "current_allowance": current,
            "broadcast": tx_result.get("broadcast", False),
            "tx_hash": tx_result.get("tx_hash"),
            "estimated_gas_cost_eth": tx_result.get("estimated_gas_cost_eth"),
            "dry_run": dry_run,
        }

    async def supply_usdc(self, amount_usdc: float, dry_run: bool = True) -> dict:
        """Supply USDC to Aave V3.

        Validation:
          1. Wallet has sufficient USDC
          2. Arbitrum gas price below max_gas_gwei
          3. ERC20 approve in place (auto-approves if needed)

        If dry_run=True and approve is needed, this returns *only* the approve
        dry-run — the supply tx can't be built without on-chain approve. Run
        with dry_run=False once to do the approve, then dry_run=True will
        return the supply preview.
        """
        if not self.enabled or self.wallet is None:
            return {"success": False, "error": "client disabled"}
        if amount_usdc <= 0:
            return {"success": False, "error": "amount must be > 0"}

        amount_units = int(round(amount_usdc * 10 ** USDC_DECIMALS))

        # 1. Wallet USDC balance
        wallet_usdc = await self.wallet.get_erc20_balance(USDC_CONTRACT, decimals=USDC_DECIMALS)
        if amount_usdc > wallet_usdc:
            return {
                "success": False,
                "error": f"insufficient USDC: have ${wallet_usdc:.2f}, need ${amount_usdc:.2f}",
            }

        # 2. Gas check
        gas_ok, gwei, gas_msg = await self._check_gas_ok()
        if not gas_ok:
            return {"success": False, "error": gas_msg, "gas_gwei": gwei}

        # 3. Approve check
        approve_result = await self.ensure_approve(dry_run=dry_run)
        if approve_result.get("error"):
            return {"success": False, "error": f"approve: {approve_result['error']}"}
        approve_needed = approve_result.get("needed", False)

        # If we need an approve and we're in dry-run, we can't reliably estimate
        # gas for the supply tx (eth_estimateGas would revert because allowance
        # is still zero on-chain). Return early with the approve preview only.
        if approve_needed and dry_run:
            return {
                "success": True,
                "stage": "approve_only_dry_run",
                "message": (
                    "Approve required first. Run with dry_run=False to broadcast "
                    "approve, then re-run supply (the supply preview will work "
                    "after allowance is on-chain)."
                ),
                "amount_usdc": amount_usdc,
                "wallet_usdc_before": wallet_usdc,
                "gas_gwei": gwei,
                "approve": approve_result,
                "dry_run": True,
            }

        # If we need approve and we're real-running, broadcast approve and wait
        if approve_needed and not dry_run:
            tx_hash = approve_result.get("tx_hash")
            try:
                receipt = await self.wallet.wait_for_receipt(tx_hash, timeout_s=60)
                if int(receipt.get("status", "0x0"), 16) != 1:
                    return {
                        "success": False,
                        "error": "approve tx reverted",
                        "approve_tx_hash": tx_hash,
                    }
            except TimeoutError as e:
                return {"success": False, "error": f"approve receipt timeout: {e}"}

        # 4. Build supply tx (now safe — allowance is on-chain or already there)
        calldata = self._build_supply_calldata(amount_units)
        try:
            supply_tx = await self.wallet.sign_and_send(
                to=AAVE_V3_POOL, data=calldata, dry_run=dry_run,
            )
        except Exception as e:
            return {"success": False, "error": f"supply tx failed: {e}"}

        # 5. Wait for receipt if real-run
        receipt_status = None
        if not dry_run:
            try:
                receipt = await self.wallet.wait_for_receipt(supply_tx["tx_hash"], timeout_s=60)
                receipt_status = int(receipt.get("status", "0x0"), 16)
                if receipt_status != 1:
                    return {
                        "success": False,
                        "error": "supply tx reverted",
                        "tx_hash": supply_tx["tx_hash"],
                    }
            except TimeoutError as e:
                return {"success": False, "error": f"supply receipt timeout: {e}"}

        # Invalidate balance cache so next get_supply_balance() refetches
        if not dry_run:
            self.wallet.invalidate_cache()

        return {
            "success": True,
            "stage": "supply" + ("_dry_run" if dry_run else ""),
            "amount_usdc": amount_usdc,
            "amount_units": amount_units,
            "wallet_usdc_before": wallet_usdc,
            "gas_gwei": gwei,
            "approve": approve_result,
            "supply_tx": supply_tx,
            "receipt_status": receipt_status,
            "dry_run": dry_run,
        }

    async def withdraw_usdc(self, amount_usdc: Optional[float], dry_run: bool = True) -> dict:
        """Withdraw USDC from Aave V3.

        Pass amount_usdc=None or any value <=0 to withdraw the full balance
        (uses MAX_UINT256, which Aave interprets as "all").
        """
        if not self.enabled or self.wallet is None:
            return {"success": False, "error": "client disabled"}

        supplied = await self.get_supply_balance()
        if supplied <= 0:
            return {"success": False, "error": "no aUSDC balance to withdraw"}

        # Determine units
        if amount_usdc is None or amount_usdc <= 0:
            amount_units = MAX_UINT256
            amount_display = f"ALL (~${supplied:.6f})"
        else:
            if amount_usdc > supplied:
                return {
                    "success": False,
                    "error": f"requested ${amount_usdc:.2f} > supplied ${supplied:.6f}",
                }
            amount_units = int(round(amount_usdc * 10 ** USDC_DECIMALS))
            amount_display = f"${amount_usdc:.2f}"

        # Gas check
        gas_ok, gwei, gas_msg = await self._check_gas_ok()
        if not gas_ok:
            return {"success": False, "error": gas_msg, "gas_gwei": gwei}

        # Build & send
        calldata = self._build_withdraw_calldata(amount_units)
        try:
            withdraw_tx = await self.wallet.sign_and_send(
                to=AAVE_V3_POOL, data=calldata, dry_run=dry_run,
            )
        except Exception as e:
            return {"success": False, "error": f"withdraw tx failed: {e}"}

        receipt_status = None
        if not dry_run:
            try:
                receipt = await self.wallet.wait_for_receipt(withdraw_tx["tx_hash"], timeout_s=60)
                receipt_status = int(receipt.get("status", "0x0"), 16)
                if receipt_status != 1:
                    return {
                        "success": False,
                        "error": "withdraw tx reverted",
                        "tx_hash": withdraw_tx["tx_hash"],
                    }
            except TimeoutError as e:
                return {"success": False, "error": f"withdraw receipt timeout: {e}"}
            self.wallet.invalidate_cache()

        return {
            "success": True,
            "stage": "withdraw" + ("_dry_run" if dry_run else ""),
            "amount_display": amount_display,
            "amount_units": amount_units,
            "supplied_before": supplied,
            "gas_gwei": gwei,
            "withdraw_tx": withdraw_tx,
            "receipt_status": receipt_status,
            "dry_run": dry_run,
        }

    # ── Auto-deposit / withdraw helpers (Phase 3.3+3.4) ─────────────────────

    async def deposit_idle(self, dry_run: bool = False) -> dict:
        """Deposit excess wallet USDC (above reserve_usdc) to Aave.

        Mirrors KaminoClient.deposit_idle() — call this after a trade settles
        when capital may have freed up. Returns a Kamino-shaped result with
        success/skipped + amount_usdc fields so the existing telegram/log
        wiring can stay symmetrical.
        """
        if not self.enabled or self.wallet is None:
            return {"success": False, "skipped": True, "reason": "client disabled"}
        if not self.auto_deposit:
            return {"success": False, "skipped": True, "reason": "auto-deposit disabled"}

        wallet_usdc = await self.wallet.get_erc20_balance(USDC_CONTRACT, decimals=USDC_DECIMALS)
        deposit_amount = wallet_usdc - self.reserve_usdc
        if deposit_amount < self.min_deposit_usd:
            return {
                "success": False,
                "skipped": True,
                "reason": f"only ${deposit_amount:.2f} above reserve (min ${self.min_deposit_usd})",
                "wallet_usdc": wallet_usdc,
            }

        result = await self.supply_usdc(deposit_amount, dry_run=dry_run)
        # Normalize shape: caller expects {success, amount_usdc, ...}
        if result.get("success"):
            result["amount_usdc"] = deposit_amount
            result["wallet_usdc_before"] = wallet_usdc
        return result

    async def withdraw_for_trade(self, trade_usd: float, dry_run: bool = False) -> dict:
        """JIT withdraw enough USDC from Aave to cover a pending trade + reserve.

        Called before an EVM BUY when wallet USDC is insufficient. Withdraws
        `trade_usd + reserve - wallet_usdc` (clamped to supplied balance).

        Returns:
            {"success": bool, "skipped": bool, "amount_usdc": float, ...}
            success=False with skipped=True means no withdraw was needed
            (wallet already had enough) — caller should proceed.
        """
        if not self.enabled or self.wallet is None:
            return {"success": False, "skipped": True, "reason": "client disabled"}

        wallet_usdc = await self.wallet.get_erc20_balance(USDC_CONTRACT, decimals=USDC_DECIMALS)
        needed = trade_usd + self.reserve_usdc
        if wallet_usdc >= needed:
            return {
                "success": True,
                "skipped": True,
                "reason": "wallet already has enough USDC",
                "wallet_usdc": wallet_usdc,
                "needed": needed,
            }

        gap = needed - wallet_usdc
        supplied = await self.get_supply_balance()
        if supplied <= 0:
            return {
                "success": False,
                "skipped": False,
                "reason": "wallet short and no aUSDC to withdraw",
                "wallet_usdc": wallet_usdc,
                "supplied": supplied,
                "gap": gap,
            }

        # Withdraw the gap, capped at what we have supplied. We don't pull the
        # whole supplied balance — that would defeat the yield point.
        withdraw_amount = min(gap, supplied)
        # If supplied is less than gap, we'll still come up short — withdraw
        # what we can and let the caller decide whether to proceed with a
        # smaller trade or skip.
        result = await self.withdraw_usdc(withdraw_amount, dry_run=dry_run)
        if result.get("success"):
            result["amount_usdc"] = withdraw_amount
            result["wallet_usdc_before"] = wallet_usdc
            result["needed"] = needed
            result["gap_filled"] = withdraw_amount >= gap
        return result

    async def get_status(self) -> dict:
        """Bundled status for dashboard. Mirrors Kamino's status payload shape."""
        if not self.enabled or self.wallet is None:
            return {
                "enabled": False,
                "configured": False,
                "deposited_usd": 0.0,
                "supply_apy": 0.0,
                "daily_yield_est": 0.0,
                "monthly_yield_est": 0.0,
                "reserve_usdc": self.reserve_usdc,
                "auto_deposit": self.auto_deposit,
            }
        balance = await self.get_supply_balance()
        apy = await self.get_supply_apy()
        daily = balance * apy / 365 if balance > 0 else 0.0
        monthly = balance * apy / 12 if balance > 0 else 0.0
        return {
            "enabled": True,
            "configured": True,
            "deposited_usd": balance,
            "supply_apy": apy,
            "daily_yield_est": daily,
            "monthly_yield_est": monthly,
            "reserve_usdc": self.reserve_usdc,
            "auto_deposit": self.auto_deposit,
        }
