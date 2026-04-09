"""SOL/Jupiter trade risk manager — daily loss limit, per-token exposure, circuit breaker.

Mirrors the Kalshi risk manager pattern but for Solana token trades via Jupiter.
Tracks realized P&L from the database and live unrealized P&L from open positions.
"""

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from threading import Lock
from typing import Optional

from app.config import get
from app.database import get_stats, get_open_positions, get_today_trades

logger = logging.getLogger("bot.sol_risk")


class SolRiskManager:
    """Risk gating for SOL/Jupiter trades."""

    def __init__(self):
        self._lock = Lock()
        self._tripped = False
        self._tripped_at: Optional[datetime] = None
        self._today: str = date.today().isoformat()

        # Per-token exposure tracking (token -> total USD committed today)
        self._token_exposure: dict[str, float] = defaultdict(float)

        # Daily P&L from database
        self._realized_pnl: float = 0.0
        self._last_sync: float = 0.0

    # ── Configuration ────────────────────────────────────────────────────

    @property
    def _cfg(self) -> dict:
        return get("sol_risk") or {}

    @property
    def enabled(self) -> bool:
        return self._cfg.get("enabled", True)

    @property
    def max_daily_loss_usd(self) -> float:
        return self._cfg.get("max_daily_loss_usd", 50.0)

    @property
    def max_token_exposure_usd(self) -> float:
        return self._cfg.get("max_token_exposure_usd", 200.0)

    @property
    def max_single_trade_usd(self) -> float:
        return self._cfg.get("max_single_trade_usd", 100.0)

    @property
    def max_open_positions(self) -> int:
        return self._cfg.get("max_open_positions", 3)

    # ── Daily Reset ──────────────────────────────────────────────────────

    def _check_day_reset(self):
        today = date.today().isoformat()
        if today != self._today:
            logger.info(f"SOL risk manager: day rolled from {self._today} to {today}")
            self._today = today
            self._tripped = False
            self._tripped_at = None
            self._token_exposure.clear()
            self._realized_pnl = 0.0

    def _sync_pnl(self):
        """Sync realized P&L from database (throttled to once per 30s)."""
        now = time.time()
        if now - self._last_sync < 30:
            return
        self._last_sync = now
        try:
            stats = get_stats()
            self._realized_pnl = stats.get("today_pnl_usd", 0.0)
        except Exception as e:
            logger.warning(f"SOL risk: could not sync P&L: {e}")

    # ── Pre-Trade Risk Check ─────────────────────────────────────────────

    def check_order(self, token: str, trade_usd: float, signal_type: str = "BUY") -> dict:
        """Check if a trade should be allowed.

        Returns:
            {"allowed": True/False, "reason": str, "capped_usd": float}
        """
        if not self.enabled:
            return {"allowed": True, "reason": "risk manager disabled", "capped_usd": trade_usd}

        # Sells always allowed (closing risk)
        if signal_type != "BUY":
            return {"allowed": True, "reason": "sells exempt", "capped_usd": trade_usd}

        with self._lock:
            self._check_day_reset()
            self._sync_pnl()

            # 1. Circuit breaker check
            if self._tripped:
                return {
                    "allowed": False,
                    "reason": f"Circuit breaker tripped at {self._tripped_at}",
                    "capped_usd": 0,
                }

            # 2. Daily loss limit
            if self._realized_pnl <= -self.max_daily_loss_usd:
                self._trip_breaker(f"daily loss ${self._realized_pnl:.2f} exceeds limit ${self.max_daily_loss_usd}")
                return {
                    "allowed": False,
                    "reason": f"Daily loss limit hit: ${self._realized_pnl:.2f} / -${self.max_daily_loss_usd:.2f}",
                    "capped_usd": 0,
                }

            # 3. Per-token exposure check
            token_upper = token.upper()
            current_exposure = self._token_exposure.get(token_upper, 0.0)
            remaining = self.max_token_exposure_usd - current_exposure
            if remaining <= 0:
                return {
                    "allowed": False,
                    "reason": f"Token exposure limit for {token_upper}: ${current_exposure:.2f} / ${self.max_token_exposure_usd:.2f}",
                    "capped_usd": 0,
                }

            # 4. Cap to single trade max
            capped = min(trade_usd, self.max_single_trade_usd, remaining)

            # 5. Open positions check
            try:
                from app.database import get_position_count
                open_count = get_position_count("open")
                if open_count >= self.max_open_positions:
                    return {
                        "allowed": False,
                        "reason": f"Max open positions reached: {open_count}/{self.max_open_positions}",
                        "capped_usd": 0,
                    }
            except Exception:
                pass

            return {
                "allowed": True,
                "reason": "ok",
                "capped_usd": round(capped, 2),
            }

    def record_trade(self, token: str, trade_usd: float):
        """Record a completed trade for exposure tracking."""
        with self._lock:
            self._check_day_reset()
            self._token_exposure[token.upper()] += trade_usd
            logger.info(
                f"SOL risk: recorded ${trade_usd:.2f} for {token.upper()}, "
                f"total exposure: ${self._token_exposure[token.upper()]:.2f}"
            )

    def _trip_breaker(self, reason: str):
        self._tripped = True
        self._tripped_at = datetime.now(timezone.utc)
        logger.critical(f"SOL CIRCUIT BREAKER TRIPPED: {reason}")

    def reset(self):
        """Manual reset (e.g., from dashboard or Telegram)."""
        with self._lock:
            self._tripped = False
            self._tripped_at = None
            self._token_exposure.clear()
            self._realized_pnl = 0.0
            logger.info("SOL risk manager manually reset")

    # ── Status ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            self._check_day_reset()
            self._sync_pnl()

            token_breakdown = {
                token: {
                    "exposure_usd": round(exp, 2),
                    "limit_usd": self.max_token_exposure_usd,
                    "remaining_usd": round(self.max_token_exposure_usd - exp, 2),
                    "usage_pct": round(exp / self.max_token_exposure_usd * 100, 1) if self.max_token_exposure_usd > 0 else 0,
                }
                for token, exp in self._token_exposure.items()
            }

            return {
                "enabled": self.enabled,
                "tripped": self._tripped,
                "tripped_at": self._tripped_at.isoformat() if self._tripped_at else None,
                "realized_pnl_usd": round(self._realized_pnl, 2),
                "max_daily_loss_usd": self.max_daily_loss_usd,
                "daily_loss_remaining_usd": round(self.max_daily_loss_usd + self._realized_pnl, 2),
                "max_single_trade_usd": self.max_single_trade_usd,
                "max_token_exposure_usd": self.max_token_exposure_usd,
                "max_open_positions": self.max_open_positions,
                "token_exposure": token_breakdown,
            }


# ── Singleton ────────────────────────────────────────────────────────────────

_instance: Optional[SolRiskManager] = None


def get_sol_risk_manager() -> SolRiskManager:
    global _instance
    if _instance is None:
        _instance = SolRiskManager()
    return _instance
