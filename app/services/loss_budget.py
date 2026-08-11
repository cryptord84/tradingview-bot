"""Cross-lane rolling loss budget — the hard stop on autonomous operation.

Why this exists (2026-08-10): the user granted autonomous authority over
position sizing and live strategy deployment, bounded by a loss budget. That
bound has to live in code, because the agent making the sizing decisions is not
running most of the time — a limit enforced only by an agent's judgment is not
a limit.

How it differs from the two breakers already in the codebase:

  sol_risk_manager    daily window, Solana lane only, in-memory latch
  kalshi_risk_manager per-category daily cap, in-memory latch
  this                rolling multi-day window, ALL lanes, stateless

Two design choices worth keeping:

1. STATELESS. Realized P&L is recomputed from the `positions` table on every
   call rather than latched in memory. Both existing breakers lose their trip
   state on restart (noted for kalshi in review_list 2026-06-10) — and restarts
   are routine now that the agent performs them, so an in-memory latch is a
   guardrail that any deploy silently removes. Deriving from ground truth every
   time cannot drift and cannot be cleared by a restart.

2. EXITS ARE NEVER BLOCKED. Only entries (BUY) are gated. Blocking a CLOSE
   during a drawdown would strand open positions in exactly the conditions
   where getting out matters most, and would fight position_monitor's TP/SL.

Calibration: worst realized week on record is -$9.62 (2026-W22, 24 trades);
every other week since May is inside -$2.25. The default $25 / 7d is ~2.6x the
worst observed week and ~5.5% of ~$450 tradeable — room to scale size several
fold before it binds, while capping a genuine bleed well short of the account.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get
from app.database import get_db

logger = logging.getLogger("bot.loss_budget")


class LossBudget:
    """Rolling realized-P&L guard across every execution lane."""

    @property
    def _cfg(self) -> dict:
        return get("loss_budget") or {}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    @property
    def window_days(self) -> int:
        return int(self._cfg.get("window_days", 7))

    @property
    def max_loss_usd(self) -> float:
        return float(self._cfg.get("max_loss_usd", 25.0))

    def realized_pnl(self, window_days: Optional[int] = None) -> tuple[float, int]:
        """Realized P&L and trade count over the rolling window, all lanes.

        Reads `positions` (not `trades`) because pnl_usdc there is the settled
        per-position result; the trades table carries one row per signal action
        and would double-count an entry against its own exit.
        """
        days = window_days if window_days is not None else self.window_days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            conn = get_db()
            row = conn.execute(
                """SELECT COALESCE(SUM(pnl_usdc), 0.0), COUNT(*)
                     FROM positions
                    WHERE status != 'open'
                      AND closed_at IS NOT NULL
                      AND closed_at >= ?""",
                (cutoff,),
            ).fetchone()
            conn.close()
            return float(row[0] or 0.0), int(row[1] or 0)
        except Exception as e:
            # Fail OPEN on a read error. A transient DB hiccup must not halt
            # trading — the daily/lane breakers still apply underneath, and a
            # silent full stop is harder to notice than a missed trade.
            logger.error(f"loss budget: P&L read failed ({e}) — allowing trade")
            return 0.0, 0

    def check(self, signal_type: str = "BUY") -> dict:
        """Gate a prospective entry. Returns {allowed, reason, realized, budget}."""
        if not self.enabled:
            return {"allowed": True, "reason": "", "realized": 0.0, "budget": self.max_loss_usd}

        # Exits always pass — see module docstring.
        if str(signal_type).upper() in ("CLOSE", "SELL"):
            return {"allowed": True, "reason": "", "realized": 0.0, "budget": self.max_loss_usd}

        realized, n = self.realized_pnl()
        budget = self.max_loss_usd

        if realized <= -budget:
            reason = (
                f"Loss budget exhausted: ${realized:.2f} realized over the last "
                f"{self.window_days}d ({n} trades) vs -${budget:.2f} limit. "
                f"New entries halted; exits still allowed."
            )
            logger.warning(f"LOSS BUDGET TRIPPED — {reason}")
            return {"allowed": False, "reason": reason, "realized": realized, "budget": budget}

        return {"allowed": True, "reason": "", "realized": realized, "budget": budget}

    def status(self) -> dict:
        """Snapshot for dashboards / health reports."""
        realized, n = self.realized_pnl()
        return {
            "enabled": self.enabled,
            "window_days": self.window_days,
            "budget_usd": self.max_loss_usd,
            "realized_usd": round(realized, 2),
            "trades": n,
            "remaining_usd": round(self.max_loss_usd + min(realized, 0.0), 2),
            "tripped": realized <= -self.max_loss_usd,
        }


_instance: Optional[LossBudget] = None


def get_loss_budget() -> LossBudget:
    global _instance
    if _instance is None:
        _instance = LossBudget()
    return _instance
