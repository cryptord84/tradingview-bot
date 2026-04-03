"""Global risk circuit breaker for all Kalshi bots.

Monitors aggregate P&L across all bots and triggers a global halt
when the daily loss exceeds the configured threshold. All bots are
killed immediately and a Telegram alert is sent.

Reset manually via API or Telegram after investigating the loss.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.risk")


class KalshiRiskManager:
    """Cross-bot daily loss circuit breaker."""

    def __init__(self):
        cfg = get("kalshi") or {}
        risk_cfg = cfg.get("risk_manager", {})

        self.enabled = risk_cfg.get("enabled", True)
        self.max_daily_loss_cents = risk_cfg.get("max_daily_loss_cents", 1000)  # $10
        self.check_interval = risk_cfg.get("check_interval_seconds", 30)
        self.telegram_alerts = risk_cfg.get("telegram_alerts", True)

        # State
        self._tripped = False
        self._tripped_at: Optional[str] = None
        self._trip_reason: Optional[str] = None
        self._trip_pnl_cents: int = 0
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_count = 0
        self._last_checked: Optional[str] = None
        self._day_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Risk manager started: max daily loss=${self.max_daily_loss_cents/100:.2f}, "
            f"check every {self.check_interval}s"
        )
        return self._task

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run_loop(self):
        while self._running:
            try:
                self._check_count += 1
                await self._check()
            except Exception as e:
                logger.error(f"Risk manager check error: {e}")
            await asyncio.sleep(self.check_interval)

    async def _check(self):
        """Check aggregate P&L and trip if threshold exceeded."""
        self._last_checked = datetime.now(timezone.utc).isoformat()

        # Reset on new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start:
            self._day_start = today
            if self._tripped:
                logger.info("New day — auto-resetting circuit breaker")
                self._tripped = False
                self._tripped_at = None
                self._trip_reason = None
                self._trip_pnl_cents = 0

        if self._tripped:
            return  # Already halted, waiting for reset

        total_pnl = self._get_aggregate_pnl()

        if total_pnl <= -self.max_daily_loss_cents:
            await self._trip(total_pnl)

    def _get_aggregate_pnl(self) -> int:
        """Sum P&L cents across all trading bots + DB-recorded positions."""
        total = 0

        # Market Maker (in-memory P&L)
        try:
            from app.services.kalshi_market_maker import get_market_maker
            total += get_market_maker()._total_pnl_cents
        except Exception:
            pass

        # Spread Bot (in-memory P&L)
        try:
            from app.services.kalshi_spread_bot import get_spread_bot
            total += get_spread_bot()._total_pnl_cents
        except Exception:
            pass

        # Technical Bot (in-memory P&L)
        try:
            from app.services.kalshi_technical_bot import get_technical_bot
            total += get_technical_bot()._total_pnl_cents
        except Exception:
            pass

        # DB-recorded Kalshi positions (covers AI agent + any manual trades)
        try:
            from app.database import get_kalshi_stats
            db_stats = get_kalshi_stats()
            db_pnl = db_stats.get("total_pnl_cents", 0) or 0
            total += db_pnl
        except Exception:
            pass

        return total

    async def _trip(self, pnl_cents: int):
        """Trip the circuit breaker — kill all bots."""
        self._tripped = True
        self._tripped_at = datetime.now(timezone.utc).isoformat()
        self._trip_pnl_cents = pnl_cents
        self._trip_reason = (
            f"Daily loss ${abs(pnl_cents)/100:.2f} exceeded "
            f"limit ${self.max_daily_loss_cents/100:.2f}"
        )
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {self._trip_reason}")

        # Kill all bots
        killed = []

        try:
            from app.services.kalshi_market_maker import get_market_maker
            mm = get_market_maker()
            if mm._running:
                mm.kill()
                killed.append("Market Maker")
        except Exception:
            pass

        try:
            from app.services.kalshi_spread_bot import get_spread_bot
            sb = get_spread_bot()
            if sb._running:
                sb.kill()
                killed.append("Spread Bot")
        except Exception:
            pass

        try:
            from app.services.kalshi_technical_bot import get_technical_bot
            tb = get_technical_bot()
            if tb._running:
                tb.stop()
                killed.append("Technical Bot")
        except Exception:
            pass

        try:
            from app.services.kalshi_ai_agent import get_ai_agent_bot
            ai = get_ai_agent_bot()
            if ai._running:
                ai.stop()
                killed.append("AI Agent")
        except Exception:
            pass

        try:
            from app.services.kalshi_arbitrage import get_arbitrage_scanner
            arb = get_arbitrage_scanner()
            if arb._running:
                arb.stop()
                killed.append("Arb Scanner")
        except Exception:
            pass

        try:
            from app.services.kalshi_sports_scanner import get_sports_scanner
            sports = get_sports_scanner()
            if sports._running:
                sports.stop()
                killed.append("Sports")
        except Exception:
            pass

        try:
            from app.services.kalshi_esports_scanner import get_esports_scanner
            esports = get_esports_scanner()
            if esports._running:
                esports.stop()
                killed.append("Esports")
        except Exception:
            pass

        logger.critical(f"Killed bots: {', '.join(killed) or 'none running'}")

        # Emergency flatten — cancel all orders and sell all positions
        flatten_result = await self._emergency_flatten()

        # Telegram alert
        flatten_summary = flatten_result.get("summary", "none")
        await self._alert(
            f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n\n"
            f"<b>Reason:</b> {self._trip_reason}\n"
            f"<b>Aggregate P&L:</b> ${pnl_cents/100:+.2f}\n"
            f"<b>Bots killed:</b> {', '.join(killed) or 'none'}\n"
            f"<b>Emergency flatten:</b> {flatten_summary}\n"
            f"<b>Time:</b> {self._tripped_at}\n\n"
            f"All Kalshi trading is HALTED.\n"
            f"Use /kalshi_reset or the dashboard to resume."
        )

    async def _emergency_flatten(self) -> dict:
        """Cancel all open orders and sell all positions to stop bleeding."""
        orders_cancelled = 0
        positions_closed = 0
        errors = []

        try:
            from app.services.kalshi_client import get_async_kalshi_client
            client = get_async_kalshi_client()

            # 1. Cancel all open/resting orders
            try:
                open_orders = await client.get_open_orders()
                for order in open_orders:
                    order_id = order.get("order_id")
                    if order_id:
                        try:
                            await client.cancel_order(order_id)
                            orders_cancelled += 1
                        except Exception as e:
                            errors.append(f"cancel {order_id}: {e}")
                logger.info(f"Emergency flatten: cancelled {orders_cancelled} orders")
            except Exception as e:
                errors.append(f"list orders: {e}")
                logger.error(f"Emergency flatten: failed to list orders: {e}")

            # 2. Sell all open positions at market (aggressive pricing)
            try:
                positions = await client.get_positions()
                for pos in positions:
                    ticker = pos.get("ticker", "")
                    count = pos.get("position", 0)
                    if count == 0 or not ticker:
                        continue

                    try:
                        if count > 0:
                            # Long YES position — sell YES at 1c (aggressive exit)
                            await client.place_order(
                                ticker=ticker, side="yes", action="sell",
                                yes_price=1, count=count, order_type="limit",
                            )
                        else:
                            # Long NO position — sell NO at 1c (aggressive exit)
                            await client.place_order(
                                ticker=ticker, side="no", action="sell",
                                no_price=1, count=abs(count), order_type="limit",
                            )
                        positions_closed += 1
                    except Exception as e:
                        errors.append(f"close {ticker}: {e}")
                logger.info(f"Emergency flatten: closed {positions_closed} positions")
            except Exception as e:
                errors.append(f"list positions: {e}")
                logger.error(f"Emergency flatten: failed to list positions: {e}")

        except Exception as e:
            errors.append(f"client init: {e}")
            logger.error(f"Emergency flatten: client error: {e}")

        summary = f"{orders_cancelled} orders cancelled, {positions_closed} positions closed"
        if errors:
            summary += f" ({len(errors)} errors)"
            logger.error(f"Emergency flatten errors: {errors}")

        return {"orders_cancelled": orders_cancelled, "positions_closed": positions_closed,
                "errors": errors, "summary": summary}

    def check_order(self, order_cost_cents: int, bot_name: str = "unknown") -> dict:
        """Pre-trade gate: returns {"allowed": True} or {"allowed": False, "reason": ...}.

        Checks:
        1. Circuit breaker not tripped
        2. Current P&L + order cost wouldn't breach daily loss limit
        """
        if self._tripped:
            return {
                "allowed": False,
                "reason": f"Circuit breaker tripped: {self._trip_reason}",
            }

        current_pnl = self._get_aggregate_pnl()
        # Worst case: we lose the entire order cost
        projected_loss = current_pnl - order_cost_cents

        if projected_loss <= -self.max_daily_loss_cents:
            reason = (
                f"Order blocked: worst-case loss ${abs(projected_loss)/100:.2f} "
                f"would breach daily limit ${self.max_daily_loss_cents/100:.2f} "
                f"(current P&L: ${current_pnl/100:+.2f}, order cost: ${order_cost_cents/100:.2f})"
            )
            logger.warning(f"[{bot_name}] {reason}")
            return {"allowed": False, "reason": reason}

        return {"allowed": True}

    def reset(self) -> dict:
        """Manually reset the circuit breaker to resume trading."""
        if not self._tripped:
            return {"reset": False, "message": "Circuit breaker is not tripped"}

        old_reason = self._trip_reason
        self._tripped = False
        self._tripped_at = None
        self._trip_reason = None
        self._trip_pnl_cents = 0
        logger.warning(f"Circuit breaker RESET manually (was: {old_reason})")
        return {"reset": True, "message": f"Circuit breaker reset. Previous trip: {old_reason}"}

    async def _alert(self, message: str):
        if not self.telegram_alerts:
            return
        try:
            from app.services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(message)
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "tripped": self._tripped,
            "tripped_at": self._tripped_at,
            "trip_reason": self._trip_reason,
            "trip_pnl_cents": self._trip_pnl_cents,
            "max_daily_loss_cents": self.max_daily_loss_cents,
            "max_daily_loss_usd": round(self.max_daily_loss_cents / 100, 2),
            "current_pnl_cents": self._get_aggregate_pnl(),
            "check_count": self._check_count,
            "last_checked": self._last_checked,
            "day": self._day_start,
        }


# Singleton
_risk_manager: Optional[KalshiRiskManager] = None


def get_risk_manager() -> KalshiRiskManager:
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = KalshiRiskManager()
    return _risk_manager
