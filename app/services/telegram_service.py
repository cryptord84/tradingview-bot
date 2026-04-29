"""Telegram notification service."""

import logging
from typing import Optional

import httpx

from app.config import get

logger = logging.getLogger("bot.telegram")


class TelegramService:
    """Send notifications to Telegram."""

    def __init__(self):
        cfg = get("telegram")
        self.enabled = cfg.get("enabled", False)
        self.bot_token = cfg.get("bot_token", "")
        self.chat_id = str(cfg.get("chat_id", ""))
        self.send_on = cfg.get("send_on", {})
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._client = httpx.AsyncClient(timeout=15)

    async def send_message(self, text: str, parse_mode: str = "") -> bool:
        if not self.enabled or not self.bot_token:
            logger.debug("Telegram disabled or no token, skipping notification")
            return False

        try:
            if len(text) > 4000:
                text = text[:4000] + "\n...(truncated)"

            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            resp = await self._client.post(f"{self.api_base}/sendMessage", json=payload)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    async def notify_webhook_received(self, signal: dict):
        if not self.send_on.get("webhook_received", True):
            return
        msg = (
            f"<b>📡 Webhook Received</b>\n\n"
            f"Signal: <b>{signal.get('signal_type', '?')}</b>\n"
            f"Symbol: {signal.get('symbol', '?')}\n"
            f"Price: ${signal.get('entry_price_estimate', 0):.4f}\n"
            f"Confidence: {signal.get('confidence_score', 0)}%\n"
            f"Leverage: {signal.get('suggested_leverage', 1)}x\n"
            f"Size: {signal.get('suggested_position_size_percent', 0)}%"
        )
        await self.send_message(msg, parse_mode="HTML")

    async def notify_claude_decision(self, decision: dict, signal_type: str):
        if not self.send_on.get("claude_decision", True):
            return
        emoji = {"EXECUTE": "✅", "REJECT": "❌", "MODIFY": "🔧"}.get(
            decision.get("decision", ""), "❓"
        )
        msg = (
            f"<b>{emoji} Claude Decision: {decision.get('decision', '?')}</b>\n\n"
            f"Signal: {signal_type}\n"
            f"Risk Score: {decision.get('risk_score', '?')}/10\n"
            f"Reasoning: {decision.get('reasoning', 'N/A')}\n"
        )
        if decision.get("modified_size_percent"):
            msg += f"Modified Size: {decision['modified_size_percent']}%\n"
        if decision.get("modified_leverage"):
            msg += f"Modified Leverage: {decision['modified_leverage']}x\n"
        if decision.get("geo_risk_note"):
            msg += f"Geo Risk: {decision['geo_risk_note']}\n"
        await self.send_message(msg, parse_mode="HTML")

    async def notify_trade_executed(
        self,
        tx_sig: str,
        action: str,
        amount_sol: float,
        price_usd: float,
        fees_sol: float,
        new_balance_sol: float,
        symbol: str = "",
        trade_usd: float = 0.0,
    ):
        if not self.send_on.get("trade_executed", True):
            return
        token = symbol.replace("USDT", "").replace("USD", "") if symbol else "SOL"
        msg = (
            f"<b>💰 Trade Executed</b>\n\n"
            f"Action: {action} {token}\n"
            f"Amount: ${trade_usd:.2f}\n"
            f"Price: ${price_usd:.4f}\n"
            f"TX: <code>{tx_sig}</code>"
        )
        await self.send_message(msg, parse_mode="HTML")

    async def notify_error(self, error: str, context: Optional[str] = None):
        if not self.send_on.get("errors", True):
            return
        msg = f"<b>⚠️ Error</b>\n\n{error}"
        if context:
            msg += f"\n\nContext: {context}"
        await self.send_message(msg, parse_mode="HTML")

    async def notify_daily_summary(self, stats: dict):
        if not self.send_on.get("daily_summary", True):
            return
        msg = (
            f"<b>📊 Daily Summary</b>\n\n"
            f"Trades: {stats.get('total_trades', 0)}\n"
            f"Win Rate: {stats.get('win_rate', 0):.1f}%\n"
            f"P&L: ${stats.get('today_pnl_usd', 0):.2f}\n"
            f"Balance: {stats.get('wallet_balance_sol', 0):.4f} SOL"
        )
        await self.send_message(msg, parse_mode="HTML")

    async def close(self):
        await self._client.aclose()
