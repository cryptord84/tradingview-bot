"""Telegram command handler — listens for incoming messages and responds."""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import httpx

from app.config import get
from app.database import get_trades, get_stats, get_today_trades, get_wallet_transactions, get_kamino_net_deposited, get_open_positions
from app import state

logger = logging.getLogger("bot.telegram_cmd")


class TelegramCommandHandler:
    """Polls Telegram for incoming messages and responds to commands."""

    COMMANDS = {
        "/help": "List available commands",
        "/status": "Bot health, uptime, active state",
        "/wallet": "SOL and USDC balances with USD value",
        "/vault": "Kamino vault balance, APY, and earnings",
        "/apis": "Check all API connections",
        "/trades": "Last 5 executed trades",
        "/txlog": "Recent wallet transactions",
        "/today": "Today's trades and P&L",
        "/pnl": "Overall P&L summary",
        "/pull": "Git pull latest changes",
        "/scan": "Scan Indicators directory for changes",
        "/review": "Review an indicator script (/review filename)",
        "/positions": "Show open positions with live P&L",
        "/scout": "Run multi-source intelligence scan (X, blogs, on-chain, news)",
        "/start": "Start the bot",
        "/stop": "Stop the bot",
    }

    # Project root directory
    PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

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

        if not text:
            return

        if text.startswith("/"):
            cmd = text.split()[0].lower().split("@")[0]  # strip @botname suffix
            logger.info(f"Telegram command: {cmd}")

            handlers = {
                "/help": self.cmd_help,
                "/status": self.cmd_status,
                "/health": self.cmd_status,
                "/wallet": self.cmd_wallet,
                "/vault": self.cmd_vault,
                "/apis": self.cmd_apis,
                "/trades": self.cmd_trades,
                "/txlog": self.cmd_txlog,
                "/today": self.cmd_today,
                "/pnl": self.cmd_pnl,
                "/pull": self.cmd_pull,
                "/scan": self.cmd_scan,
                "/positions": self.cmd_positions,
                "/scout": self.cmd_scout,
                "/start": self.cmd_start,
                "/stop": self.cmd_stop,
            }

            # /review takes an argument
            if cmd == "/review":
                args = text.split(maxsplit=1)
                filename = args[1].strip() if len(args) > 1 else ""
                await self.cmd_review(filename)
                return

            handler = handlers.get(cmd)
            if handler:
                await handler()
            else:
                await self.send(f"Unknown command: <code>{cmd}</code>\nType /help for available commands.")
        else:
            # Free-form message — route to Claude
            await self.cmd_chat(text)

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
            f"Strategy: 4H v1.3 + 1H v3.5 + Daily v2\n"
            f"Total Trades: {db_stats.get('total_trades', 0)}\n"
            f"Win Rate: {db_stats.get('win_rate', 0):.1f}%\n"
            f"Time: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def cmd_wallet(self):
        from app.services.wallet_service import WalletService
        from app.services.jupiter_client import JupiterClient
        from app.services.kamino_client import KaminoClient

        try:
            wallet = WalletService()
            jupiter = JupiterClient()
            kamino = KaminoClient()

            sol_balance = await wallet.get_balance_sol()
            sol_price = await jupiter.get_sol_price()
            sol_usd = sol_balance * sol_price

            usdc_balance = 0.0
            try:
                usdc_balance = await wallet.get_usdc_balance()
            except Exception:
                pass

            kamino_usdc = 0.0
            try:
                if kamino.enabled:
                    position = await kamino.get_user_position(wallet.public_key)
                    kamino_usdc = position.get("deposited_usdc", 0)
            except Exception:
                pass

            total_usd = sol_usd + usdc_balance + kamino_usdc
            addr = wallet.public_key

            msg = (
                f"<b>💰 WALLET BALANCES</b>\n\n"
                f"SOL: <b>{sol_balance:.4f}</b> (${sol_usd:.2f})\n"
                f"USDC: <b>${usdc_balance:.2f}</b>\n"
                f"Kamino Vault: <b>${kamino_usdc:.2f}</b>\n\n"
                f"Total: <b>${total_usd:.2f} USD</b>\n"
                f"SOL Price: ${sol_price:.2f}\n\n"
                f"<a href='https://solscan.io/account/{addr}'>View on Solscan ↗</a>"
            )

            await jupiter.close()
            await wallet.close()
            await kamino.close()
        except Exception as e:
            msg = f"<b>💰 WALLET</b>\n\n⚠️ Error fetching balances: {e}"

        await self.send(msg)

    async def cmd_vault(self):
        from app.services.wallet_service import WalletService
        from app.services.kamino_client import KaminoClient

        try:
            wallet = WalletService()
            kamino = KaminoClient()

            if not kamino.enabled:
                await self.send("<b>🏦 KAMINO VAULT</b>\n\nKamino Lend is disabled.")
                return

            metrics = await kamino.get_reserve_metrics()
            position = await kamino.get_user_position(wallet.public_key)
            deposited = position.get("deposited_usdc", 0)
            net_deposited = get_kamino_net_deposited()
            earnings = deposited - net_deposited if deposited > 0 else 0.0
            supply_apy = metrics.get("supply_apy", 0)
            daily_est = deposited * supply_apy / 100 / 365
            monthly_est = deposited * supply_apy / 100 / 12

            earn_str = f"+${earnings:.4f}" if earnings >= 0 else f"-${abs(earnings):.4f}"

            msg = (
                f"<b>🏦 KAMINO VAULT</b>\n\n"
                f"Deposited: <b>${deposited:.2f}</b>\n"
                f"Earnings: <b>{earn_str}</b>\n"
                f"APY: <b>{supply_apy:.2f}%</b>\n\n"
                f"Est. Daily: ${daily_est:.4f}\n"
                f"Est. Monthly: ${monthly_est:.2f}\n\n"
                f"Auto-deposit: {'ON' if kamino.auto_deposit else 'OFF'}\n"
                f"Auto-withdraw: {'ON' if kamino.auto_withdraw else 'OFF'}\n"
                f"Reserve: ${kamino.reserve_usdc:.0f} USDC"
            )

            await wallet.close()
            await kamino.close()
        except Exception as e:
            msg = f"<b>🏦 KAMINO VAULT</b>\n\n⚠️ Error: {e}"

        await self.send(msg)

    async def cmd_txlog(self):
        txs = get_wallet_transactions(limit=10)
        if not txs:
            await self.send("<b>📒 WALLET TRANSACTIONS</b>\n\nNo transactions recorded yet.")
            return

        type_labels = {
            "kamino_deposit": "Kamino Deposit",
            "kamino_withdraw": "Kamino Withdraw",
            "swap": "Swap",
        }

        lines = ["<b>📒 RECENT TRANSACTIONS</b>\n"]
        for tx in txs:
            ts = tx.get("timestamp", "")[:16]
            tx_type = type_labels.get(tx["tx_type"], tx["tx_type"])
            direction = "→ IN" if tx["direction"] == "in" else "← OUT"
            token = tx["token"]
            amount = f"{tx['amount']:.4f}" if token == "SOL" else f"${tx['amount']:.2f}"
            fee = tx.get("fee_sol", 0)
            status = "✅" if tx["status"] == "success" else "❌"
            sig = tx.get("tx_signature", "")
            sig_link = f"<a href='https://solscan.io/tx/{sig}'>{sig[:8]}...</a>" if sig else "--"

            lines.append(
                f"{status} <b>{tx_type}</b> {direction}\n"
                f"    {amount} {token} | Fee: {fee:.6f} SOL\n"
                f"    {ts} | {sig_link}"
            )

        await self.send("\n".join(lines))

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

    async def cmd_pull(self):
        """Run git pull in the project directory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "pull",
                cwd=str(self.PROJECT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode().strip()
            err = stderr.decode().strip()

            if proc.returncode == 0:
                msg = f"<b>📥 GIT PULL</b>\n\n<code>{output}</code>"
                if err:
                    msg += f"\n\n{err}"
            else:
                msg = f"<b>📥 GIT PULL — FAILED</b>\n\n<code>{err or output}</code>"

            await self.send(msg)
        except asyncio.TimeoutError:
            await self.send("⚠️ Git pull timed out after 30s")
        except Exception as e:
            await self.send(f"⚠️ Git pull error: {e}")

    async def cmd_scan(self):
        """Scan the Indicators directory for files and show recent changes."""
        indicators_dir = self.PROJECT_DIR / "Indicators"
        if not indicators_dir.exists():
            await self.send("<b>📂 SCAN</b>\n\nIndicators/ directory not found.")
            return

        try:
            # Get all indicator files
            files = sorted(indicators_dir.rglob("*.pine"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                files = sorted(indicators_dir.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
                files = [f for f in files if f.is_file() and not f.name.startswith(".")]

            if not files:
                await self.send("<b>📂 SCAN</b>\n\nNo indicator files found.")
                return

            # Get git status for indicators
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", "Indicators/",
                cwd=str(self.PROJECT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            git_changes = stdout.decode().strip()

            lines = [f"<b>📂 INDICATORS SCAN</b>\n"]
            lines.append(f"Found <b>{len(files)}</b> file(s):\n")

            for f in files[:20]:
                rel = f.relative_to(self.PROJECT_DIR)
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                size = f.stat().st_size
                age = datetime.now() - mtime
                if age.days > 0:
                    age_str = f"{age.days}d ago"
                elif age.seconds > 3600:
                    age_str = f"{age.seconds // 3600}h ago"
                else:
                    age_str = f"{age.seconds // 60}m ago"

                lines.append(f"  📄 <code>{rel}</code>\n      {size:,}b | modified {age_str}")

            if git_changes:
                lines.append(f"\n<b>Uncommitted changes:</b>\n<code>{git_changes}</code>")
            else:
                lines.append("\n✅ All files committed")

            lines.append(f"\nUse <code>/review filename</code> to analyze a script")

            await self.send("\n".join(lines))
        except Exception as e:
            await self.send(f"⚠️ Scan error: {e}")

    async def cmd_review(self, filename: str):
        """Read an indicator file and send it to Claude for analysis."""
        if not filename:
            await self.send(
                "<b>📝 REVIEW</b>\n\n"
                "Usage: <code>/review filename.pine</code>\n"
                "Or: <code>/review Indicators/subfolder/file.pine</code>\n\n"
                "Run /scan to see available files."
            )
            return

        # Search for the file
        search_path = Path(filename)
        candidates = []

        # Try exact path from project root
        exact = self.PROJECT_DIR / search_path
        if exact.is_file():
            candidates.append(exact)

        # Try inside Indicators/
        in_indicators = self.PROJECT_DIR / "Indicators" / search_path
        if in_indicators.is_file() and in_indicators not in candidates:
            candidates.append(in_indicators)

        # Fuzzy search by filename in Indicators/
        if not candidates:
            indicators_dir = self.PROJECT_DIR / "Indicators"
            if indicators_dir.exists():
                for f in indicators_dir.rglob("*"):
                    if f.is_file() and filename.lower() in f.name.lower():
                        candidates.append(f)

        if not candidates:
            await self.send(f"❌ File not found: <code>{filename}</code>\n\nRun /scan to see available files.")
            return

        target = candidates[0]
        rel_path = target.relative_to(self.PROJECT_DIR)

        try:
            content = target.read_text()
        except Exception as e:
            await self.send(f"❌ Cannot read <code>{rel_path}</code>: {e}")
            return

        if len(content) > 15000:
            content = content[:15000] + "\n// ... (truncated)"

        await self.send(f"🔍 Reviewing <code>{rel_path}</code> ({len(content):,} chars)...\nThis may take a moment.")

        # Send to Claude for analysis
        system_prompt = (
            "You are Trinity, an expert Pine Script and trading indicator analyst. "
            "Review the following indicator script and provide:\n"
            "1. **Summary** — what the indicator does in 2-3 sentences\n"
            "2. **Signals** — what buy/sell/close signals it generates\n"
            "3. **Strengths** — what it does well\n"
            "4. **Weaknesses** — repainting risk, overfitting, missing features\n"
            "5. **Verdict** — would you recommend using it? Score 1-10\n\n"
            "Be concise — this is Telegram. Use HTML formatting (bold, italic, code)."
        )

        try:
            cfg = get("claude")
            mode = cfg.get("mode", "cli")

            user_msg = f"Review this Pine Script indicator ({rel_path}):\n\n{content}"

            if mode == "cli":
                response = await self._call_claude_cli(system_prompt, user_msg)
            else:
                response = await self._call_claude_api(system_prompt, user_msg)

            if len(response) > 3900:
                response = response[:3900] + "\n...(truncated)"

            await self.send(response)
        except Exception as e:
            logger.error(f"Review error: {e}")
            await self.send(f"⚠️ Claude review failed: {str(e)[:100]}")

    async def cmd_positions(self):
        """Show open positions with live P&L."""
        positions = get_open_positions()
        if not positions:
            await self.send("<b>📊 OPEN POSITIONS</b>\n\nNo open positions.")
            return

        from app.services.jupiter_client import JupiterClient
        jupiter = JupiterClient()
        try:
            price = await jupiter.get_sol_price()
        finally:
            await jupiter.close()

        lines = [f"<b>📊 OPEN POSITIONS</b> (SOL=${price:.2f})\n"]
        for p in positions:
            pnl = (price - p["entry_price"]) * p["amount_sol"]
            pnl_pct = ((price - p["entry_price"]) / p["entry_price"]) * 100
            icon = "📈" if pnl >= 0 else "📉"

            tp_dist = ((p["tp_price"] - price) / price) * 100
            sl_dist = ((price - p["sl_price"]) / price) * 100

            lines.append(
                f"{icon} <b>{p['symbol']}</b> LONG\n"
                f"  Entry: ${p['entry_price']:.2f} | Size: {p['amount_sol']:.4f} SOL\n"
                f"  TP: ${p['tp_price']:.2f} ({tp_dist:+.1f}%) | SL: ${p['sl_price']:.2f} (-{sl_dist:.1f}%)\n"
                f"  P&L: <b>${pnl:+.2f}</b> ({pnl_pct:+.1f}%)\n"
                f"  Opened: {p['created_at'][:16]}"
            )

        await self.send("\n".join(lines))

    async def cmd_scout(self):
        """Manually trigger the multi-source intelligence scan."""
        await self.send("🔍 Running multi-source intelligence scan (X.com, blogs, on-chain, news)...\nThis may take 30-60 seconds.")
        try:
            from app.services.scout_service import ScoutService
            scout = ScoutService()
            report = await scout.generate_report()
            msg = await scout.format_telegram_message(report)
            await scout.close()
            await self.send(msg)
        except Exception as e:
            await self.send(f"⚠️ Scout scan failed: {str(e)[:200]}")

    async def cmd_chat(self, message: str):
        """Send a free-form message to Claude and reply with the response."""
        logger.info(f"Chat message: {message[:80]}")

        # Gather bot context so Claude has awareness
        context = await self._build_chat_context()
        system_prompt = (
            "You are Trinity, an AI assistant for a Solana trading bot. "
            "You help the user with trading questions, market analysis, strategy ideas, "
            "bot configuration, and general questions. Be concise — this is Telegram, "
            "keep responses under 300 words. Use plain text (Telegram HTML is OK for bold/italic). "
            "You have access to the bot's current state below.\n\n"
            f"{context}"
        )

        try:
            cfg = get("claude")
            mode = cfg.get("mode", "cli")

            if mode == "cli":
                response_text = await self._call_claude_cli(system_prompt, message)
            else:
                response_text = await self._call_claude_api(system_prompt, message)

            if len(response_text) > 3900:
                response_text = response_text[:3900] + "\n...(truncated)"

            await self.send(response_text)
        except Exception as e:
            logger.error(f"Chat error: {e}")
            await self.send(f"⚠️ Claude unavailable: {str(e)[:100]}\n\nTry a /command instead.")

    async def _build_chat_context(self) -> str:
        """Gather current bot state for Claude context."""
        lines = []
        try:
            stats = get_stats()
            lines.append(
                f"Bot: {'ACTIVE' if state.is_active() else 'STOPPED'} | "
                f"Uptime: {state.get_uptime()}"
            )
            lines.append(
                f"Trades: {stats['total_trades']} total | "
                f"P&L: ${stats['total_pnl_usd']:+.2f} | "
                f"Today: ${stats['today_pnl_usd']:+.2f}"
            )
        except Exception:
            pass

        try:
            from app.services.wallet_service import WalletService
            from app.services.jupiter_client import JupiterClient
            from app.services.kamino_client import KaminoClient

            wallet = WalletService()
            jupiter = JupiterClient()
            sol = await wallet.get_balance_sol()
            price = await jupiter.get_sol_price()
            usdc = await wallet.get_usdc_balance()

            kamino_usdc = 0.0
            kamino = KaminoClient()
            if kamino.enabled:
                pos = await kamino.get_user_position(wallet.public_key)
                kamino_usdc = pos.get("deposited_usdc", 0)
                await kamino.close()

            total = sol * price + usdc + kamino_usdc
            lines.append(
                f"Wallet: {sol:.4f} SOL (${sol*price:.2f}) | "
                f"USDC: ${usdc:.2f} | Kamino: ${kamino_usdc:.2f} | "
                f"Total: ${total:.2f}"
            )
            lines.append(f"SOL Price: ${price:.2f}")
            await jupiter.close()
            await wallet.close()
        except Exception:
            pass

        return "## Current Bot State\n" + "\n".join(lines) if lines else ""

    async def _call_claude_cli(self, system_prompt: str, user_message: str) -> str:
        """Call Claude via CLI for chat.

        Uses create_subprocess_exec (not shell) to avoid command injection.
        The prompt is passed as a positional argument, not interpolated into a shell string.
        """
        cfg = get("claude")
        cli_path = cfg.get("cli_path", "claude")
        timeout = cfg.get("timeout_seconds", 60)

        resolved = shutil.which(cli_path)
        if not resolved:
            raise FileNotFoundError("Claude CLI not found")

        full_prompt = f"{system_prompt}\n\n---\n\nUser message: {user_message}"

        # create_subprocess_exec passes args as a list — no shell expansion, safe from injection
        proc = await asyncio.create_subprocess_exec(
            resolved, "--print", full_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"Claude CLI timed out after {timeout}s")

        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "unknown error"
            raise RuntimeError(f"CLI error: {err[:200]}")

        return stdout.decode().strip()

    async def _call_claude_api(self, system_prompt: str, user_message: str) -> str:
        """Call Claude via Anthropic API for chat."""
        import anthropic

        cfg = get("claude")
        api_key = cfg.get("api_key", "")
        if not api_key:
            raise ValueError("No API key configured — set claude.api_key or use mode: cli")

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=cfg.get("model", "sonnet"),
            max_tokens=cfg.get("max_tokens", 1024),
            temperature=0.7,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return message.content[0].text

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
