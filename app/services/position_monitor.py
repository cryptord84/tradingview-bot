"""Position Monitor — background TP/SL auto-close service with trailing stop."""

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
    update_trail_sl,
)

logger = logging.getLogger("bot.positions")


class PositionMonitor:
    """Polls SOL price and auto-closes positions when TP/SL is hit.

    Supports trailing stop-loss: once price moves past activation threshold
    (entry + activation_atr * ATR), the SL ratchets up behind price at
    offset_atr * ATR distance. The effective SL is max(fixed SL, trailing SL).
    """

    def __init__(self):
        cfg = get("position_monitor") or {}
        self.poll_interval = cfg.get("poll_interval_seconds", 30)
        self.max_retries = cfg.get("max_close_retries", 3)
        self.enabled = cfg.get("enabled", True)

        # Trailing stop config
        trail_cfg = cfg.get("trailing_stop") or {}
        self.trail_enabled = trail_cfg.get("enabled", False)
        self.trail_activation_atr = trail_cfg.get("activation_atr", 1.5)
        self.trail_offset_atr = trail_cfg.get("offset_atr", 1.0)

        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> asyncio.Task:
        """Start the background polling task."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        trail_status = "ON" if self.trail_enabled else "OFF"
        logger.info(
            f"Position monitor started (poll every {self.poll_interval}s, "
            f"trailing stop: {trail_status})"
        )
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
        """Check all open positions against current price per token."""
        positions = get_open_positions()
        if not positions:
            return

        from app.services.jupiter_client import JupiterClient
        from app.services.price_feed import get_price_feed

        feed = get_price_feed()
        jupiter = JupiterClient()

        for pos in positions:
            # Resolve the token symbol from the position (e.g. WIFUSDT → WIF)
            token_symbol = pos.get("symbol", "SOLUSDT").replace("USDT", "").replace("USD", "")

            # Try real-time price feed first (instant, no network call)
            current_price = None
            if feed.is_running:
                pd = feed.get_price(token_symbol)
                if pd and pd.price > 0:
                    current_price = pd.price

            # Fall back to Jupiter HTTP if feed unavailable
            if current_price is None:
                try:
                    if token_symbol == "SOL":
                        current_price = await jupiter.get_sol_price()
                    else:
                        current_price = await jupiter.get_token_price(token_symbol)
                except Exception as e:
                    logger.warning(f"Price fetch failed for {token_symbol}, skipping: {e}")
                    continue

            logger.debug(
                f"Position #{pos['id']} {token_symbol} @ ${current_price:.6f} "
                f"(TP=${pos['tp_price']:.6f} SL=${pos['sl_price']:.6f})"
            )

            trigger = None
            effective_sl = pos["sl_price"]

            if pos["direction"] == "long":
                # Update trailing stop if enabled and ATR is available
                if self.trail_enabled and pos.get("atr") and pos["atr"] > 0:
                    effective_sl = self._update_trailing_sl(pos, current_price)

                if current_price >= pos["tp_price"]:
                    trigger = "tp"
                elif current_price <= effective_sl:
                    trigger = "trail_sl" if effective_sl > pos["sl_price"] else "sl"
            else:  # short (future use)
                if current_price <= pos["tp_price"]:
                    trigger = "tp"
                elif current_price >= pos["sl_price"]:
                    trigger = "sl"

            if trigger:
                logger.info(
                    f"Position #{pos['id']} {trigger.upper()} triggered @ ${current_price:.2f} "
                    f"(entry=${pos['entry_price']:.2f}, "
                    f"TP=${pos['tp_price']:.2f}, SL=${pos['sl_price']:.2f}, "
                    f"Trail SL=${effective_sl:.2f})"
                )
                await self._close_position(pos, current_price, trigger, jupiter)

        await jupiter.close()

    def _update_trailing_sl(self, pos: dict, current_price: float) -> float:
        """Update trailing stop-loss and return the effective SL price.

        Trail activates when price reaches entry + activation_atr * ATR.
        Once active, trail SL = current_price - offset_atr * ATR.
        Trail only ratchets UP (never down).
        """
        atr = pos["atr"]
        entry = pos["entry_price"]
        activation_price = entry + (self.trail_activation_atr * atr)

        # Current trail SL from DB (may be None)
        current_trail = pos.get("trail_sl_price") or 0

        if current_price >= activation_price:
            # Trail is active — compute new trail level
            new_trail = current_price - (self.trail_offset_atr * atr)

            # Only ratchet up, never down
            if new_trail > current_trail:
                update_trail_sl(pos["id"], new_trail)
                logger.debug(
                    f"Position #{pos['id']}: trail SL updated "
                    f"${current_trail:.2f} → ${new_trail:.2f} "
                    f"(price=${current_price:.2f}, activation=${activation_price:.2f})"
                )
                current_trail = new_trail

        # Effective SL is the higher of fixed SL and trailing SL
        return max(pos["sl_price"], current_trail)

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
            # For long positions: sell target token for USDC
            token_symbol = pos.get("symbol", "SOLUSDT").replace("USDT", "").replace("USD", "")
            from app.services.trade_engine import TradeEngine
            input_mint = TradeEngine._KNOWN_MINTS.get(token_symbol, jupiter.sol_mint)
            output_mint = jupiter.usdc_mint
            decimals = TradeEngine._TOKEN_DECIMALS.get(token_symbol, 9)

            # Pre-flight balance check: verify wallet holds sufficient tokens.
            # Uses actual wallet balance for the swap to handle small discrepancies
            # (fees, rounding) between the recorded position amount and real balance.
            swap_amount = pos["amount_sol"]
            try:
                spl_balances = await wallet.get_spl_token_balances()
                wallet_amount = spl_balances.get(token_symbol, 0.0)

                if wallet_amount < pos["amount_sol"] * 0.01:
                    # Wallet holds < 1% of expected — true ghost position
                    logger.warning(
                        f"Position #{pos['id']} ghost: expected {pos['amount_sol']:.4f} "
                        f"{token_symbol}, wallet holds {wallet_amount:.4f}. Marking abandoned."
                    )
                    await telegram.notify_error(
                        f"Ghost position #{pos['id']} {pos['symbol']} abandoned",
                        f"Expected {pos['amount_sol']:.4f} {token_symbol} in wallet but found "
                        f"{wallet_amount:.4f}. Position closed as unrecoverable.",
                    )
                    close_position(
                        position_id=pos["id"],
                        exit_price=current_price,
                        exit_tx="",
                        status="abandoned",
                        pnl_usdc=0.0,
                        pnl_percent=0.0,
                    )
                    await telegram.close()
                    await wallet.close()
                    if own_jupiter:
                        await jupiter.close()
                    return
                elif wallet_amount < pos["amount_sol"]:
                    # Wallet holds slightly less than recorded (fees, rounding).
                    # Use actual balance so Jupiter doesn't fail on insufficient funds.
                    logger.info(
                        f"Position #{pos['id']}: using wallet balance {wallet_amount:.6f} "
                        f"{token_symbol} (recorded: {pos['amount_sol']:.6f}, "
                        f"diff: {pos['amount_sol'] - wallet_amount:.6f})"
                    )
                    swap_amount = wallet_amount
            except Exception as e:
                logger.warning(f"Pre-flight balance check failed for #{pos['id']}: {e}")

            amount_lamports = int(swap_amount * (10 ** decimals))

            swap_result = None
            for attempt in range(self.max_retries):
                try:
                    swap_result = await jupiter.execute_swap(
                        keypair=wallet.get_keypair(),
                        input_mint=input_mint,
                        output_mint=output_mint,
                        amount_lamports=amount_lamports,
                        slippage_bps=300,  # 3% — higher tolerance for closes
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
            status_map = {
                "tp": "closed_tp",
                "sl": "closed_sl",
                "trail_sl": "closed_trail",
                "manual": "closed_manual",
            }
            status = status_map.get(trigger, "closed_manual")

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
            swap_fee_sol = 0.000055
            log_wallet_tx(
                tx_type="jupiter_swap",
                direction="sell",
                amount=pos["amount_sol"],
                token=token_symbol,
                fee_sol=swap_fee_sol,
                tx_signature=swap_result["tx_signature"],
                status="success",
                notes=f"Position #{pos['id']} {trigger.upper()} auto-close",
            )

            # Send Telegram notification
            trigger_labels = {
                "tp": ("Take Profit", "\U0001f3af"),
                "sl": ("Stop Loss", "\U0001f6d1"),
                "trail_sl": ("Trailing Stop", "\U0001f4c9"),
                "manual": ("Manual Close", "\u2705"),
            }
            label, emoji = trigger_labels.get(trigger, ("Close", "\u2705"))
            pnl_emoji = "\U0001f4c8" if pnl_usdc >= 0 else "\U0001f4c9"

            trail_info = ""
            if trigger == "trail_sl" and pos.get("trail_sl_price"):
                trail_info = f"Trail SL: ${pos['trail_sl_price']:.2f}\n"

            msg = (
                f"<b>{emoji} Position Closed — {label}</b>\n\n"
                f"Symbol: {pos['symbol']}\n"
                f"Entry: ${pos['entry_price']:.2f}\n"
                f"Exit: ${current_price:.2f}\n"
                f"{trail_info}"
                f"Amount: {pos['amount_sol']:.4f} {token_symbol} (${pos.get('amount_usdc', pos['amount_sol'] * pos['entry_price']):.2f})\n"
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
