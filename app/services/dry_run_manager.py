"""Per-alert dry-run simulation manager.

Each alert (identified by strategy name + token + timeframe) can be toggled
between 'live' and 'dry_run' mode independently.  When an alert fires in
dry_run mode the full Claude decision pipeline still runs but execution is
replaced with a simulated log entry and phantom P&L tracking.

Modes are persisted to a JSON file so they survive restarts.  The dashboard
exposes GET/POST endpoints to view and toggle modes without editing config.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.dryrun")

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_MODES_FILE = _DATA_DIR / "dry_run_modes.json"
_SIM_LOG_FILE = _DATA_DIR / "dry_run_trades.json"


def _alert_key(strategy: str, token: str, timeframe: str) -> str:
    """Canonical key for an alert combo."""
    return f"{strategy}|{token}|{timeframe}".upper()


class DryRunManager:
    """Tracks per-alert live/dry_run mode and simulated trade history."""

    def __init__(self):
        self._lock = Lock()
        # alert_key -> "live" | "dry_run"
        self._modes: dict[str, str] = {}
        # Simulated trade log
        self._sim_trades: list[dict] = []
        # Cumulative simulated P&L
        self._sim_pnl: float = 0.0
        # Stats
        self._sim_count: int = 0
        self._sim_wins: int = 0
        self._sim_losses: int = 0
        # Hourly summary tracking
        self._hourly_received: int = 0
        self._hourly_simulated: int = 0
        self._hour_start: float = time.time()

        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load_modes()
        self._load_sim_trades()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load_modes(self):
        try:
            if _MODES_FILE.exists():
                with open(_MODES_FILE) as f:
                    self._modes = json.load(f)
                logger.info(f"Loaded {len(self._modes)} dry-run mode overrides")
        except Exception as e:
            logger.warning(f"Could not load dry-run modes: {e}")

    def _save_modes(self):
        try:
            with open(_MODES_FILE, "w") as f:
                json.dump(self._modes, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save dry-run modes: {e}")

    def _load_sim_trades(self):
        try:
            if _SIM_LOG_FILE.exists():
                with open(_SIM_LOG_FILE) as f:
                    self._sim_trades = json.load(f)
                # Rebuild stats from history
                for t in self._sim_trades:
                    self._sim_count += 1
                    pnl = t.get("simulated_pnl", 0)
                    self._sim_pnl += pnl
                    if pnl > 0:
                        self._sim_wins += 1
                    elif pnl < 0:
                        self._sim_losses += 1
                logger.info(f"Loaded {len(self._sim_trades)} simulated trades (P&L: ${self._sim_pnl:.2f})")
        except Exception as e:
            logger.warning(f"Could not load sim trades: {e}")

    def _save_sim_trades(self):
        try:
            with open(_SIM_LOG_FILE, "w") as f:
                json.dump(self._sim_trades[-500:], f, indent=2)  # Keep last 500
        except Exception as e:
            logger.warning(f"Could not save sim trades: {e}")

    # ── Mode Management ──────────────────────────────────────────────────

    def get_default_mode(self) -> str:
        """Default mode from config (defaults to 'live')."""
        cfg = get("dry_run") or {}
        return cfg.get("default_mode", "live")

    def get_mode(self, strategy: str, token: str, timeframe: str) -> str:
        """Get mode for a specific alert. Falls back to default."""
        key = _alert_key(strategy, token, timeframe)
        with self._lock:
            return self._modes.get(key, self.get_default_mode())

    def is_dry_run(self, strategy: str, token: str, timeframe: str) -> bool:
        return self.get_mode(strategy, token, timeframe) == "dry_run"

    def set_mode(self, strategy: str, token: str, timeframe: str, mode: str) -> dict:
        """Set mode for a specific alert. mode must be 'live' or 'dry_run'."""
        if mode not in ("live", "dry_run"):
            raise ValueError(f"Invalid mode: {mode}")
        key = _alert_key(strategy, token, timeframe)
        with self._lock:
            old = self._modes.get(key, self.get_default_mode())
            if mode == self.get_default_mode():
                self._modes.pop(key, None)  # Remove override if it matches default
            else:
                self._modes[key] = mode
            self._save_modes()
        logger.info(f"Alert mode changed: {key} {old} -> {mode}")
        return {"key": key, "old_mode": old, "new_mode": mode}

    def get_all_modes(self) -> dict:
        """Return all alert modes including known alerts at default."""
        with self._lock:
            return dict(self._modes)

    def get_all_alerts_with_modes(self) -> list[dict]:
        """Return structured list of all known alerts with their modes."""
        # Combine configured overrides with known alerts from config
        known_alerts = self._get_known_alerts()
        result = []
        with self._lock:
            seen = set()
            # First add all alerts with explicit overrides
            for key, mode in self._modes.items():
                parts = key.split("|")
                if len(parts) == 3:
                    entry = {
                        "key": key,
                        "strategy": parts[0],
                        "token": parts[1],
                        "timeframe": parts[2],
                        "mode": mode,
                    }
                    result.append(entry)
                    seen.add(key)
            # Then add known alerts that don't have overrides
            default = self.get_default_mode()
            for alert in known_alerts:
                key = _alert_key(alert["strategy"], alert["token"], alert["timeframe"])
                if key not in seen:
                    result.append({
                        "key": key,
                        "strategy": alert["strategy"],
                        "token": alert["token"],
                        "timeframe": alert["timeframe"],
                        "mode": default,
                    })
            # Also add any alerts we've seen in sim trades
            for t in self._sim_trades:
                key = _alert_key(
                    t.get("strategy", ""),
                    t.get("token", ""),
                    t.get("timeframe", ""),
                )
                if key and key not in seen and all(p for p in key.split("|")):
                    result.append({
                        "key": key,
                        "strategy": t.get("strategy", ""),
                        "token": t.get("token", ""),
                        "timeframe": t.get("timeframe", ""),
                        "mode": self._modes.get(key, default),
                    })
                    seen.add(key)
        return result

    def _get_known_alerts(self) -> list[dict]:
        """Get known alerts from config."""
        cfg = get("dry_run") or {}
        alerts = cfg.get("known_alerts", [])
        return alerts

    # ── Simulation Logging ───────────────────────────────────────────────

    def log_simulated_trade(
        self,
        strategy: str,
        token: str,
        timeframe: str,
        signal_type: str,
        entry_price: float,
        trade_usd: float,
        confidence: int,
        atr: Optional[float] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        claude_decision: Optional[str] = None,
        claude_reasoning: Optional[str] = None,
    ) -> dict:
        """Log a simulated trade that would have executed in live mode."""
        # Estimate P&L using TP/SL expectation
        # Simple model: assume price hits TP 50% of time based on confidence
        simulated_pnl = 0.0
        expected_rr = None
        if tp_price and sl_price and entry_price > 0:
            tp_dist = abs(tp_price - entry_price)
            sl_dist = abs(entry_price - sl_price)
            expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
            # Use confidence as rough win probability
            win_prob = confidence / 100.0
            expected_gain = trade_usd * (tp_dist / entry_price)
            expected_loss = trade_usd * (sl_dist / entry_price)
            simulated_pnl = (win_prob * expected_gain) - ((1 - win_prob) * expected_loss)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy.upper(),
            "token": token.upper(),
            "timeframe": (timeframe or "").upper(),
            "signal_type": signal_type,
            "entry_price": entry_price,
            "trade_usd": round(trade_usd, 2),
            "confidence": confidence,
            "atr": atr,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "expected_rr": round(expected_rr, 2) if expected_rr else None,
            "simulated_pnl": round(simulated_pnl, 2),
            "claude_decision": claude_decision,
            "claude_reasoning": claude_reasoning,
        }

        with self._lock:
            self._sim_trades.append(entry)
            self._sim_count += 1
            self._sim_pnl += simulated_pnl
            if simulated_pnl > 0:
                self._sim_wins += 1
            elif simulated_pnl < 0:
                self._sim_losses += 1
            self._hourly_simulated += 1
            self._save_sim_trades()

        logger.info(
            f"[DRY-RUN] WOULD EXECUTE {signal_type} {token} via {strategy} {timeframe}\n"
            f"  Entry: ${entry_price:.4f} | Size: ${trade_usd:.2f} | Conf: {confidence}\n"
            f"  TP: ${tp_price:.4f if tp_price else 0} | SL: ${sl_price:.4f if sl_price else 0} | "
            f"R:R: {expected_rr:.2f if expected_rr else '?'}\n"
            f"  Projected P&L: ${simulated_pnl:.2f} | Cumulative: ${self._sim_pnl:.2f}"
        )

        return entry

    def record_signal_received(self):
        """Track that a signal was received (for hourly summary)."""
        with self._lock:
            self._hourly_received += 1

    # ── Status & Summary ─────────────────────────────────────────────────

    def get_hourly_summary(self) -> dict:
        """Get hourly summary and optionally reset counters."""
        now = time.time()
        with self._lock:
            summary = {
                "period_seconds": int(now - self._hour_start),
                "signals_received": self._hourly_received,
                "signals_simulated": self._hourly_simulated,
                "cumulative_sim_pnl": round(self._sim_pnl, 2),
            }
            # Reset if over an hour
            if now - self._hour_start >= 3600:
                self._hourly_received = 0
                self._hourly_simulated = 0
                self._hour_start = now
        return summary

    def get_status(self) -> dict:
        with self._lock:
            win_rate = (
                (self._sim_wins / self._sim_count * 100) if self._sim_count > 0 else 0
            )
            return {
                "default_mode": self.get_default_mode(),
                "overrides_count": len(self._modes),
                "total_simulated_trades": self._sim_count,
                "simulated_pnl": round(self._sim_pnl, 2),
                "sim_wins": self._sim_wins,
                "sim_losses": self._sim_losses,
                "sim_win_rate": round(win_rate, 1),
                "recent_trades": self._sim_trades[-10:][::-1],
            }

    def get_sim_trades(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return self._sim_trades[-limit:][::-1]

    def reset_sim_trades(self):
        """Clear all simulated trade history."""
        with self._lock:
            self._sim_trades.clear()
            self._sim_pnl = 0.0
            self._sim_count = 0
            self._sim_wins = 0
            self._sim_losses = 0
            self._save_sim_trades()
        logger.info("Dry-run simulation history reset")


# ── Singleton ────────────────────────────────────────────────────────────────

_instance: Optional[DryRunManager] = None


def get_dry_run_manager() -> DryRunManager:
    global _instance
    if _instance is None:
        _instance = DryRunManager()
    return _instance
