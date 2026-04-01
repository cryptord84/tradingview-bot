"""Paper trading service - simulates trades without real execution."""

import logging
from datetime import datetime
from typing import Optional

from app.config import get
from app.database import get_db

logger = logging.getLogger("bot.paper")

_paper_trader: Optional["PaperTradingService"] = None


def get_paper_trader() -> "PaperTradingService":
    """Singleton accessor for PaperTradingService."""
    global _paper_trader
    if _paper_trader is None:
        _paper_trader = PaperTradingService()
    return _paper_trader


class PaperTradingService:
    """Simulates trade execution against a virtual portfolio."""

    def __init__(self):
        cfg = get("paper_trading") or {}
        self.starting_balance = cfg.get("starting_balance_usd", 1000.0)
        self.slippage_bps = cfg.get("simulated_slippage_bps", 10)
        self.fee_bps = cfg.get("simulated_fee_bps", 5)
        self._init_portfolio()

    def _init_portfolio(self):
        """Load portfolio state from DB or initialize fresh."""
        conn = get_db()
        # Check for existing open positions and cash balance from last trade
        last_trade = conn.execute(
            "SELECT balance_after FROM paper_trades ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if last_trade:
            self.cash = last_trade["balance_after"]
        else:
            self.cash = self.starting_balance

        # Load open positions from DB
        self.positions = {}
        open_rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open'"
        ).fetchall()
        for row in open_rows:
            self.positions[row["symbol"]] = {
                "id": row["id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "amount_usd": row["amount_usd"],
                "entry_price": row["price"],
                "timestamp": row["timestamp"],
                "confidence": row["signal_confidence"],
            }
        conn.close()
        logger.info(
            f"Paper portfolio loaded: ${self.cash:.2f} cash, "
            f"{len(self.positions)} open positions"
        )

    @property
    def enabled(self) -> bool:
        cfg = get("paper_trading") or {}
        return cfg.get("enabled", False)

    def execute_paper_trade(
        self,
        signal,
        decision: dict,
        token_price: float,
        trade_amount_usd: float,
    ) -> dict:
        """Simulate a trade execution.

        Args:
            signal: WebhookSignal model
            decision: Claude decision dict
            token_price: Current token price in USD
            trade_amount_usd: Dollar amount to trade

        Returns:
            dict with trade details
        """
        side = signal.signal_type.value
        symbol = signal.symbol

        # Simulate slippage
        slippage_mult = self.slippage_bps / 10000
        if side == "BUY":
            fill_price = token_price * (1 + slippage_mult)
        else:
            fill_price = token_price * (1 - slippage_mult)

        # Simulate fees
        fees_usd = trade_amount_usd * (self.fee_bps / 10000)

        # Cap trade to available cash
        if side == "BUY":
            max_trade = self.cash - fees_usd
            if trade_amount_usd > max_trade:
                trade_amount_usd = max(0, max_trade)
                if trade_amount_usd <= 0:
                    logger.warning("Paper trade rejected: insufficient virtual cash")
                    return {"status": "rejected", "reason": "insufficient_cash"}

        # Update virtual balance
        if side == "BUY":
            self.cash -= (trade_amount_usd + fees_usd)
        else:
            self.cash += (trade_amount_usd - fees_usd)

        # Record in DB
        conn = get_db()
        status = "open" if side == "BUY" else "closed"
        cur = conn.execute(
            """INSERT INTO paper_trades
            (timestamp, symbol, side, amount_usd, price, fees_usd,
             balance_after, signal_confidence, claude_decision, pnl_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                symbol,
                side,
                trade_amount_usd,
                fill_price,
                fees_usd,
                self.cash,
                signal.confidence_score,
                decision.get("reasoning", ""),
                None,
                status,
            ),
        )
        conn.commit()
        trade_id = cur.lastrowid
        conn.close()

        # Track position in memory
        if side == "BUY":
            self.positions[symbol] = {
                "id": trade_id,
                "symbol": symbol,
                "side": side,
                "amount_usd": trade_amount_usd,
                "entry_price": fill_price,
                "timestamp": datetime.utcnow().isoformat(),
                "confidence": signal.confidence_score,
            }

        logger.info(
            f"[PAPER] {side} {symbol}: ${trade_amount_usd:.2f} @ ${fill_price:.4f} "
            f"(fees: ${fees_usd:.4f}, balance: ${self.cash:.2f})"
        )

        return {
            "status": "paper_executed",
            "trade_id": trade_id,
            "side": side,
            "symbol": symbol,
            "amount_usd": trade_amount_usd,
            "fill_price": fill_price,
            "fees_usd": fees_usd,
            "balance_after": self.cash,
        }

    def close_paper_position(
        self, ticker: str, price: float, reason: str = "manual"
    ) -> dict:
        """Close an open paper position.

        Args:
            ticker: Symbol to close
            price: Current market price
            reason: Reason for close (tp, sl, manual)

        Returns:
            dict with close details
        """
        if ticker not in self.positions:
            return {"status": "error", "reason": f"No open position for {ticker}"}

        pos = self.positions[ticker]
        entry_price = pos["entry_price"]
        amount_usd = pos["amount_usd"]

        # Simulate slippage on close
        slippage_mult = self.slippage_bps / 10000
        fill_price = price * (1 - slippage_mult)  # Selling, so slippage hurts

        # Calculate P&L
        price_change_pct = (fill_price - entry_price) / entry_price
        pnl_usd = amount_usd * price_change_pct

        # Fees on close
        fees_usd = amount_usd * (self.fee_bps / 10000)
        pnl_usd -= fees_usd

        # Update cash
        self.cash += amount_usd + pnl_usd

        # Update the open trade record
        conn = get_db()
        conn.execute(
            "UPDATE paper_trades SET status = 'closed', pnl_usd = ? WHERE id = ?",
            (pnl_usd, pos["id"]),
        )

        # Insert a CLOSE record
        conn.execute(
            """INSERT INTO paper_trades
            (timestamp, symbol, side, amount_usd, price, fees_usd,
             balance_after, signal_confidence, claude_decision, pnl_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                ticker,
                "CLOSE",
                amount_usd,
                fill_price,
                fees_usd,
                self.cash,
                pos.get("confidence", 0),
                f"Close: {reason}",
                pnl_usd,
                "closed",
            ),
        )
        conn.commit()
        conn.close()

        # Remove from memory
        del self.positions[ticker]

        logger.info(
            f"[PAPER] CLOSE {ticker}: ${amount_usd:.2f} @ ${fill_price:.4f} "
            f"P&L: ${pnl_usd:.2f} ({reason}), balance: ${self.cash:.2f}"
        )

        return {
            "status": "closed",
            "symbol": ticker,
            "entry_price": entry_price,
            "exit_price": fill_price,
            "pnl_usd": round(pnl_usd, 4),
            "fees_usd": round(fees_usd, 4),
            "balance_after": round(self.cash, 2),
            "reason": reason,
        }

    def get_paper_portfolio(self, current_prices: dict = None) -> dict:
        """Get current paper portfolio state.

        Args:
            current_prices: Optional dict of {symbol: price} for unrealized P&L

        Returns:
            dict with cash, positions, total equity
        """
        current_prices = current_prices or {}
        positions_list = []
        total_unrealized = 0.0

        for symbol, pos in self.positions.items():
            current_price = current_prices.get(symbol, pos["entry_price"])
            price_change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
            unrealized_pnl = pos["amount_usd"] * price_change_pct
            total_unrealized += unrealized_pnl

            positions_list.append({
                "symbol": symbol,
                "side": pos["side"],
                "amount_usd": round(pos["amount_usd"], 2),
                "entry_price": round(pos["entry_price"], 4),
                "current_price": round(current_price, 4),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "pnl_pct": round(price_change_pct * 100, 2),
                "opened": pos["timestamp"],
            })

        total_equity = self.cash + sum(p["amount_usd"] for p in positions_list) + total_unrealized

        return {
            "cash": round(self.cash, 2),
            "positions": positions_list,
            "total_unrealized_pnl": round(total_unrealized, 2),
            "total_equity": round(total_equity, 2),
            "starting_balance": self.starting_balance,
            "total_return_pct": round((total_equity - self.starting_balance) / self.starting_balance * 100, 2),
        }

    def get_paper_trades(self, limit: int = 50) -> list[dict]:
        """Get paper trade history."""
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_paper_stats(self) -> dict:
        """Get paper trading statistics."""
        conn = get_db()

        # Overall stats from closed trades
        row = conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losing_trades,
                COALESCE(SUM(pnl_usd), 0) as total_pnl,
                COALESCE(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0) as avg_win,
                COALESCE(AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END), 0) as avg_loss,
                COALESCE(MAX(pnl_usd), 0) as best_trade,
                COALESCE(MIN(pnl_usd), 0) as worst_trade
            FROM paper_trades
            WHERE status = 'closed' AND side != 'BUY'
        """).fetchone()

        # Get balance history for drawdown calculation
        balances = conn.execute(
            "SELECT balance_after FROM paper_trades ORDER BY id ASC"
        ).fetchall()

        conn.close()

        total = row["total_trades"] or 0
        wins = row["winning_trades"] or 0
        total_pnl = row["total_pnl"] or 0.0

        # Max drawdown calculation
        max_drawdown = 0.0
        peak = self.starting_balance
        for b in balances:
            bal = b["balance_after"]
            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # Simple Sharpe estimate (annualized, assuming ~252 trading days)
        avg_return = (total_pnl / total) if total > 0 else 0
        # Use avg_win and avg_loss to estimate volatility
        avg_win = row["avg_win"] or 0
        avg_loss = abs(row["avg_loss"] or 0)
        vol_estimate = ((avg_win + avg_loss) / 2) if (avg_win + avg_loss) > 0 else 1
        sharpe_estimate = round((avg_return / vol_estimate) * (252 ** 0.5), 2) if vol_estimate > 0 else 0

        return {
            "enabled": self.enabled,
            "starting_balance": self.starting_balance,
            "current_equity": round(self.cash + sum(p["amount_usd"] for p in self.positions.values()), 2),
            "cash": round(self.cash, 2),
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": row["losing_trades"] or 0,
            "win_rate": round((wins / total * 100), 1) if total > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round((self.cash - self.starting_balance) / self.starting_balance * 100, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(row["avg_loss"] or 0, 2),
            "best_trade": round(row["best_trade"] or 0, 2),
            "worst_trade": round(row["worst_trade"] or 0, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe_estimate": sharpe_estimate,
            "open_positions": len(self.positions),
        }

    def reset(self):
        """Reset paper portfolio to starting balance."""
        conn = get_db()
        conn.execute("DELETE FROM paper_trades")
        conn.commit()
        conn.close()

        self.cash = self.starting_balance
        self.positions = {}
        logger.info(f"[PAPER] Portfolio reset to ${self.starting_balance:.2f}")
        return {"status": "reset", "balance": self.starting_balance}
