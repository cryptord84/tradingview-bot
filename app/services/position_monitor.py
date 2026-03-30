"""Position Monitor — background TP/SL auto-close service."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.config import get
from app.database import (
    get_open_positions,
    close_position,
    insert_trade,
    log_wallet_tx,
)

logger = logging.getLogger("bot.positions")


class PositionMonitor:
    """Polls SOL price and auto-closes positions when TP/SL is hit."""

    def __init__(self):
        cfg = get("position_monitor") or {}
        self.poll_interval = cfg.get("poll_interval_seconds", 30)
        self.max_retries = cfg.get("max_close_retries", 3)
        self.enabled = cfg.get("enabled", True)

        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> asyncio.Task:
        """Start the background polling task."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Position monitor started (poll every {self.poll_interval}s)")
        return self._task

    async def stop(self):
        """Stop polling and clean up."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Position monitor stopped")

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._check_positions()
            except Exception as e:
                logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _check_positions(self):
        """Check all open positions against current price."""
        positions = get_open_positions()
        if not positions:
            return

        # Lazy imports to avoid circular dependencies
        from app.services.jupiter_client import JupiterClient

        jupiter = JupiterClient()
        try:
            current_price = await jupiter.get_sol_price()
        except Exception as e:
            logger.warning(f"Price fetch failed, skipping check: {e}")
            await jupiter.close()
            return

        logger.debug(
            f"Checking {len(positions)} position(s) @ SOL=${current_price:.2f}"
        )

        for pos in positions:
            trigger = None
            if pos["direction"] == "long":
                if current_price >= pos["tp_price"]:
                    trigger = "tp"
                elif current_price <= pos["sl_price"]:
                    trigger = "sl"
            else:  # short (future use)
                if current_price <= pos["tp_price"]:
                    trigger = "tp"
                elif current_price >= pos["sl_price"]:
                    trigger = "sl"

            if trigger:
                logger.info(
                    f"Position #{pos['id']} {trigger.upper()} triggered @ ${current_price:.2f} "
                    f"(entry=${pos['entry_price']:.2f}, "
                    f"TP=${pos['tp_price']:.2f}, SL=${pos['sl_price']:.2f})"
                )
                await self._close_position(pos, current_price, trigger, jupiter)

        await jupiter.close()

    async def _close_position(
        self, pos: dict, current_price: float, trigger: str, jupiter=None
    ):
        """Execute a position close via Jupiter swap."""
        from app.services.jupiter_client import JupiterClient
        from app.services.wallet_service import WalletService
        from app.services.telegram_service import TelegramService
        from app.services.kamino_client import KaminoClient

        own_jupiter = jupiter is None
        if own_jupiter:
            jupiter = JupiterClient()

        wallet = WalletService()
        telegram = TelegramService()

        try:
            # For long positions: sell SOL for USDC
            input_mint = jupiter.sol_mint
            output_mint = jupiter.usdc_mint
            amount_lamports = int(pos["amount_sol"] * 1_000_000_000)

            swap_result = None
            for attempt in range(self.max_retries):
                try:
                    swap_result = await jupiter.execute_swap(
                        keypair=wallet.get_keypair(),
                        input_mint=input_mint,
                        output_mint=output_mint,
                        amount_lamports=amount_lamports,
                    )
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        logger.error(
                            f"Failed to close position #{pos['id']} after "
                            f"{self.max_retries} attempts: {e}"
                        )
                        await telegram.notify_error(
                            f"Failed to close position #{pos['id']}: {e}",
                            f"Entry=${pos['entry_price']:.2f}, "
                            f"Trigger={trigger.upper()} @ ${current_price:.2f}",
                        )
                        await telegram.close()
                        await wallet.close()
                        if own_jupiter:
                            await jupiter.close()
                        return
                    logger.warning(
                        f"Close attempt {attempt + 1} failed: {e}, retrying..."
                    )
                    await asyncio.sleep(2)

            # Calculate P&L
            pnl_usdc = (current_price - pos["entry_price"]) * pos["amount_sol"]
            pnl_percent = (
                (current_price - pos["entry_price"]) / pos["entry_price"]
            ) * 100

            # Map trigger to status
            status = {
                "tp": "closed_tp",
                "sl": "closed_sl",
                "manual": "closed_manual",
            }[trigger]

            # Update position in database
            close_position(
                position_id=pos["id"],
                exit_price=current_price,
                exit_tx=swap_result["tx_signature"],
                status=status,
                pnl_usdc=pnl_usdc,
                pnl_percent=pnl_percent,
            )

            # Log as a SELL trade for consistency
            insert_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "tx_id": swap_result["tx_signature"],
                "signal_type": "SELL",
                "symbol": pos["symbol"],
                "action": "EXECUTE",
                "amount_sol": pos["amount_sol"],
                "price_usd": current_price,
                "fees_sol": 0.000005,
                "leverage": 1,
                "wallet_address": wallet.public_key,
                "confidence_score": pos["confidence"],
                "claude_reasoning": f"Auto-close: {trigger.upper()} hit",
                "pnl_usd": pnl_usdc,
                "notes": f"Position #{pos['id']} {status} | "
                         f"Entry=${pos['entry_price']:.2f} Exit=${current_price:.2f}",
            })

            # Log wallet transaction
            swap_fee_sol = 0.000055  # base + priority fee estimate
            log_wallet_tx(
                tx_type="jupiter_swap",
                direction="sell",
                amount=pos["amount_sol"],
                token="SOL",
                fee_sol=swap_fee_sol,
                tx_signature=swap_result["tx_signature"],
                status="success",
                notes=f"Position #{pos['id']} {trigger.upper()} auto-close",
            )

            # Send Telegram notification
            trigger_label = {
                "tp": "Take Profit",
                "sl": "Stop Loss",
                "manual": "Manual Close",
            }[trigger]
            trigger_emoji = {"tp": "\U0001f3af", "sl": "\U0001f6d1", "manual": "\u2705"}[trigger]
            pnl_emoji = "\U0001f4c8" if pnl_usdc >= 0 else "\U0001f4c9"

            msg = (
                f"<b>{trigger_emoji} Position Closed — {trigger_label}</b>\n\n"
                f"Symbol: {pos['symbol']}\n"
                f"Entry: ${pos['entry_price']:.2f}\n"
                f"Exit: ${current_price:.2f}\n"
                f"Amount: {pos['amount_sol']:.4f} SOL\n"
                f"{pnl_emoji} P&L: <b>${pnl_usdc:+.2f}</b> ({pnl_percent:+.1f}%)\n"
                f"TX: <code>{swap_result['tx_signature'][:20]}...</code>"
            )
            await telegram.send_message(msg)

            logger.info(
                f"Position #{pos['id']} closed ({status}): "
                f"${pnl_usdc:+.2f} ({pnl_percent:+.1f}%)"
            )

            # Auto-deposit idle USDC to Kamino
            kamino = KaminoClient()
            if kamino.enabled and kamino.auto_deposit:
                try:
                    await asyncio.sleep(3)
                    usdc_balance = await wallet.get_usdc_balance()
                    deposit_result = await kamino.deposit_idle(
                        wallet.get_keypair(), usdc_balance
                    )
                    if deposit_result.get("success"):
                        await telegram.send_message(
                            f"Kamino Deposit: {deposit_result['amount_usdc']:.2f} "
                            f"USDC deposited to earn yield"
                        )
                except Exception as e:
                    logger.warning(f"Kamino auto-deposit after close failed: {e}")
            await kamino.close()

        finally:
            await telegram.close()
            await wallet.close()
            if own_jupiter:
                await jupiter.close()


# Singleton
_monitor: Optional[PositionMonitor] = None


def get_position_monitor() -> PositionMonitor:
    global _monitor
    if _monitor is None:
        _monitor = PositionMonitor()
    return _monitor
