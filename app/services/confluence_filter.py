"""Confluence filter — requires cross-alert confirmation before execution.

When enabled, a BUY signal is held until a *different* strategy on the same
token fires within the configured time window.  This reduces false positives
by requiring independent indicator agreement.

Design:
- Each incoming signal is stored in a pending buffer keyed by token.
- If a second signal from a *different* strategy arrives for the same token
  within the window, both are "confirmed" and the highest-confidence one
  proceeds to Claude/execution.
- If the window expires with no confirmation, the signal is either dropped
  or force-executed based on config (`on_timeout`).
- SELL/CLOSE signals always pass through — we never delay exits.
- Confluence is checked per-token regardless of timeframe.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from app.config import get
from app.models import WebhookSignal

logger = logging.getLogger("bot.confluence")


@dataclass
class PendingSignal:
    signal: WebhookSignal
    source_ip: str
    strategy: str
    token: str
    timestamp: float = field(default_factory=time.time)
    timeout_task: Optional[asyncio.Task] = None


class ConfluenceFilter:
    """Cross-alert confirmation filter."""

    def __init__(self):
        # token -> list of pending signals (different strategies)
        self._pending: dict[str, list[PendingSignal]] = {}
        self._lock = asyncio.Lock()
        # Stats
        self.total_received: int = 0
        self.total_confirmed: int = 0
        self.total_expired: int = 0
        self.total_bypassed: int = 0
        # Callback set by trade engine
        self._on_confirmed: Optional[Callable[[WebhookSignal, str], Awaitable]] = None

    @property
    def enabled(self) -> bool:
        cfg = get("confluence") or {}
        return cfg.get("enabled", False)

    @property
    def window_seconds(self) -> int:
        cfg = get("confluence") or {}
        return cfg.get("window_seconds", 1800)  # 30 min default

    @property
    def min_strategies(self) -> int:
        cfg = get("confluence") or {}
        return cfg.get("min_strategies", 2)

    @property
    def on_timeout(self) -> str:
        """What to do when window expires: 'drop' or 'execute'."""
        cfg = get("confluence") or {}
        return cfg.get("on_timeout", "drop")

    @property
    def bypass_confidence(self) -> int:
        """Signals above this confidence skip confluence check."""
        cfg = get("confluence") or {}
        return cfg.get("bypass_confidence", 95)

    def set_confirmed_callback(self, cb: Callable[[WebhookSignal, str], Awaitable]):
        """Set the callback invoked when a signal is confirmed."""
        self._on_confirmed = cb

    async def check(self, signal: WebhookSignal, source_ip: str) -> Optional[WebhookSignal]:
        """Check confluence for a signal.

        Returns:
            The signal to execute if confluence is confirmed or bypassed.
            None if the signal is being held pending confirmation.
        """
        if not self.enabled:
            return signal

        self.total_received += 1

        # SELL/CLOSE always pass through
        if signal.signal_type.value != "BUY":
            self.total_bypassed += 1
            return signal

        strategy = (signal.strategy or "unknown").upper()
        token = signal.symbol.replace("USDT", "").replace("USD", "").upper()

        # High-confidence bypass
        if signal.confidence_score >= self.bypass_confidence:
            logger.info(
                f"Confluence BYPASS: {token} from {strategy} "
                f"(confidence {signal.confidence_score} >= {self.bypass_confidence})"
            )
            self.total_bypassed += 1
            return signal

        async with self._lock:
            pending_list = self._pending.setdefault(token, [])

            # Check if we already have a signal from a DIFFERENT strategy
            other_strategies = [
                p for p in pending_list
                if p.strategy != strategy
                and (time.time() - p.timestamp) <= self.window_seconds
            ]

            if other_strategies:
                # Confluence confirmed! Pick the highest confidence signal
                all_signals = other_strategies + [
                    PendingSignal(signal=signal, source_ip=source_ip,
                                  strategy=strategy, token=token)
                ]
                best = max(all_signals, key=lambda p: p.signal.confidence_score)
                strategies_involved = {p.strategy for p in all_signals}

                # Cancel pending timeout tasks
                for p in pending_list:
                    if p.timeout_task and not p.timeout_task.done():
                        p.timeout_task.cancel()

                # Clear pending for this token
                self._pending[token] = []
                self.total_confirmed += 1

                logger.info(
                    f"Confluence CONFIRMED: {token} — "
                    f"{len(strategies_involved)} strategies ({', '.join(strategies_involved)}) "
                    f"agreed. Executing {best.strategy} (conf: {best.signal.confidence_score})"
                )
                return best.signal

            # No match yet — check if same strategy is already pending
            existing_same = [p for p in pending_list if p.strategy == strategy]
            if existing_same:
                # Update the existing pending signal if this one is higher confidence
                for p in existing_same:
                    if signal.confidence_score > p.signal.confidence_score:
                        p.signal = signal
                        p.source_ip = source_ip
                        p.timestamp = time.time()
                logger.debug(f"Confluence: updated pending {token} from {strategy}")
                return None

            # New pending signal — start timeout
            pending = PendingSignal(
                signal=signal, source_ip=source_ip,
                strategy=strategy, token=token,
            )
            pending.timeout_task = asyncio.create_task(
                self._handle_timeout(token, pending)
            )
            pending_list.append(pending)

            logger.info(
                f"Confluence HOLD: {token} from {strategy} "
                f"(conf: {signal.confidence_score}) — "
                f"waiting {self.window_seconds}s for confirmation from another strategy"
            )
            return None

    async def _handle_timeout(self, token: str, pending: PendingSignal):
        """Handle expiry of a pending signal."""
        try:
            await asyncio.sleep(self.window_seconds)
        except asyncio.CancelledError:
            return  # Cancelled because confluence was confirmed

        async with self._lock:
            # Remove this specific pending signal
            if token in self._pending:
                self._pending[token] = [
                    p for p in self._pending[token] if p is not pending
                ]

        action = self.on_timeout
        if action == "execute" and self._on_confirmed:
            logger.info(
                f"Confluence TIMEOUT (execute): {token} from {pending.strategy} "
                f"— no confirmation in {self.window_seconds}s, executing anyway"
            )
            self.total_expired += 1
            try:
                await self._on_confirmed(pending.signal, pending.source_ip)
            except Exception as e:
                logger.error(f"Confluence timeout execute error: {e}")
        else:
            logger.info(
                f"Confluence TIMEOUT (drop): {token} from {pending.strategy} "
                f"— no confirmation in {self.window_seconds}s, dropping signal"
            )
            self.total_expired += 1

            # Notify Telegram about the drop
            try:
                from app.services.telegram_service import TelegramService
                tg = TelegramService()
                await tg.send_message(
                    f"[CONFLUENCE] Dropped {pending.signal.signal_type.value} "
                    f"{pending.signal.symbol} from {pending.strategy}\n"
                    f"No confirming alert within {self.window_seconds}s window"
                )
                await tg.close()
            except Exception:
                pass

    def get_pending(self) -> list[dict]:
        """Return current pending signals."""
        now = time.time()
        result = []
        for token, plist in self._pending.items():
            for p in plist:
                remaining = max(0, self.window_seconds - (now - p.timestamp))
                result.append({
                    "token": token,
                    "strategy": p.strategy,
                    "confidence": p.signal.confidence_score,
                    "timeframe": p.signal.timeframe or "",
                    "waiting_seconds": int(now - p.timestamp),
                    "remaining_seconds": int(remaining),
                })
        return result

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "window_seconds": self.window_seconds,
            "min_strategies": self.min_strategies,
            "on_timeout": self.on_timeout,
            "bypass_confidence": self.bypass_confidence,
            "total_received": self.total_received,
            "total_confirmed": self.total_confirmed,
            "total_expired": self.total_expired,
            "total_bypassed": self.total_bypassed,
            "pending": self.get_pending(),
        }


# ── Singleton ────────────────────────────────────────────────────────────────

_instance: Optional[ConfluenceFilter] = None


def get_confluence_filter() -> ConfluenceFilter:
    global _instance
    if _instance is None:
        _instance = ConfluenceFilter()
    return _instance
