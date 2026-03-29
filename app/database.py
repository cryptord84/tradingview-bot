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
    """)
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
    return {
        "total_trades": row["total_trades"] or 0,
        "winning_trades": row["winning_trades"] or 0,
        "losing_trades": row["losing_trades"] or 0,
        "total_pnl_usd": row["total_pnl_usd"] or 0.0,
        "avg_trade_size_sol": row["avg_trade_size_sol"] or 0.0,
        "last_trade_time": row["last_trade_time"],
        "today_pnl_usd": today_row["today_pnl"] or 0.0,
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


def get_recent_signal_hash() -> Optional[str]:
    """Get the most recent signal hash for duplicate detection."""
    conn = get_db()
    row = conn.execute(
        "SELECT raw_payload FROM signals_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row["raw_payload"] if row else None
