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
    insert_reentry_watch,
    get_active_reentry_watches,
    update_reentry_watch,
    expire_stale_reentry_watches,
    TradeReason,
)

TRIGGER_TO_REASON = {
    "tp": TradeReason.TP_HIT,
    "sl": TradeReason.SL_HIT,
    "trail_sl": TradeReason.TRAIL_SL,
    "signal": TradeReason.SIGNAL_CLOSE,
    "manual": TradeReason.MANUAL_CLOSE,
}

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

        # Max-hold rule (added 2026-05-03 after position #74 sat 14 days un-closed
        # because EMA Ribbon never produced an exit signal and SL was just barely
        # missed). After max_hold_days, force-close regardless of trigger so
        # capital doesn't sit indefinitely. Set to 0 to disable.
        self.max_hold_days = float(cfg.get("max_hold_days", 7))

        # Trailing stop config
        trail_cfg = cfg.get("trailing_stop") or {}
        self.trail_enabled = trail_cfg.get("enabled", False)
        self.trail_activation_atr = trail_cfg.get("activation_atr", 1.5)
        self.trail_offset_atr = trail_cfg.get("offset_atr", 1.0)

        # Re-entry watch config: after SL, set a buy target at 1.0x SL-distance
        # below exit. If price reaches it within expiry_hours, fire a new BUY.
        reentry_cfg = cfg.get("reentry") or {}
        self.reentry_enabled = reentry_cfg.get("enabled", False)
        self.reentry_sl_distance_mult = reentry_cfg.get("sl_distance_mult", 1.0)
        self.reentry_expiry_hours = reentry_cfg.get("expiry_hours", 24)

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
                if self.reentry_enabled:
                    await self._check_reentry_watches()
            except Exception as e:
                logger.error(f"Position monitor error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _check_positions(self):
        """Check all open positions against current price per token."""
        positions = get_open_positions()
        if not positions:
            return

        from app.services.jupiter_client import JupiterClient
        from app.services.price_router import get_monitor_price

        # JupiterClient kept for the close path (_close_position passes it down).
        # Price lookup itself goes through the chain-aware router.
        jupiter = JupiterClient()

        for pos in positions:
            # Resolve the token symbol from the position (e.g. WIFUSDT → WIF, FARTCOINUSDT.P → FARTCOIN)
            token_symbol = pos.get("symbol", "SOLUSDT").replace("USDT", "").replace("USD", "").replace(".P", "")

            # Chain-aware price routing (per CLAUDE.md hard rule #8): the source
            # MUST match the execution venue. Solana → Jupiter, EVM → OpenOcean,
            # Binance.US → Binance.US WS/REST. CoinGecko is last-resort fallback.
            # Avoids the 2026-05-06 zombie-listing fake-SL incident class.
            current_price = await get_monitor_price(token_symbol)
            if current_price is None or current_price <= 0:
                logger.warning(
                    f"Price fetch failed for {token_symbol} on all sources, "
                    f"skipping position #{pos.get('id')} (TP/SL won't fire this cycle)"
                )
                continue

            # Sanity gate: reject prices wildly off from entry. Lesson 2026-05-11
            # PENGU #177: Jupiter returned outAmount corresponding to $39.75 for
            # 1 PENGU (entry $0.0103) — 3800× off — fake-fired TP and we closed
            # for -0.5%. Real market moves rarely exceed 10× entry intraday;
            # anything bigger is almost certainly a feed glitch. Skip and retry
            # next cycle. False-negative cost: one cycle of unmonitored TP/SL.
            # False-positive cost: fake-fired exit at the actual swap price.
            entry = pos.get("entry_price") or 0
            if entry > 0 and (current_price > entry * 10 or current_price < entry * 0.1):
                logger.warning(
                    f"Position #{pos['id']} {token_symbol}: rejected price "
                    f"${current_price:.6f} as out-of-band vs entry ${entry:.6f} "
                    f"(ratio {current_price/entry:.2f}x). Skipping this cycle — "
                    f"feed glitch suspected."
                )
                continue

            logger.debug(
                f"Position #{pos['id']} {token_symbol} @ ${current_price:.6f} "
                f"(TP=${pos['tp_price']:.6f} SL=${pos['sl_price']:.6f})"
            )

            trigger = None
            effective_sl = pos["sl_price"]

            # Max-hold check: force-close if position older than configured days
            if self.max_hold_days > 0 and pos.get("created_at"):
                from datetime import datetime as _dt
                try:
                    created = _dt.fromisoformat(pos["created_at"].replace("Z", ""))
                    age_days = (_dt.utcnow() - created).total_seconds() / 86400
                    if age_days >= self.max_hold_days:
                        logger.info(
                            f"Position #{pos['id']} max-hold trigger: age "
                            f"{age_days:.1f}d >= {self.max_hold_days}d threshold"
                        )
                        trigger = "manual"  # closed_manual status, not SL/TP
                except Exception:
                    pass

            if not trigger and pos["direction"] == "long":
                # Update trailing stop if enabled and ATR is available
                if self.trail_enabled and pos.get("atr") and pos["atr"] > 0:
                    effective_sl = self._update_trailing_sl(pos, current_price)

                if current_price >= pos["tp_price"]:
                    trigger = "tp"
                elif current_price <= effective_sl:
                    trigger = "trail_sl" if effective_sl > pos["sl_price"] else "sl"
            elif not trigger:  # short (future use)
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
        """Execute a position close via Jupiter (Solana) or OpenOcean (EVM).

        Chain is inferred from the position symbol — if it's in
        TradeEngine.EVM_TOKENS we route to the EVM executor; otherwise the
        original Jupiter Solana path runs unchanged.
        """
        from app.services.jupiter_client import JupiterClient
        from app.services.wallet_service import WalletService
        from app.services.telegram_service import TelegramService
        from app.services.kamino_client import KaminoClient
        from app.services.trade_engine import TradeEngine

        # ── Lane routing ────────────────────────────────────────────────────
        token_symbol_norm = (pos.get("symbol", "") or "").upper()\
            .replace("USDT", "").replace("USD", "").replace(".P", "")
        # EVM takes precedence over Binance (matches trade_engine._is_binance_symbol)
        if token_symbol_norm in TradeEngine.EVM_TOKENS:
            await self._close_position_evm(pos, current_price, trigger, token_symbol_norm)
            return
        if token_symbol_norm in TradeEngine.BINANCE_TOKENS:
            await self._close_position_binance(pos, current_price, trigger, token_symbol_norm)
            return

        own_jupiter = jupiter is None
        if own_jupiter:
            jupiter = JupiterClient()

        wallet = WalletService()
        telegram = TelegramService()

        try:
            # For long positions: sell target token for USDC
            token_symbol = pos.get("symbol", "SOLUSDT").replace("USDT", "").replace("USD", "").replace(".P", "")
            from app.services.trade_engine import TradeEngine
            input_mint = TradeEngine._KNOWN_MINTS.get(token_symbol, jupiter.sol_mint)
            output_mint = jupiter.usdc_mint
            decimals = TradeEngine._TOKEN_DECIMALS.get(token_symbol, 9)

            # Pre-flight balance check: verify wallet holds sufficient tokens.
            # Uses actual wallet balance for the swap to handle small discrepancies
            # (fees, rounding) between the recorded position amount and real balance.
            # SOL is the native Solana token, not an SPL token — querying SPL balances
            # for "SOL" always returns 0 and falsely flags every SOL position as ghost.
            # That bug abandoned 40+ legitimate SOL positions; now special-cased.
            swap_amount = pos["amount_sol"]
            try:
                if token_symbol == "SOL":
                    wallet_amount = await wallet.get_balance_sol()
                else:
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

            # Calculate exit price + P&L from the ACTUAL swap output, not the
            # monitor's trigger price. The monitor price comes from the oracle
            # which can drift (e.g., Binance.US zombie listing returned $0.10
            # for JUP when real spot was $0.19, fake-firing 4 SLs and recording
            # phantom losses). The swap_result["output_amount"] is the literal
            # USDC the bot received — that's the only honest exit basis.
            usdc_received = swap_result.get("output_amount", 0) / 1e6  # USDC has 6 decimals
            if usdc_received > 0 and pos["amount_sol"] > 0:
                actual_exit_price = usdc_received / pos["amount_sol"]
            else:
                # Fall back to current_price if swap_result missing (older swap clients)
                actual_exit_price = current_price
                logger.warning(
                    f"Position #{pos['id']}: swap_result missing output_amount; "
                    f"falling back to monitor price ${current_price:.6f}"
                )
            pnl_usdc = (actual_exit_price - pos["entry_price"]) * pos["amount_sol"]
            pnl_percent = (
                (actual_exit_price - pos["entry_price"]) / pos["entry_price"]
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
                exit_price=actual_exit_price,
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
                "price_usd": actual_exit_price,
                "fees_sol": 0.000005,
                "leverage": 1,
                "wallet_address": wallet.public_key,
                "confidence_score": pos["confidence"],
                "claude_reasoning": f"Auto-close: {trigger.upper()} hit (trigger price ${current_price:.6f}, fill ${actual_exit_price:.6f})",
                "pnl_usd": pnl_usdc,
                "notes": f"Position #{pos['id']} {status} | "
                         f"Entry=${pos['entry_price']:.2f} Exit=${actual_exit_price:.2f}",
                "strategy": pos.get("strategy", ""),
                "reason": TRIGGER_TO_REASON.get(trigger, TradeReason.MANUAL_CLOSE),
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

            if self.reentry_enabled and trigger == "sl":
                self._create_reentry_watch(pos, actual_exit_price)

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

    def _create_reentry_watch(self, pos: dict, exit_price: float):
        """Create a re-entry watch after an SL close.

        Target price = exit_price - (sl_distance_mult × SL distance).
        SL distance = entry_price - sl_price (the original stop range).
        """
        from datetime import timedelta

        entry = pos.get("entry_price", 0)
        sl = pos.get("sl_price", 0)
        sl_distance = abs(entry - sl)
        if sl_distance <= 0:
            return

        target_price = exit_price - (self.reentry_sl_distance_mult * sl_distance)
        if target_price <= 0:
            return

        expires = datetime.utcnow() + timedelta(hours=self.reentry_expiry_hours)

        watch_id = insert_reentry_watch({
            "symbol": pos["symbol"],
            "strategy": pos.get("strategy", ""),
            "timeframe": pos.get("timeframe", ""),
            "sl_exit_price": exit_price,
            "target_price": target_price,
            "sl_distance": sl_distance,
            "atr": pos.get("atr"),
            "sizing_pct": None,
            "expires_at": expires.isoformat(),
            "source_position_id": pos["id"],
            "notes": (
                f"Re-entry: SL exit ${exit_price:.6f} - "
                f"{self.reentry_sl_distance_mult}x SL-dist ${sl_distance:.6f} "
                f"= target ${target_price:.6f}"
            ),
        })
        logger.info(
            f"Re-entry watch #{watch_id} created for {pos['symbol']}: "
            f"target ${target_price:.6f} (SL exit ${exit_price:.6f} "
            f"- {self.reentry_sl_distance_mult}x ${sl_distance:.6f}), "
            f"expires {expires.strftime('%Y-%m-%d %H:%M')}"
        )

    async def _check_reentry_watches(self):
        """Check active re-entry watches and fire BUY signals when triggered."""
        expired = expire_stale_reentry_watches()
        if expired:
            logger.info(f"Expired {expired} stale re-entry watch(es)")

        watches = get_active_reentry_watches()
        if not watches:
            return

        from app.services.price_router import get_monitor_price

        for watch in watches:
            token_symbol = (watch["symbol"] or "").upper()\
                .replace("USDT", "").replace("USD", "").replace(".P", "")

            current_price = await get_monitor_price(token_symbol)
            if current_price is None or current_price <= 0:
                continue

            if current_price <= watch["target_price"]:
                logger.info(
                    f"Re-entry watch #{watch['id']} TRIGGERED: {watch['symbol']} "
                    f"${current_price:.6f} <= target ${watch['target_price']:.6f}"
                )
                await self._fire_reentry(watch, current_price)

    async def _fire_reentry(self, watch: dict, current_price: float):
        """Fire a synthetic BUY signal for a triggered re-entry watch."""
        from app.models import WebhookSignal, SignalType
        from app.services.trade_engine import TradeEngine
        from app.services.telegram_service import TelegramService
        from app.config import get as cfg_get

        telegram = TelegramService()
        try:
            secret = cfg_get("webhook_secret")
            signal = WebhookSignal(
                secret=secret,
                signal_type=SignalType.BUY,
                symbol=watch["symbol"],
                entry_price_estimate=current_price,
                confidence_score=65,
                suggested_leverage=1,
                suggested_position_size_percent=watch.get("sizing_pct") or 9.0,
                atr=watch.get("atr"),
                timeframe=watch.get("timeframe"),
                strategy=watch.get("strategy"),
            )

            engine = TradeEngine()
            try:
                await engine.process_signal(signal, source_ip="reentry_watch")
                update_reentry_watch(watch["id"], "triggered")
                await telegram.send_message(
                    f"<b>🔄 Re-entry BUY triggered</b>\n\n"
                    f"Symbol: {watch['symbol']}\n"
                    f"Price: ${current_price:.6f}\n"
                    f"Original SL exit: ${watch['sl_exit_price']:.6f}\n"
                    f"Dip: {(watch['sl_exit_price'] - current_price) / watch['sl_exit_price'] * 100:.1f}%\n"
                    f"Watch #{watch['id']} (source position #{watch['source_position_id']})"
                )
            except Exception as e:
                logger.error(f"Re-entry watch #{watch['id']} fire failed: {e}")
                update_reentry_watch(watch["id"], "failed")
                await telegram.send_message(
                    f"❌ Re-entry BUY failed for {watch['symbol']}: {str(e)[:100]}"
                )
            finally:
                await engine.shutdown()
        finally:
            await telegram.close()

    async def _close_position_evm(
        self, pos: dict, current_price: float, trigger: str, token_symbol: str,
    ):
        """Close an EVM position via OpenOcean on Arbitrum.

        Mirrors `_close_position` (Solana/Jupiter) but uses EVMSwapExecutor.
        Pre-flight balance check uses on-chain ERC20 balanceOf instead of
        Solana SPL token accounts.
        """
        from app.services.evm_wallet_service import EVMWalletService
        from app.services.evm_swap_executor import EVMSwapExecutor
        from app.services.telegram_service import TelegramService
        from app.services.trade_engine import TradeEngine

        wallet = EVMWalletService()
        executor = EVMSwapExecutor(wallet)
        telegram = TelegramService()

        try:
            # Resolve EVM contract + decimals
            evm_token_addr, evm_token_decimals = TradeEngine.EVM_TOKENS[token_symbol]

            # Pre-flight balance check via on-chain ERC20 balanceOf
            on_chain_amt = await wallet.get_erc20_balance(evm_token_addr, decimals=evm_token_decimals)
            recorded_amt = float(pos.get("amount_sol") or 0)

            if recorded_amt > 0 and on_chain_amt < recorded_amt * 0.01:
                # Ghost EVM position — wallet has < 1% of recorded
                logger.warning(
                    f"EVM Position #{pos['id']} ghost: expected {recorded_amt:.6f} {token_symbol}, "
                    f"wallet holds {on_chain_amt:.6f}. Marking abandoned."
                )
                await telegram.notify_error(
                    f"Ghost EVM position #{pos['id']} {pos['symbol']} abandoned",
                    f"Expected {recorded_amt:.6f} {token_symbol} on Arbitrum but found "
                    f"{on_chain_amt:.6f}. Position closed as unrecoverable.",
                )
                close_position(
                    position_id=pos["id"], exit_price=current_price, exit_tx="",
                    status="abandoned", pnl_usdc=0.0, pnl_percent=0.0,
                )
                return

            # Use min(recorded, on_chain) so we don't try to sell more than we hold
            sell_amount = min(recorded_amt, on_chain_amt) if recorded_amt > 0 else on_chain_amt
            sell_wei = int(sell_amount * (10 ** evm_token_decimals))

            # Execute SELL: token → USDC
            try:
                evm_result = await executor.execute_swap(
                    src_token=evm_token_addr, src_decimals=evm_token_decimals,
                    dst_token=TradeEngine.EVM_USDC_CONTRACT,
                    dst_decimals=TradeEngine.EVM_USDC_DECIMALS,
                    amount_wei=sell_wei,
                    slippage_bps=300,  # 3% — higher tolerance for forced closes
                    dry_run=False,
                    wait_for_swap_receipt=True,
                    receipt_timeout_s=120,
                )
            except Exception as e:
                logger.error(f"Failed to close EVM position #{pos['id']}: {e}")
                await telegram.notify_error(
                    f"Failed to close EVM position #{pos['id']}: {e}",
                    f"Entry=${pos['entry_price']:.4f}, "
                    f"Trigger={trigger.upper()} @ ${current_price:.4f}",
                )
                return

            receipt = evm_result.get("receipt") or {}
            if int(receipt.get("status", "0x0"), 16) != 1:
                logger.error(f"EVM close tx reverted on-chain for #{pos['id']}")
                await telegram.notify_error(
                    f"EVM close reverted on-chain (#{pos['id']})",
                    f"tx={evm_result['swap_tx']['hash']}",
                )
                return

            tx_hash = evm_result["swap_tx"]["hash"]

            # Calculate P&L
            pnl_usdc = (current_price - pos["entry_price"]) * sell_amount
            pnl_percent = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100

            status_map = {
                "tp": "closed_tp", "sl": "closed_sl",
                "trail_sl": "closed_trail", "manual": "closed_manual",
            }
            status = status_map.get(trigger, "closed_manual")

            close_position(
                position_id=pos["id"], exit_price=current_price, exit_tx=tx_hash,
                status=status, pnl_usdc=pnl_usdc, pnl_percent=pnl_percent,
            )

            insert_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "tx_id": tx_hash,
                "signal_type": "SELL",
                "symbol": pos["symbol"],
                "action": "EXECUTE",
                "amount_sol": sell_amount,
                "price_usd": current_price,
                "fees_sol": 0.0,
                "leverage": 1,
                "wallet_address": wallet.address,
                "confidence_score": pos.get("confidence", 0),
                "claude_reasoning": f"Auto-close (EVM): {trigger.upper()} hit",
                "pnl_usd": pnl_usdc,
                "notes": f"Position #{pos['id']} {status} | EVM | "
                         f"Entry=${pos['entry_price']:.4f} Exit=${current_price:.4f} | chain=arbitrum",
                "strategy": pos.get("strategy", ""),
                "reason": TRIGGER_TO_REASON.get(trigger, TradeReason.MANUAL_CLOSE),
            })

            # Telegram notification
            trigger_labels = {
                "tp": ("Take Profit", "\U0001f3af"),
                "sl": ("Stop Loss", "\U0001f6d1"),
                "trail_sl": ("Trailing Stop", "\U0001f4c9"),
                "manual": ("Manual Close", "✅"),
            }
            label, emoji = trigger_labels.get(trigger, ("Close", "✅"))
            pnl_emoji = "\U0001f4c8" if pnl_usdc >= 0 else "\U0001f4c9"
            await telegram.send_message(
                f"{emoji} <b>{label} (Arbitrum)</b>: {pos['symbol']}\n"
                f"Sold {sell_amount:.6f} @ ${current_price:.4f} for "
                f"{pnl_emoji} ${pnl_usdc:+.2f} ({pnl_percent:+.2f}%)\n"
                f"Tx: <a href=\"https://arbiscan.io/tx/{tx_hash}\">{tx_hash[:10]}...</a>"
            )

            logger.info(
                f"EVM position #{pos['id']} closed via {trigger}: "
                f"P&L=${pnl_usdc:+.2f} ({pnl_percent:+.2f}%)"
            )

            if self.reentry_enabled and trigger == "sl":
                self._create_reentry_watch(pos, current_price)

            # Auto-deposit recovered USDC to Aave V3 (mirrors Kamino's hook
            # in the Solana close path). Wallet just gained ~exit_value USDC;
            # park excess above reserve.
            try:
                from app.services.aave_v3_client import AaveV3Client
                aave = AaveV3Client()
                if aave.enabled and aave.auto_deposit:
                    await asyncio.sleep(3)  # let chain state settle
                    dep = await aave.deposit_idle(dry_run=False)
                    if dep.get("success") and not dep.get("skipped"):
                        amt = dep.get("amount_usdc", 0)
                        await telegram.send_message(
                            f"Aave Deposit: ${amt:.2f} USDC supplied to earn yield"
                        )
            except Exception as e:
                logger.warning(f"Aave auto-deposit after EVM close failed: {e}")

        finally:
            await telegram.close()
            await executor.close()
            await wallet.close()

    async def _close_position_binance(
        self, pos: dict, current_price: float, trigger: str, token_symbol: str
    ):
        """Close a Binance.US CEX position via market sell.

        Pre-flight balance check: read free balance directly from Binance,
        clamp recorded position size to actual on-exchange balance (in case
        of dust loss from prior partial fills). Update DB with realized P&L.
        """
        from app.services.binance_us_client import BinanceUSClient
        from app.services.telegram_service import TelegramService
        from app.services.trade_engine import TradeEngine

        bn_symbol = TradeEngine.BINANCE_TOKENS[token_symbol]
        bn = BinanceUSClient()
        telegram = TelegramService()

        try:
            recorded_amt = float(pos.get("amount_sol", 0) or 0)  # token units (column name is legacy)

            # Pre-flight: actual free balance on Binance
            free_balance = await bn.get_balance(token_symbol)
            if free_balance <= 0:
                logger.warning(
                    f"Position #{pos['id']} ({token_symbol}): no Binance.US balance to sell. Marking abandoned."
                )
                close_position(
                    position_id=pos["id"], exit_price=current_price,
                    exit_tx="", status="closed_abandoned",
                    pnl_usdc=0, pnl_percent=0,
                )
                await telegram.send_message(
                    f"⚠️ Position #{pos['id']} ({token_symbol}) — no Binance.US balance, marked abandoned."
                )
                return

            # Get symbol filters for stepSize quantization
            sym_info = await bn.get_symbol_info(bn_symbol)
            step_size = sym_info.get("step_size", 0) if sym_info.get("success") else 0

            # Sell min(recorded, free) — protect against over-sell
            sell_amount_raw = min(recorded_amt, free_balance) if recorded_amt > 0 else free_balance
            sell_amount = bn.quantize_qty(sell_amount_raw, step_size) if step_size > 0 else sell_amount_raw

            if sell_amount * current_price < (sym_info.get("min_notional", 0) if sym_info.get("success") else 0):
                logger.warning(
                    f"Position #{pos['id']}: sell qty {sell_amount} {token_symbol} below min notional, marking abandoned"
                )
                close_position(
                    position_id=pos["id"], exit_price=current_price,
                    exit_tx="", status="closed_abandoned",
                    pnl_usdc=0, pnl_percent=0,
                )
                return

            order_result = await bn.place_market_sell_base(bn_symbol, sell_amount)
            if not order_result.get("success"):
                logger.error(
                    f"Binance.US sell failed for #{pos['id']}: {order_result.get('error')}"
                )
                await telegram.send_message(
                    f"❌ Binance.US close FAILED for #{pos['id']} ({token_symbol})\n"
                    f"Error: {(order_result.get('error') or 'unknown')[:200]}"
                )
                return

            order = order_result["data"]
            order_id = order.get("orderId")
            exec_qty = float(order.get("executedQty", 0))
            cum_quote = float(order.get("cummulativeQuoteQty", 0))
            avg_fill_price = (cum_quote / exec_qty) if exec_qty > 0 else current_price

            # Compute P&L using actual fill price
            entry = float(pos.get("entry_price", 0) or 0)
            entry_qty = float(pos.get("amount_sol", 0) or 0)
            entry_usdc = entry_qty * entry if entry_qty > 0 else float(pos.get("amount_usdc", 0) or 0)
            pnl_usdc = cum_quote - entry_usdc
            pnl_percent = (pnl_usdc / entry_usdc * 100) if entry_usdc > 0 else 0
            status_map = {
                "tp": "closed_tp", "sl": "closed_sl",
                "trail_sl": "closed_trail", "manual": "closed_manual",
            }
            status = status_map.get(trigger, "closed_manual")

            close_position(
                position_id=pos["id"], exit_price=avg_fill_price,
                exit_tx=f"binance:{order_id}", status=status,
                pnl_usdc=pnl_usdc, pnl_percent=pnl_percent,
            )
            insert_trade({
                "tx_id": f"binance:{order_id}",
                "signal_type": "CLOSE",
                "symbol": pos["symbol"],
                "action": "EXECUTE",
                "amount_sol": exec_qty,
                "amount_usd": cum_quote,
                "price_usd": avg_fill_price,
                "leverage": 1,
                "wallet_address": "binance_us",
                "pnl_usd": pnl_usdc,
                "notes": f"Position #{pos['id']} {status} | Binance.US | "
                         f"Entry=${entry:.4f} Exit=${avg_fill_price:.4f} | chain=binance",
                "strategy": pos.get("strategy", ""),
                "reason": TRIGGER_TO_REASON.get(trigger, TradeReason.MANUAL_CLOSE),
            })

            trigger_labels = {
                "tp": ("Take Profit", "🎯"),
                "sl": ("Stop Loss", "🛑"),
                "trail_sl": ("Trailing Stop", "📉"),
                "manual": ("Manual Close", "✅"),
            }
            label, emoji = trigger_labels.get(trigger, ("Close", "✅"))
            pnl_emoji = "📈" if pnl_usdc >= 0 else "📉"
            await telegram.send_message(
                f"{emoji} <b>{label} (Binance.US)</b>: {pos['symbol']}\n"
                f"Sold {exec_qty:.6f} @ ${avg_fill_price:.4f} for "
                f"{pnl_emoji} ${pnl_usdc:+.2f} ({pnl_percent:+.2f}%)\n"
                f"Order: <code>{order_id}</code>"
            )
            logger.info(
                f"Binance.US position #{pos['id']} closed via {trigger}: "
                f"P&L=${pnl_usdc:+.2f} ({pnl_percent:+.2f}%)"
            )

            if self.reentry_enabled and trigger == "sl":
                self._create_reentry_watch(pos, avg_fill_price)

        finally:
            await telegram.close()
            await bn.close()


# Singleton
_monitor: Optional[PositionMonitor] = None


def get_position_monitor() -> PositionMonitor:
    global _monitor
    if _monitor is None:
        _monitor = PositionMonitor()
    return _monitor
