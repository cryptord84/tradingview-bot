"""SQLite database for trade logging and tax compliance."""

import csv
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from app.config import get

DB_PATH = Path(get("database", "path", "data/trades.db"))
CSV_DIR = Path(get("database", "csv_backup_dir", "data/csv_backups"))


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tx_id TEXT,
            signal_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            amount_sol REAL NOT NULL DEFAULT 0,
            price_usd REAL NOT NULL DEFAULT 0,
            fees_sol REAL NOT NULL DEFAULT 0,
            leverage INTEGER NOT NULL DEFAULT 1,
            wallet_address TEXT,
            confidence_score INTEGER DEFAULT 0,
            claude_reasoning TEXT,
            pnl_usd REAL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS signals_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            source_ip TEXT,
            processed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT PRIMARY KEY,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            losing_trades INTEGER DEFAULT 0,
            total_pnl_usd REAL DEFAULT 0,
            start_balance_sol REAL DEFAULT 0,
            end_balance_sol REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tx_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            token TEXT NOT NULL DEFAULT 'USDC',
            fee_sol REAL NOT NULL DEFAULT 0,
            tx_signature TEXT,
            status TEXT NOT NULL DEFAULT 'success',
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_wallet_tx_timestamp ON wallet_transactions(timestamp);

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'long',
            entry_price REAL NOT NULL,
            exit_price REAL,
            amount_sol REAL NOT NULL DEFAULT 0,
            amount_usdc REAL NOT NULL DEFAULT 0,
            tp_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            trail_sl_price REAL,
            status TEXT NOT NULL DEFAULT 'open',
            pnl_usdc REAL,
            pnl_percent REAL,
            entry_tx TEXT,
            exit_tx TEXT,
            timeframe TEXT,
            confidence INTEGER DEFAULT 0,
            atr REAL,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_created ON positions(created_at);

        CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            version TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            symbol TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            initial_capital REAL,
            net_profit_usd REAL,
            net_profit_pct REAL,
            gross_profit REAL,
            gross_loss REAL,
            profit_factor REAL,
            total_trades INTEGER,
            winning_trades INTEGER,
            losing_trades INTEGER,
            win_rate REAL,
            avg_win REAL,
            avg_loss REAL,
            win_loss_ratio REAL,
            largest_win REAL,
            largest_loss REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            sortino_ratio REAL,
            long_trades INTEGER,
            long_win_rate REAL,
            long_pnl REAL,
            short_trades INTEGER,
            short_win_rate REAL,
            short_pnl REAL,
            source_file TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'tested'
        );

        CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests(strategy_name, version);

        CREATE TABLE IF NOT EXISTS kalshi_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            order_id TEXT,
            ticker TEXT NOT NULL,
            event_ticker TEXT,
            title TEXT,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            price_cents INTEGER NOT NULL DEFAULT 0,
            total_cost_cents INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            client_order_id TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS kalshi_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            ticker TEXT NOT NULL,
            event_ticker TEXT,
            title TEXT,
            side TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            avg_price_cents INTEGER NOT NULL DEFAULT 0,
            current_price_cents INTEGER,
            invested_cents INTEGER NOT NULL DEFAULT 0,
            pnl_cents INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            settled_payout_cents INTEGER,
            close_date TEXT,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_kalshi_trades_ticker ON kalshi_trades(ticker);
        CREATE INDEX IF NOT EXISTS idx_kalshi_trades_ts ON kalshi_trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_kalshi_positions_status ON kalshi_positions(status);

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            amount_usd REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            fees_usd REAL NOT NULL DEFAULT 0,
            balance_after REAL NOT NULL DEFAULT 0,
            signal_confidence INTEGER DEFAULT 0,
            claude_decision TEXT,
            pnl_usd REAL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
        CREATE INDEX IF NOT EXISTS idx_paper_trades_ts ON paper_trades(timestamp);

        """)
    # Migration: add trail_sl_price column if not exists
    try:
        conn.execute("SELECT trail_sl_price FROM positions LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE positions ADD COLUMN trail_sl_price REAL")
    conn.commit()
    conn.close()


def insert_trade(trade: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO trades
        (timestamp, tx_id, signal_type, symbol, action, amount_sol, price_usd,
         fees_sol, leverage, wallet_address, confidence_score, claude_reasoning, pnl_usd, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.get("timestamp", datetime.utcnow().isoformat()),
            trade.get("tx_id"),
            trade["signal_type"],
            trade["symbol"],
            trade["action"],
            trade.get("amount_sol", 0),
            trade.get("price_usd", 0),
            trade.get("fees_sol", 0),
            trade.get("leverage", 1),
            trade.get("wallet_address", ""),
            trade.get("confidence_score", 0),
            trade.get("claude_reasoning", ""),
            trade.get("pnl_usd"),
            trade.get("notes", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def log_signal(payload: str, source_ip: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO signals_log (timestamp, raw_payload, source_ip) VALUES (?, ?, ?)",
        (datetime.utcnow().isoformat(), payload, source_ip),
    )
    conn.commit()
    conn.close()


def get_trades(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_today_trades() -> list[dict]:
    conn = get_db()
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC",
        (today,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_db()
    row = conn.execute(
        """SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
            COALESCE(AVG(amount_sol), 0) as avg_trade_size_sol,
            MAX(timestamp) as last_trade_time
        FROM trades WHERE action = 'EXECUTE'"""
    ).fetchone()

    today = date.today().isoformat()
    today_row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) as today_pnl FROM trades WHERE timestamp >= ? AND action = 'EXECUTE'",
        (today,),
    ).fetchone()

    conn.close()
    total = row["total_trades"] or 0
    winning = row["winning_trades"] or 0
    return {
        "total_trades": total,
        "winning_trades": winning,
        "losing_trades": row["losing_trades"] or 0,
        "total_pnl_usd": row["total_pnl_usd"] or 0.0,
        "avg_trade_size_sol": row["avg_trade_size_sol"] or 0.0,
        "last_trade_time": row["last_trade_time"],
        "today_pnl_usd": today_row["today_pnl"] or 0.0,
        "win_rate": round(winning / total * 100, 1) if total > 0 else 0.0,
    }


def export_csv(output_path: Optional[str] = None) -> str:
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = str(CSV_DIR / f"trades_{date.today().isoformat()}.csv")
    conn = get_db()
    rows = conn.execute("SELECT * FROM trades ORDER BY timestamp").fetchall()
    conn.close()

    if not rows:
        return output_path

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow(tuple(row))
    return output_path


def log_wallet_tx(
    tx_type: str,
    direction: str,
    amount: float,
    token: str = "USDC",
    fee_sol: float = 0.000005,
    tx_signature: str = "",
    status: str = "success",
    notes: str = "",
) -> int:
    """Log a wallet transaction (Kamino deposit/withdraw, swap, transfer)."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO wallet_transactions
        (timestamp, tx_type, direction, amount, token, fee_sol, tx_signature, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            tx_type,
            direction,
            amount,
            token,
            fee_sol,
            tx_signature,
            status,
            notes,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_wallet_transactions(limit: int = 50) -> list[dict]:
    """Get recent wallet transactions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM wallet_transactions ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_kamino_net_deposited() -> float:
    """Get net USDC deposited into Kamino (deposits - withdrawals)."""
    conn = get_db()
    row = conn.execute(
        """SELECT
            COALESCE(SUM(CASE WHEN tx_type = 'kamino_deposit' AND status = 'success' THEN amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN tx_type = 'kamino_withdraw' AND status = 'success' THEN amount ELSE 0 END), 0)
            AS net_deposited
        FROM wallet_transactions"""
    ).fetchone()
    conn.close()
    return row["net_deposited"] if row else 0.0


def insert_position(pos: dict) -> int:
    """Insert a new position record."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO positions
        (created_at, symbol, direction, entry_price, amount_sol, amount_usdc,
         tp_price, sl_price, status, entry_tx, timeframe, confidence, atr, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pos.get("created_at", datetime.utcnow().isoformat()),
            pos["symbol"],
            pos.get("direction", "long"),
            pos["entry_price"],
            pos.get("amount_sol", 0),
            pos.get("amount_usdc", 0),
            pos["tp_price"],
            pos["sl_price"],
            pos.get("status", "open"),
            pos.get("entry_tx", ""),
            pos.get("timeframe", ""),
            pos.get("confidence", 0),
            pos.get("atr"),
            pos.get("notes", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_open_positions() -> list[dict]:
    """Get all open positions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_positions(limit: int = 50) -> list[dict]:
    """Get recent positions (all statuses)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_position(
    position_id: int,
    exit_price: float,
    exit_tx: str,
    status: str,
    pnl_usdc: float,
    pnl_percent: float,
) -> None:
    """Close a position with exit details."""
    conn = get_db()
    conn.execute(
        """UPDATE positions
        SET closed_at=?, exit_price=?, exit_tx=?, status=?, pnl_usdc=?, pnl_percent=?
        WHERE id=?""",
        (
            datetime.utcnow().isoformat(),
            exit_price,
            exit_tx,
            status,
            pnl_usdc,
            pnl_percent,
            position_id,
        ),
    )
    conn.commit()
    conn.close()


def get_position_analytics() -> dict:
    """Get aggregated position analytics for the dashboard."""
    conn = get_db()

    # Overall closed position stats
    overall = conn.execute("""
        SELECT
            COUNT(*) as total_closed,
            SUM(CASE WHEN status = 'closed_tp' THEN 1 ELSE 0 END) as tp_wins,
            SUM(CASE WHEN status = 'closed_sl' THEN 1 ELSE 0 END) as sl_losses,
            SUM(CASE WHEN status = 'closed_manual' THEN 1 ELSE 0 END) as manual_closes,
            COALESCE(SUM(pnl_usdc), 0) as total_pnl_usdc,
            COALESCE(AVG(pnl_usdc), 0) as avg_pnl_usdc,
            COALESCE(AVG(pnl_percent), 0) as avg_pnl_percent,
            COALESCE(AVG(CASE WHEN pnl_usdc > 0 THEN pnl_usdc END), 0) as avg_win_usdc,
            COALESCE(AVG(CASE WHEN pnl_usdc < 0 THEN pnl_usdc END), 0) as avg_loss_usdc,
            COALESCE(MAX(pnl_usdc), 0) as best_trade_usdc,
            COALESCE(MIN(pnl_usdc), 0) as worst_trade_usdc,
            COALESCE(AVG(amount_sol), 0) as avg_size_sol,
            COALESCE(AVG(
                CASE WHEN closed_at IS NOT NULL AND created_at IS NOT NULL
                THEN (julianday(closed_at) - julianday(created_at)) * 24
                END
            ), 0) as avg_hold_hours
        FROM positions WHERE status != 'open'
    """).fetchone()

    # Per-timeframe breakdown (maps to indicator)
    by_timeframe = conn.execute("""
        SELECT
            COALESCE(timeframe, 'unknown') as timeframe,
            COUNT(*) as total,
            SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(pnl_usdc), 0) as pnl_usdc,
            COALESCE(AVG(pnl_percent), 0) as avg_pnl_pct
        FROM positions WHERE status != 'open'
        GROUP BY timeframe
        ORDER BY pnl_usdc DESC
    """).fetchall()

    # Equity curve: cumulative P&L over time (closed positions chronologically)
    equity_curve = conn.execute("""
        SELECT
            closed_at as ts,
            pnl_usdc,
            symbol,
            status
        FROM positions
        WHERE status != 'open' AND closed_at IS NOT NULL
        ORDER BY closed_at ASC
    """).fetchall()

    # Monthly breakdown
    monthly = conn.execute("""
        SELECT
            strftime('%Y-%m', closed_at) as month,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(pnl_usdc), 0) as pnl_usdc
        FROM positions
        WHERE status != 'open' AND closed_at IS NOT NULL
        GROUP BY month
        ORDER BY month ASC
    """).fetchall()

    # Open position count
    open_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE status = 'open'"
    ).fetchone()

    conn.close()

    # Build equity curve with running total
    curve_data = []
    cumulative = 0.0
    for row in equity_curve:
        cumulative += row["pnl_usdc"]
        curve_data.append({
            "ts": row["ts"],
            "pnl": row["pnl_usdc"],
            "cumulative": round(cumulative, 2),
            "symbol": row["symbol"],
            "status": row["status"],
        })

    o = {k: (v if v is not None else 0) for k, v in dict(overall).items()} if overall else {}
    win_count = o.get("tp_wins", 0) + o.get("manual_closes", 0)
    total = o.get("total_closed", 0)
    # Win/loss ratio
    avg_win = abs(o.get("avg_win_usdc", 0))
    avg_loss = abs(o.get("avg_loss_usdc", 0) or 1)
    win_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0

    return {
        "total_closed": total,
        "open_count": open_count["cnt"] if open_count else 0,
        "tp_wins": o.get("tp_wins", 0),
        "sl_losses": o.get("sl_losses", 0),
        "manual_closes": o.get("manual_closes", 0),
        "win_rate": round((win_count / total) * 100, 1) if total > 0 else 0,
        "total_pnl_usdc": round(o.get("total_pnl_usdc", 0), 2),
        "avg_pnl_usdc": round(o.get("avg_pnl_usdc", 0), 2),
        "avg_pnl_percent": round(o.get("avg_pnl_percent", 0), 2),
        "avg_win_usdc": round(o.get("avg_win_usdc", 0), 2),
        "avg_loss_usdc": round(o.get("avg_loss_usdc", 0), 2),
        "win_loss_ratio": win_loss_ratio,
        "best_trade_usdc": round(o.get("best_trade_usdc", 0), 2),
        "worst_trade_usdc": round(o.get("worst_trade_usdc", 0), 2),
        "avg_size_sol": round(o.get("avg_size_sol", 0), 4),
        "avg_hold_hours": round(o.get("avg_hold_hours", 0), 1),
        "by_timeframe": [dict(r) for r in by_timeframe],
        "equity_curve": curve_data,
        "monthly": [dict(r) for r in monthly],
    }


def update_trail_sl(position_id: int, trail_sl_price: float) -> None:
    """Update the trailing stop-loss price for a position."""
    conn = get_db()
    conn.execute(
        "UPDATE positions SET trail_sl_price = ? WHERE id = ?",
        (trail_sl_price, position_id),
    )
    conn.commit()
    conn.close()


def get_position_count(status: str = "open") -> int:
    """Count positions by status."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE status = ?",
        (status,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def insert_backtest(bt: dict) -> int:
    """Insert a backtest result record."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO backtests
        (created_at, strategy_name, version, timeframe, symbol,
         period_start, period_end, initial_capital,
         net_profit_usd, net_profit_pct, gross_profit, gross_loss, profit_factor,
         total_trades, winning_trades, losing_trades, win_rate,
         avg_win, avg_loss, win_loss_ratio, largest_win, largest_loss,
         max_drawdown, sharpe_ratio, sortino_ratio,
         long_trades, long_win_rate, long_pnl,
         short_trades, short_win_rate, short_pnl,
         source_file, notes, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bt.get("created_at", datetime.utcnow().isoformat()),
            bt["strategy_name"],
            bt["version"],
            bt["timeframe"],
            bt["symbol"],
            bt.get("period_start"),
            bt.get("period_end"),
            bt.get("initial_capital"),
            bt.get("net_profit_usd"),
            bt.get("net_profit_pct"),
            bt.get("gross_profit"),
            bt.get("gross_loss"),
            bt.get("profit_factor"),
            bt.get("total_trades"),
            bt.get("winning_trades"),
            bt.get("losing_trades"),
            bt.get("win_rate"),
            bt.get("avg_win"),
            bt.get("avg_loss"),
            bt.get("win_loss_ratio"),
            bt.get("largest_win"),
            bt.get("largest_loss"),
            bt.get("max_drawdown"),
            bt.get("sharpe_ratio"),
            bt.get("sortino_ratio"),
            bt.get("long_trades"),
            bt.get("long_win_rate"),
            bt.get("long_pnl"),
            bt.get("short_trades"),
            bt.get("short_win_rate"),
            bt.get("short_pnl"),
            bt.get("source_file"),
            bt.get("notes"),
            bt.get("status", "tested"),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_backtests(strategy: str = None, limit: int = 50) -> list[dict]:
    """Get backtest results, optionally filtered by strategy name."""
    conn = get_db()
    if strategy:
        rows = conn.execute(
            "SELECT * FROM backtests WHERE strategy_name = ? ORDER BY created_at DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM backtests ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_backtest(backtest_id: int) -> bool:
    """Delete a backtest record."""
    conn = get_db()
    conn.execute("DELETE FROM backtests WHERE id = ?", (backtest_id,))
    conn.commit()
    conn.close()
    return True


def insert_kalshi_trade(trade: dict) -> int:
    """Insert a Kalshi trade record."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO kalshi_trades
        (timestamp, order_id, ticker, event_ticker, title, side, action,
         count, price_cents, total_cost_cents, status, client_order_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.get("timestamp", datetime.utcnow().isoformat()),
            trade.get("order_id", ""),
            trade["ticker"],
            trade.get("event_ticker", ""),
            trade.get("title", ""),
            trade["side"],
            trade["action"],
            trade.get("count", 0),
            trade.get("price_cents", 0),
            trade.get("total_cost_cents", 0),
            trade.get("status", "pending"),
            trade.get("client_order_id", ""),
            trade.get("notes", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_kalshi_trades(limit: int = 50) -> list[dict]:
    """Get recent Kalshi trades."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM kalshi_trades ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_kalshi_position(pos: dict) -> int:
    """Insert a Kalshi position record."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO kalshi_positions
        (opened_at, ticker, event_ticker, title, side, count,
         avg_price_cents, invested_cents, status, close_date, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pos.get("opened_at", datetime.utcnow().isoformat()),
            pos["ticker"],
            pos.get("event_ticker", ""),
            pos.get("title", ""),
            pos["side"],
            pos.get("count", 0),
            pos.get("avg_price_cents", 0),
            pos.get("invested_cents", 0),
            pos.get("status", "open"),
            pos.get("close_date", ""),
            pos.get("notes", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_kalshi_positions(status: str = "open") -> list[dict]:
    """Get Kalshi positions by status."""
    conn = get_db()
    if status == "all":
        rows = conn.execute(
            "SELECT * FROM kalshi_positions ORDER BY opened_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kalshi_positions WHERE status = ? ORDER BY opened_at DESC",
            (status,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_kalshi_position(
    position_id: int,
    pnl_cents: int,
    settled_payout_cents: int = 0,
    status: str = "closed",
) -> None:
    """Close a Kalshi position."""
    conn = get_db()
    conn.execute(
        """UPDATE kalshi_positions
        SET closed_at=?, pnl_cents=?, settled_payout_cents=?, status=?
        WHERE id=?""",
        (
            datetime.utcnow().isoformat(),
            pnl_cents,
            settled_payout_cents,
            status,
            position_id,
        ),
    )
    conn.commit()
    conn.close()


def get_kalshi_stats() -> dict:
    """Get aggregated Kalshi trading stats."""
    conn = get_db()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_positions,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as winning,
            SUM(CASE WHEN pnl_cents < 0 THEN 1 ELSE 0 END) as losing,
            COALESCE(SUM(pnl_cents), 0) as total_pnl_cents,
            COALESCE(SUM(invested_cents), 0) as total_invested_cents,
            COALESCE(SUM(settled_payout_cents), 0) as total_payout_cents
        FROM kalshi_positions
    """).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_recent_signal_hash() -> Optional[str]:
    """Get the most recent signal hash for duplicate detection."""
    conn = get_db()
    row = conn.execute(
        "SELECT raw_payload FROM signals_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row["raw_payload"] if row else None
