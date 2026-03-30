"""Trade engine - orchestrates signal processing, Claude decision, and execution."""

import hashlib
import logging
import time
from datetime import datetime

from app.config import get
from app.database import insert_trade, get_stats, log_signal
from app.models import WebhookSignal, ClaudeDecision
from app.services.claude_decision import get_claude_decision
from app.services.jupiter_client import JupiterClient
from app.services.news_service import NewsService
from app.services.kamino_client import KaminoClient
from app.services.telegram_service import TelegramService
from app.services.wallet_service import WalletService

logger = logging.getLogger("bot.engine")

# Duplicate signal detection
_recent_signals: dict[str, float] = {}


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
            balance_usd = balance_sol * sol_price
            market_data = await self.jupiter.get_market_data()
            headlines = await self.news.get_headlines()

            # 5. Check low balance shutdown
            risk_cfg = get("risk")
            if balance_sol < risk_cfg.get("low_balance_shutdown_sol", 0.5):
                result["status"] = "shutdown"
                result["error"] = f"Balance too low: {balance_sol:.4f} SOL"
                await self.telegram.notify_error(result["error"], "Low balance shutdown triggered")
                insert_trade({
                    "signal_type": signal.signal_type.value,
                    "symbol": signal.symbol,
                    "action": "REJECT",
                    "amount_sol": 0,
                    "price_usd": sol_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": "Low balance shutdown",
                    "wallet_address": self.wallet.public_key,
                    "notes": "Automatic shutdown - balance below threshold",
                })
                return result

            # 6. Build risk params
            db_stats = get_stats()
            risk_params = {
                "max_purchase_sol": risk_cfg.get("max_purchase_sol", 5.0),
                "max_purchase_usd": risk_cfg.get("max_purchase_usd", 500.0),
                "max_leverage": risk_cfg.get("max_leverage", 5),
                "daily_loss_limit_percent": risk_cfg.get("daily_loss_limit_percent", 10.0),
                "low_balance_shutdown_sol": risk_cfg.get("low_balance_shutdown_sol", 0.5),
                "today_pnl_usd": db_stats.get("today_pnl_usd", 0),
                "geo_risk_weight": get("geo_risk", "weight", 0.7),
            }

            # 7. Get Claude decision
            logger.info(f"Requesting Claude decision for {signal.signal_type.value} {signal.symbol}")
            claude_resp = await get_claude_decision(
                signal=signal,
                wallet_balance_sol=balance_sol,
                wallet_balance_usd=balance_usd,
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
                    "price_usd": sol_price,
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

            # Calculate trade amount
            trade_sol = balance_sol * (size_pct / 100)
            trade_sol = min(trade_sol, risk_cfg.get("max_purchase_sol", 5.0))
            trade_usd = trade_sol * sol_price
            if trade_usd > risk_cfg.get("max_purchase_usd", 500.0):
                trade_sol = risk_cfg["max_purchase_usd"] / sol_price

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
                    "price_usd": sol_price,
                    "confidence_score": signal.confidence_score,
                    "claude_reasoning": claude_resp.reasoning,
                    "wallet_address": self.wallet.public_key,
                    "leverage": 1,
                })
                return result

            # Withdraw from Kamino if needed (auto-withdraw before trade)
            if self.kamino.enabled and self.kamino.auto_withdraw:
                try:
                    position = await self.kamino.get_user_position(self.wallet.public_key)
                    if position.get("has_position"):
                        logger.info(f"Withdrawing {position['deposited_usdc']:.2f} USDC from Kamino before trade")
                        withdraw_result = await self.kamino.withdraw_all(self.wallet.get_keypair())
                        if withdraw_result.get("success"):
                            await self.telegram.send_message(
                                f"Kamino Withdraw: {position['deposited_usdc']:.2f} USDC withdrawn for trade execution"
                            )
                            # Brief pause for transaction to finalize
                            import asyncio
                            await asyncio.sleep(2)
                        else:
                            logger.warning(f"Kamino withdraw failed: {withdraw_result.get('error')}")
                except Exception as e:
                    logger.warning(f"Kamino withdraw error (continuing with available balance): {e}")

            # Execute swap via Jupiter
            amount_lamports = int(trade_sol * 1_000_000_000)

            if signal.signal_type.value == "BUY":
                # Buy SOL with USDC (or increase SOL exposure)
                input_mint = self.jupiter.usdc_mint
                output_mint = self.jupiter.sol_mint
                # Convert SOL amount to USDC lamports (USDC has 6 decimals)
                amount_lamports = int(trade_usd * 1_000_000)
            else:
                # Sell SOL for USDC
                input_mint = self.jupiter.sol_mint
                output_mint = self.jupiter.usdc_mint

            logger.info(
                f"Executing {signal.signal_type.value}: {trade_sol:.4f} SOL "
                f"(${trade_usd:.2f}) at {leverage}x"
            )

            swap_result = await self.jupiter.execute_swap(
                keypair=self.wallet.get_keypair(),
                input_mint=input_mint,
                output_mint=output_mint,
                amount_lamports=amount_lamports,
            )

            result["status"] = "executed"
            result["tx_signature"] = swap_result["tx_signature"]

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
                "price_usd": sol_price,
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
                price_usd=sol_price,
                fees_sol=0.000005,
                new_balance_sol=new_balance,
            )

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

    async def shutdown(self):
        self._running = False
        await self.jupiter.close()
        await self.wallet.close()
        await self.telegram.close()
        await self.news.close()
        await self.kamino.close()
