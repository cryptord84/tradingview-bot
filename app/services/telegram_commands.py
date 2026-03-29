"""Telegram command handler — listens for incoming messages and responds."""

import asyncio
import logging
from datetime import datetime

import httpx

from app.config import get
from app.database import get_trades, get_stats, get_today_trades
from app import state

logger = logging.getLogger("bot.telegram_cmd")


class TelegramCommandHandler:
    """Polls Telegram for incoming messages and responds to commands."""

    COMMANDS = {
        "/help": "List available commands",
        "/status": "Bot health, uptime, active state",
        "/wallet": "SOL and USDC balances with USD value",
        "/apis": "Check all API connections",
        "/trades": "Last 5 executed trades",
        "/today": "Today's trades and P&L",
        "/pnl": "Overall P&L summary",
        "/start": "Start the bot",
        "/stop": "Stop the bot",
    }

    def __init__(self):
        cfg = get("telegram")
        self.enabled = cfg.get("enabled", False)
        self.bot_token = cfg.get("bot_token", "")
        self.chat_id = str(cfg.get("chat_id", ""))
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._client = httpx.AsyncClient(timeout=15)
        self._offset = 0
        self._running = False

    async def send(self, text: str) -> bool:
        """Send a message to the configured chat."""
        try:
            if len(text) > 4000:
                text = text[:4000] + "\n...(truncated)"
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    async def poll_updates(self):
        """Fetch new messages from Telegram.

        Uses short polling (timeout=3) to coexist with other processes
        (e.g. Claude Code MCP plugin) that may also poll the same bot.
        409 Conflict responses are expected and handled gracefully.
        """
        try:
            resp = await self._client.get(
                f"{self.api_base}/getUpdates",
                params={"offset": self._offset, "timeout": 3, "allowed_updates": '["message"]'},
            )
            if resp.status_code == 409:
                # Another process is polling — back off and retry
                await asyncio.sleep(2)
                return []
            data = resp.json()
            if not data.get("ok"):
                return []
            updates = data.get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates
        except httpx.ReadTimeout:
            return []
        except Exception as e:
            logger.error(f"Telegram poll error: {e}")
            await asyncio.sleep(3)
            return []

    async def handle_message(self, message: dict):
        """Process a single incoming message."""
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        # Security: only respond to configured chat_id
        if chat_id != self.chat_id:
            logger.warning(f"Ignoring message from unauthorized chat: {chat_id}")
            return

        if not text.startswith("/"):
            return

        cmd = text.split()[0].lower().split("@")[0]  # strip @botname suffix
        logger.info(f"Telegram command: {cmd}")

        handlers = {
            "/help": self.cmd_help,
            "/status": self.cmd_status,
            "/health": self.cmd_status,
            "/wallet": self.cmd_wallet,
            "/apis": self.cmd_apis,
            "/trades": self.cmd_trades,
            "/today": self.cmd_today,
            "/pnl": self.cmd_pnl,
            "/start": self.cmd_start,
            "/stop": self.cmd_stop,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler()
        else:
            await self.send(f"Unknown command: <code>{cmd}</code>\nType /help for available commands.")

    # ── Command Handlers ─────────────────────────────────────────

    async def cmd_help(self):
        lines = ["<b>TRINITY BOT COMMANDS</b>\n"]
        for cmd, desc in self.COMMANDS.items():
            lines.append(f"  {cmd} — {desc}")
        await self.send("\n".join(lines))

    async def cmd_status(self):
        active = state.is_active()
        uptime = state.get_uptime()
        status_icon = "🟢" if active else "🔴"

        # Quick health check
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get("http://localhost:8000/health")
                api_ok = resp.status_code == 200
        except Exception:
            api_ok = False

        db_stats = get_stats()

        msg = (
            f"<b>{status_icon} BOT STATUS</b>\n\n"
            f"Active: <b>{'YES' if active else 'NO'}</b>\n"
            f"API Health: <b>{'OK' if api_ok else 'DOWN'}</b>\n"
            f"Uptime: <code>{uptime}</code>\n"
            f"Strategy: 1H v3.5 + Daily v2\n"
            f"Total Trades: {db_stats.get('total_trades', 0)}\n"
            f"Win Rate: {db_stats.get('win_rate', 0):.1f}%\n"
            f"Time: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def cmd_wallet(self):
        from app.services.wallet_service import WalletService
        from app.services.jupiter_client import JupiterClient

        try:
            wallet = WalletService()
            jupiter = JupiterClient()

            sol_balance = await wallet.get_balance_sol()
            sol_price = await jupiter.get_sol_price()
            sol_usd = sol_balance * sol_price

            # Try to get USDC balance
            usdc_balance = 0.0
            try:
                usdc_balance = await wallet.get_usdc_balance()
            except Exception:
                pass

            total_usd = sol_usd + usdc_balance

            msg = (
                f"<b>💰 WALLET BALANCES</b>\n\n"
                f"SOL: <b>{sol_balance:.4f}</b>\n"
                f"  └ ${sol_usd:.2f} USD @ ${sol_price:.2f}/SOL\n\n"
                f"USDC: <b>{usdc_balance:.2f}</b>\n\n"
                f"Total: <b>${total_usd:.2f} USD</b>"
            )

            await jupiter.close()
            await wallet.close()
        except Exception as e:
            msg = f"<b>💰 WALLET</b>\n\n⚠️ Error fetching balances: {e}"

        await self.send(msg)

    async def cmd_apis(self):
        results = []

        # 1. Bot API
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get("http://localhost:8000/health")
                results.append(("Bot API", resp.status_code == 200, "localhost:8000"))
        except Exception as e:
            results.append(("Bot API", False, str(e)[:40]))

        # 2. Jupiter DEX
        try:
            from app.services.jupiter_client import JupiterClient
            jupiter = JupiterClient()
            price = await jupiter.get_sol_price()
            await jupiter.close()
            results.append(("Jupiter DEX", True, f"SOL=${price:.2f}"))
        except Exception as e:
            results.append(("Jupiter DEX", False, str(e)[:40]))

        # 3. Solana RPC
        try:
            from app.services.wallet_service import WalletService
            wallet = WalletService()
            bal = await wallet.get_balance_sol()
            await wallet.close()
            results.append(("Solana RPC", True, f"{bal:.4f} SOL"))
        except Exception as e:
            results.append(("Solana RPC", False, str(e)[:40]))

        # 4. Telegram Bot
        try:
            resp = await self._client.get(f"{self.api_base}/getMe")
            data = resp.json()
            ok = data.get("ok", False)
            name = data.get("result", {}).get("username", "?") if ok else "?"
            results.append(("Telegram", ok, f"@{name}"))
        except Exception as e:
            results.append(("Telegram", False, str(e)[:40]))

        # 5. Claude AI
        try:
            import shutil
            claude_ok = shutil.which("claude") is not None
            results.append(("Claude AI", claude_ok, "CLI found" if claude_ok else "not found"))
        except Exception:
            results.append(("Claude AI", False, "check failed"))

        # 6. NewsAPI
        try:
            from app.services.news_service import NewsService
            news = NewsService()
            headlines = await news.get_headlines()
            await news.close()
            results.append(("NewsAPI", True, f"{len(headlines)} headlines"))
        except Exception as e:
            results.append(("NewsAPI", False, str(e)[:40]))

        # 7. Database
        try:
            from app.database import get_stats
            stats = get_stats()
            results.append(("Database", True, f"{stats.get('total_trades', 0)} trades"))
        except Exception as e:
            results.append(("Database", False, str(e)[:40]))

        lines = ["<b>🔌 API STATUS</b>\n"]
        for name, ok, detail in results:
            icon = "✅" if ok else "❌"
            lines.append(f"  {icon} {name}: {detail}")

        all_ok = all(ok for _, ok, _ in results)
        lines.append(f"\n{'🟢 All systems operational' if all_ok else '🟡 Some systems degraded'}")

        await self.send("\n".join(lines))

    async def cmd_trades(self):
        trades = get_trades(limit=5)
        if not trades:
            await self.send("<b>📋 RECENT TRADES</b>\n\nNo trades recorded yet.")
            return

        lines = ["<b>📋 LAST 5 TRADES</b>\n"]
        for t in trades:
            ts = t.get("timestamp", "")[:16]
            sig = t.get("signal_type", "?")
            action = t.get("action", "?")
            amount = t.get("amount_sol", 0)
            price = t.get("price_usd", 0)
            pnl = t.get("pnl_usd")
            conf = t.get("confidence_score", 0)

            icon = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "🟡"}.get(sig, "⚪")
            pnl_str = f"  P&L: ${pnl:.2f}" if pnl is not None else ""

            lines.append(
                f"{icon} <b>{sig}</b> {action} | {amount:.4f} SOL @ ${price:.2f}\n"
                f"    {ts} | Conf: {conf}%{pnl_str}"
            )

        await self.send("\n".join(lines))

    async def cmd_today(self):
        trades = get_today_trades()
        stats = get_stats()

        today_pnl = stats.get("today_pnl_usd", 0)
        total_today = len(trades) if trades else 0

        lines = [
            f"<b>📅 TODAY'S ACTIVITY</b>\n",
            f"Trades: <b>{total_today}</b>",
            f"P&L: <b>${today_pnl:+.2f}</b>",
        ]

        if trades:
            lines.append("")
            for t in trades[:10]:
                sig = t.get("signal_type", "?")
                action = t.get("action", "?")
                amount = t.get("amount_sol", 0)
                price = t.get("price_usd", 0)
                icon = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "🟡"}.get(sig, "⚪")
                lines.append(f"  {icon} {sig} {action} | {amount:.4f} SOL @ ${price:.2f}")

        await self.send("\n".join(lines))

    async def cmd_pnl(self):
        stats = get_stats()

        total_trades = stats.get("total_trades", 0)
        win_rate = stats.get("win_rate", 0)
        total_pnl = stats.get("total_pnl_usd", 0)
        today_pnl = stats.get("today_pnl_usd", 0)

        pnl_icon = "📈" if total_pnl >= 0 else "📉"

        msg = (
            f"<b>{pnl_icon} P&L SUMMARY</b>\n\n"
            f"Total P&L: <b>${total_pnl:+.2f}</b>\n"
            f"Today P&L: <b>${today_pnl:+.2f}</b>\n"
            f"Total Trades: {total_trades}\n"
            f"Win Rate: {win_rate:.1f}%\n\n"
            f"Monthly Costs: $133.95\n"
            f"Break-even: ~$134/mo profit needed"
        )
        await self.send(msg)

    async def cmd_start(self):
        if state.is_active():
            await self.send("🟢 Bot is already running.")
            return
        result = state.start_bot()
        await self.send(f"🟢 <b>Bot started</b>\nStarted at: {result['started_at']}")

    async def cmd_stop(self):
        if not state.is_active():
            await self.send("🔴 Bot is already stopped.")
            return
        result = state.stop_bot()
        await self.send(f"🔴 <b>Bot stopped</b>\nStopped at: {result['stopped_at']}")

    # ── Polling Loop ─────────────────────────────────────────────

    async def run(self):
        """Main polling loop — runs as a background task."""
        if not self.enabled or not self.bot_token:
            logger.info("Telegram commands disabled (no token or disabled in config)")
            return

        self._running = True
        logger.info(f"Telegram command handler started (chat_id: {self.chat_id})")

        # Skip any messages that arrived while bot was offline
        try:
            await self.poll_updates()
        except Exception:
            pass

        while self._running:
            try:
                updates = await self.poll_updates()
                for update in updates:
                    msg = update.get("message")
                    if msg:
                        await self.handle_message(msg)
                # Brief pause between polls to reduce 409 conflicts
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Telegram command loop error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        await self._client.aclose()
