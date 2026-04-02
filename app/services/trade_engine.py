"""Trade engine - orchestrates signal processing, Claude decision, and execution."""

import asyncio
import hashlib
import logging
import time
from datetime import datetime
from typing import Optional

from app.config import get
from app.database import insert_trade, get_stats, log_signal, insert_position, get_position_count
from app.models import WebhookSignal, ClaudeDecision
from app.services.claude_decision import get_claude_decision
from app.services.jupiter_client import JupiterClient
from app.services.news_service import NewsService
from app.services.kamino_client import KaminoClient
from app.services.paper_trading import get_paper_trader
from app.services.telegram_service import TelegramService
from app.services.wallet_service import WalletService

logger = logging.getLogger("bot.engine")

# Duplicate signal detection
_recent_signals: dict[str, float] = {}


# =============================================================================
# SIGNAL PRIORITY QUEUE
# =============================================================================

class SignalQueue:
    """Batches simultaneous signals and processes highest confidence first.

    When multiple alerts fire on the same candle close (e.g., 3 tokens on 4H),
    they arrive within seconds. This queue collects them in a short window,
    then processes in confidence-descending order, checking budget before each.

    The webhook returns immediately (fire-and-forget) so TradingView doesn't
    timeout. Processing results are sent via Telegram notifications.
    """

    def __init__(self):
        self._queue: list[tuple[WebhookSignal, str]] = []  # (signal, source_ip)
        self._lock = asyncio.Lock()
        self._drain_task: Optional[asyncio.Task] = None

    async def enqueue(self, signal: WebhookSignal, source_ip: str):
        """Add signal to queue. Processing happens after batch window expires."""
        queue_cfg = get("signal_queue") or {}
        enabled = queue_cfg.get("enabled", True)
        batch_window = queue_cfg.get("batch_window_seconds", 5)

        if not enabled:
            # Queue disabled — process immediately (legacy behavior)
            engine = TradeEngine()
            try:
                await engine.process_signal(signal, source_ip)
            finally:
                await engine.shutdown()
            return

        async with self._lock:
            self._queue.append((signal, source_ip))

            # Start drain timer if not already running
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(self._drain_after(batch_window))

    async def _drain_after(self, delay: float):
        """Wait for batch window then process all queued signals."""
        await asyncio.sleep(delay)

        async with self._lock:
            batch = list(self._queue)
            self._queue.clear()

        if not batch:
            return

        # Sort by confidence (highest first)
        batch.sort(key=lambda x: x[0].confidence_score, reverse=True)

        logger.info(
            f"Signal queue draining {len(batch)} signals: "
            + ", ".join(f"{s.symbol}({s.confidence_score})" for s, _ in batch)
        )

        # Notify if multiple signals batched
        if len(batch) > 1:
            engine = TradeEngine()
            order_str = " > ".join(
                f"{s.symbol} {s.timeframe or '?'} (conf:{s.confidence_score})"
                for s, _ in batch
            )
            await engine.telegram.send_message(
                f"Signal Queue: {len(batch)} simultaneous signals\n"
                f"Priority order: {order_str}"
            )
            await engine.shutdown()

        # Process in priority order
        for signal, source_ip in batch:
            engine = TradeEngine()
            try:
                # Check position limit before processing
                risk_cfg = get("risk")
                max_positions = risk_cfg.get("max_open_positions", 3)
                open_count = get_position_count("open")
                if open_count >= max_positions and signal.signal_type.value == "BUY":
                    logger.warning(
                        f"Skipping {signal.symbol} (conf:{signal.confidence_score}) — "
                        f"max positions reached after higher-priority trades"
                    )
                    await engine.telegram.send_message(
                        f"Signal Queue: SKIPPED {signal.symbol} {signal.timeframe or ''} "
                        f"(conf:{signal.confidence_score}) — max positions reached"
                    )
                else:
                    await engine.process_signal(signal, source_ip)
            except Exception as e:
                logger.error(f"Signal queue processing error for {signal.symbol}: {e}")
            finally:
                await engine.shutdown()


# Singleton queue
_signal_queue: Optional[SignalQueue] = None


def get_signal_queue() -> SignalQueue:
    global _signal_queue
    if _signal_queue is None:
        _signal_queue = SignalQueue()
    return _signal_queue


class TradeEngine:
    """Main trade orchestration engine."""

    def __init__(self):
        self.jupiter = JupiterClient()
        self.wallet = WalletService()
        self.telegram = TelegramService()
        self.news = NewsService()
        self.kamino = KaminoClient()
        self._running = True

    def is_duplicate(self, signal: WebhookSignal) -> bool:
        """Check if this signal is a duplicate within the configured window."""
        window = get("webhook", "duplicate_window_seconds", 30)
        sig_hash = hashlib.sha256(
            f"{signal.signal_type}:{signal.symbol}:{signal.entry_price_estimate}".encode()
        ).hexdigest()[:16]

        now = time.time()
        # Clean old entries
        expired = [k for k, v in _recent_signals.items() if now - v > window]
        for k in expired:
            del _recent_signals[k]

        if sig_hash in _recent_signals:
            logger.warning(f"Duplicate signal detected: {sig_hash}")
            return True

        _recent_signals[sig_hash] = now
        return False

    async def process_signal(self, signal: WebhookSignal, source_ip: str = "") -> dict:
        """Full signal processing pipeline."""
        result = {
            "status": "pending",
            "signal_type": signal.signal_type.value,
            "decision": None,
            "tx_signature": None,
            "error": None,
        }

        try:
            # 1. Log raw signal
            log_signal(signal.model_dump_json(), source_ip)

            # 2. Check duplicate
            if self.is_duplicate(signal):
                result["status"] = "duplicate"
                result["error"] = "Duplicate signal ignored"
                return result

            # 3. Notify Telegram
            await self.telegram.notify_webhook_received(signal.model_dump())

            # 4. Gather context for Claude
            sol_price = await self.jupiter.get_sol_price()
            balance_sol = await self.wallet.get_balance_sol()
            usdc_balance = await self.wallet.get_usdc_balance()
            market_data = await self.jupiter.get_market_data()
            headlines = await self.news.get_headlines()

            # Check Kamino deposits (available purchasing power)
            kamino_usdc = 0.0
            if self.kamino.enabled:
                try:
                    kamino_pos = await self.kamino.get_user_position(self.wallet.public_key)
                    if kamino_pos.get("has_position"):
                        kamino_usdc = kamino_pos.get("deposited_usdc", 0.0)
                except Exception as e:
                    logger.warning(f"Could not check Kamino balance: {e}")

            # Total purchasing power = USDC in wallet + Kamino deposits + SOL value
            sol_usd_value = balance_sol * sol_price
            total_usd = usdc_balance + kamino_usdc + sol_usd_value
            # Tradeable USDC = wallet USDC + Kamino (SOL reserved for gas)
            tradeable_usd = usdc_balance + kamino_usdc

            # Get actual token price (use Jupiter price lookup, fall back to signal estimate)
            token_symbol = signal.symbol.replace("USDT", "").replace("USD", "")
            if token_symbol == "SOL":
                token_price = sol_price
            else:
                try:
                    token_price = await self.jupiter.get_token_price(token_symbol)
                except Exception:
                    token_price = signal.entry_price_estimate

            # 5. Check low balance shutdown (based on total USD, not just SOL)
            risk_cfg = get("risk")
            low_bal_usd = risk_cfg.get("low_balance_shutdown_usd", 50.0)
            if total_usd < low_bal_usd:
                result["status"] = "shutdown"
                result["error"] = (
                    f"Total balance too low: ${total_usd:.2f} "
                    f"(SOL: ${sol_usd_value:.2f}, USDC: ${usdc_balance:.2f}, Kamino: ${kamino_usdc:.2f})"
                )
                await self.telegram.notify_error(result["error"], "Low balance shutdown triggered")
                insert_trade({
                    "signal_type": signal.signal_type.value,
                    "symbol": signal.symbol,
                    "action": "REJECT",
                    "amount_sol": 0,
                    "price_usd": token_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": "Low balance shutdown",
                    "wallet_address": self.wallet.public_key,
                    "notes": f"Automatic shutdown - total ${total_usd:.2f} below ${low_bal_usd:.2f} threshold",
                })
                return result

            # 6. Build risk params
            db_stats = get_stats()
            risk_params = {
                "max_purchase_sol": risk_cfg.get("max_purchase_sol", 5.0),
                "max_purchase_usd": risk_cfg.get("max_purchase_usd", 500.0),
                "max_leverage": risk_cfg.get("max_leverage", 5),
                "daily_loss_limit_percent": risk_cfg.get("daily_loss_limit_percent", 10.0),
                "low_balance_shutdown_usd": low_bal_usd,
                "today_pnl_usd": db_stats.get("today_pnl_usd", 0),
                "geo_risk_weight": get("geo_risk", "weight", 0.7),
                # Full balance breakdown for Claude
                "usdc_wallet": usdc_balance,
                "usdc_kamino": kamino_usdc,
                "tradeable_usd": tradeable_usd,
                "total_usd": total_usd,
            }

            # 7. Get Claude decision
            logger.info(
                f"Requesting Claude decision for {signal.signal_type.value} {signal.symbol} "
                f"(tradeable: ${tradeable_usd:.2f}, total: ${total_usd:.2f})"
            )
            claude_resp = await get_claude_decision(
                signal=signal,
                wallet_balance_sol=balance_sol,
                wallet_balance_usd=total_usd,
                sol_price=sol_price,
                market_data=market_data,
                news_headlines=headlines,
                risk_params=risk_params,
            )

            result["decision"] = claude_resp.model_dump()
            await self.telegram.notify_claude_decision(result["decision"], signal.signal_type.value)

            # 8. Execute based on decision
            if claude_resp.decision == ClaudeDecision.REJECT:
                result["status"] = "rejected"
                insert_trade({
                    "signal_type": signal.signal_type.value,
                    "symbol": signal.symbol,
                    "action": "REJECT",
                    "amount_sol": 0,
                    "price_usd": token_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": claude_resp.reasoning,
                    "wallet_address": self.wallet.public_key,
                    "notes": f"Risk score: {claude_resp.risk_score}",
                })
                return result

            # Determine trade parameters
            size_pct = (
                claude_resp.modified_size_percent
                if claude_resp.decision == ClaudeDecision.MODIFY and claude_resp.modified_size_percent
                else signal.suggested_position_size_percent
            )
            leverage = (
                claude_resp.modified_leverage
                if claude_resp.decision == ClaudeDecision.MODIFY and claude_resp.modified_leverage
                else signal.suggested_leverage
            )

            # Apply risk caps
            leverage = min(leverage, risk_cfg.get("max_leverage", 5))
            size_pct = min(size_pct, risk_cfg.get("max_position_size_percent", 15))

            # Calculate trade amount in USD (base currency is USDC, not SOL)
            trade_usd = tradeable_usd * (size_pct / 100)
            max_purchase_usd = risk_cfg.get("max_purchase_usd", 500.0)
            trade_usd = min(trade_usd, max_purchase_usd)
            # Convert to SOL equivalent for logging/compatibility
            trade_sol = trade_usd / sol_price if sol_price > 0 else 0

            # ── PAPER TRADING MODE ──
            paper_trader = get_paper_trader()
            if paper_trader.enabled:
                if signal.signal_type.value == "CLOSE":
                    paper_result = paper_trader.close_paper_position(
                        signal.symbol, token_price, reason="signal"
                    )
                else:
                    paper_result = paper_trader.execute_paper_trade(
                        signal=signal,
                        decision=result["decision"],
                        token_price=token_price,
                        trade_amount_usd=trade_usd,
                    )

                result["status"] = paper_result.get("status", "paper_executed")
                result["paper"] = paper_result

                # Telegram notification with [PAPER] prefix
                await self.telegram.send_message(
                    f"[PAPER] {signal.signal_type.value} {signal.symbol}\n"
                    f"Amount: ${trade_usd:.2f}\n"
                    f"Price: ${token_price:.4f}\n"
                    f"P&L: ${paper_result.get('pnl_usd', 0):.2f}\n"
                    f"Balance: ${paper_result.get('balance_after', 0):.2f}"
                )

                insert_trade({
                    "signal_type": signal.signal_type.value,
                    "symbol": signal.symbol,
                    "action": "PAPER",
                    "amount_sol": trade_sol,
                    "price_usd": token_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": claude_resp.reasoning,
                    "wallet_address": "paper",
                    "notes": f"Paper trade: {paper_result.get('status', '')}",
                })
                return result

            if signal.signal_type.value == "CLOSE":
                # For CLOSE signals, we'd close existing position
                # Simplified: swap back to SOL/USDC
                logger.info("CLOSE signal - would close existing position")
                result["status"] = "closed"
                insert_trade({
                    "signal_type": "CLOSE",
                    "symbol": signal.symbol,
                    "action": "EXECUTE",
                    "amount_sol": trade_sol,
                    "price_usd": token_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": claude_resp.reasoning,
                    "wallet_address": self.wallet.public_key,
                    "leverage": 1,
                })
                return result

            # Withdraw from Kamino if needed (auto-withdraw before trade)
            if self.kamino.enabled and self.kamino.auto_withdraw and kamino_usdc > 0:
                try:
                    logger.info(f"Withdrawing {kamino_usdc:.2f} USDC from Kamino before trade")
                    withdraw_result = await self.kamino.withdraw_all(self.wallet.get_keypair())
                    if withdraw_result.get("success"):
                        await self.telegram.send_message(
                            f"Kamino Withdraw: {kamino_usdc:.2f} USDC withdrawn for trade execution"
                        )
                        await asyncio.sleep(2)  # Wait for tx to finalize
                        self.wallet.invalidate_cache()
                    else:
                        logger.warning(f"Kamino withdraw failed: {withdraw_result.get('error')}")
                except Exception as e:
                    logger.warning(f"Kamino withdraw error (continuing with available balance): {e}")

            # Resolve target token mint address
            target_mint = self._resolve_token_mint(token_symbol)

            # Execute swap via Jupiter
            if signal.signal_type.value == "BUY":
                # BUY: swap USDC → target token
                input_mint = self.jupiter.usdc_mint
                output_mint = target_mint
                amount_lamports = int(trade_usd * 1_000_000)  # USDC has 6 decimals
            else:
                # SELL: swap target token → USDC
                input_mint = target_mint
                output_mint = self.jupiter.usdc_mint
                # For sells, amount is in target token's smallest unit
                # We use trade_usd / token_price to get token amount
                token_amount = trade_usd / token_price if token_price > 0 else 0
                decimals = self._token_decimals(token_symbol)
                amount_lamports = int(token_amount * (10 ** decimals))

            logger.info(
                f"Executing {signal.signal_type.value} {token_symbol}: "
                f"${trade_usd:.2f} USDC at {leverage}x "
                f"(swap {input_mint[:8]}..→{output_mint[:8]}..)"
            )

            swap_result = await self.jupiter.execute_swap(
                keypair=self.wallet.get_keypair(),
                input_mint=input_mint,
                output_mint=output_mint,
                amount_lamports=amount_lamports,
            )

            result["status"] = "executed"
            result["tx_signature"] = swap_result["tx_signature"]

            # Invalidate cache so next balance read is fresh
            self.wallet.invalidate_cache()

            # Get new balance
            new_balance = await self.wallet.get_balance_sol()

            # Log trade
            insert_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "tx_id": swap_result["tx_signature"],
                "signal_type": signal.signal_type.value,
                "symbol": signal.symbol,
                "action": "EXECUTE",
                "amount_sol": trade_sol,
                "price_usd": token_price,
                "fees_sol": 0.000005,  # Base tx fee
                "leverage": leverage,
                "confidence_score": signal.confidence_score,
                "claude_reasoning": claude_resp.reasoning,
                "wallet_address": self.wallet.public_key,
                "notes": f"Price impact: {swap_result.get('price_impact', '?')}",
            })

            # Notify Telegram
            await self.telegram.notify_trade_executed(
                tx_sig=swap_result["tx_signature"],
                action=signal.signal_type.value,
                amount_sol=trade_sol,
                price_usd=token_price,
                fees_sol=0.000005,
                new_balance_sol=new_balance,
            )

            # Create position record for TP/SL monitoring (BUY only)
            if signal.signal_type.value == "BUY" and signal.atr and signal.atr > 0:
                pos_cfg = get("position_monitor") or {}
                tp_mult = pos_cfg.get("tp_multiplier", 4.0)
                sl_mult = pos_cfg.get("sl_multiplier", 1.5)
                tp_price = token_price + (signal.atr * tp_mult)
                sl_price = token_price - (signal.atr * sl_mult)

                risk_cfg = get("risk")
                max_positions = risk_cfg.get("max_open_positions", 3)
                open_count = get_position_count("open")

                if open_count < max_positions:
                    pos_id = insert_position({
                        "symbol": signal.symbol,
                        "direction": "long",
                        "entry_price": token_price,
                        "amount_sol": trade_sol,
                        "amount_usdc": trade_usd,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "entry_tx": swap_result["tx_signature"],
                        "timeframe": signal.timeframe or "",
                        "confidence": signal.confidence_score,
                        "atr": signal.atr,
                        "notes": f"TP=${tp_price:.2f} SL=${sl_price:.2f} ATR={signal.atr:.4f}",
                    })
                    logger.info(
                        f"Position #{pos_id} opened: {trade_sol:.4f} {token_symbol} @ ${token_price:.4f}, "
                        f"TP=${tp_price:.4f}, SL=${sl_price:.4f}"
                    )
                else:
                    logger.warning(f"Max open positions ({max_positions}) reached, skipping position tracking")
            elif signal.signal_type.value == "BUY":
                logger.warning("No ATR in signal, cannot set TP/SL for position tracking")

            # Auto-deposit idle USDC back to Kamino after trade
            if self.kamino.enabled and self.kamino.auto_deposit:
                try:
                    import asyncio
                    await asyncio.sleep(3)  # Wait for balances to settle
                    usdc_balance = await self.wallet.get_usdc_balance()
                    deposit_result = await self.kamino.deposit_idle(self.wallet.get_keypair(), usdc_balance)
                    if deposit_result.get("success"):
                        deposited = deposit_result["amount_usdc"]
                        await self.telegram.send_message(
                            f"Kamino Deposit: {deposited:.2f} USDC deposited to earn yield"
                        )
                except Exception as e:
                    logger.warning(f"Kamino auto-deposit after trade failed: {e}")

            return result

        except Exception as e:
            logger.exception(f"Trade engine error: {e}")
            result["status"] = "error"
            result["error"] = str(e)
            await self.telegram.notify_error(str(e), f"Signal: {signal.signal_type.value} {signal.symbol}")
            return result

    # Well-known Solana token mints
    _KNOWN_MINTS = {
        "SOL": "So11111111111111111111111111111111111111112",
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
        "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
        "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
        "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    }

    _TOKEN_DECIMALS = {
        "SOL": 9, "USDC": 6, "JTO": 9, "WIF": 6,
        "BONK": 5, "PYTH": 6, "RAY": 6, "ETH": 8, "ORCA": 6,
    }

    def _resolve_token_mint(self, symbol: str) -> str:
        """Resolve token symbol to Solana mint address."""
        # Check well-known mints first, then rebalancer config
        if symbol in self._KNOWN_MINTS:
            return self._KNOWN_MINTS[symbol]
        rebal_cfg = get("rebalancer") or {}
        token_mints = rebal_cfg.get("token_mints", {})
        if symbol in token_mints:
            return token_mints[symbol]
        # Fallback to SOL mint if unknown
        logger.warning(f"Unknown token mint for {symbol}, falling back to SOL")
        return self._KNOWN_MINTS["SOL"]

    def _token_decimals(self, symbol: str) -> int:
        """Get decimal places for a token (for lamport conversion)."""
        return self._TOKEN_DECIMALS.get(symbol, 9)

    async def shutdown(self):
        self._running = False
        await self.jupiter.close()
        await self.wallet.close()
        await self.telegram.close()
        await self.news.close()
        await self.kamino.close()
