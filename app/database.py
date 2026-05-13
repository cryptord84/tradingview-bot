"""SQLite database for trade logging and tax compliance."""

import csv
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional

from app.config import get

DB_PATH = Path(get("database", "path", "data/trades.db"))
CSV_DIR = Path(get("database", "csv_backup_dir", "data/csv_backups"))


class TradeReason:
    """Reason tags for trade log rows — drives the dashboard Reason column and
    enables filtering by cause (e.g. 'show me all trail_sl exits')."""
    WEBHOOK = "webhook"                      # BUY/SELL executed from TV alert
    SIGNAL_CLOSE = "signal_close"            # CLOSE alert matched open position
    TP_HIT = "tp_hit"                        # position_monitor: take-profit
    SL_HIT = "sl_hit"                        # position_monitor: stop-loss
    TRAIL_SL = "trail_sl"                    # position_monitor: trailing stop
    MANUAL_CLOSE = "manual_close"            # position_monitor: manual trigger
    CLAUDE_REJECT = "claude_reject"          # Claude said REJECT
    RISK_REJECT = "risk_reject"              # SOL risk manager blocked
    CORRELATION_REJECT = "correlation_reject"  # Correlation cap hit
    LOW_BALANCE = "low_balance"              # Auto-shutdown threshold
    DRY_RUN = "dry_run"                      # Per-alert dry-run simulation
    PAPER = "paper"                          # Paper-trading mode


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
            notes TEXT,
            strategy TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS signals_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            source_ip TEXT,
            processed INTEGER DEFAULT 0,
            -- Disposition fields added 2026-05-13. Every webhook arrival is
            -- logged immediately with disposition='received'; engine updates
            -- to the terminal state when it finishes processing. Closes the
            -- audit-trail gap where skipped/dropped signals were invisible.
            disposition TEXT,                -- received|executed|skipped_*|rejected_*|dropped_*|failed_*
            disposition_reason TEXT,         -- short human-readable explanation
            disposition_at TEXT,             -- when the terminal state was set
            trade_id INTEGER                 -- FK trades.id when disposition=executed
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
            strategy TEXT NOT NULL DEFAULT '',
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

        -- Whale flow capture (2026-05-12). Whale tracker has been scanning
        -- Kalshi fills for weeks but only logging to bot.log. Persisting now
        -- so we can backtest "whale-aware crypto_strikes gate" hypothesis at
        -- the 2026-05-19 Stage 1 retro.
        CREATE TABLE IF NOT EXISTS kalshi_whales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,                 -- Kalshi trade timestamp (created_time)
            ticker TEXT NOT NULL,             -- e.g. KXBTCD-26MAY1216-T80499.99
            event_ticker TEXT,                -- e.g. KXBTCD-26MAY1216
            side TEXT NOT NULL,               -- 'yes' or 'no'
            count INTEGER NOT NULL,
            price_cents INTEGER NOT NULL,
            cost_cents INTEGER NOT NULL,
            trade_id TEXT NOT NULL UNIQUE,    -- dedup on re-scan
            detected_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kalshi_whales_ts ON kalshi_whales(ts);
        CREATE INDEX IF NOT EXISTS idx_kalshi_whales_event_ts ON kalshi_whales(event_ticker, ts);
        CREATE INDEX IF NOT EXISTS idx_kalshi_whales_ticker_ts ON kalshi_whales(ticker, ts);

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

        CREATE TABLE IF NOT EXISTS kamino_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            deposited_usdc REAL NOT NULL DEFAULT 0,
            supply_apy REAL,
            source TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_kamino_snap_ts ON kamino_snapshots(timestamp);

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sol_usd REAL NOT NULL DEFAULT 0,
            usdc_usd REAL NOT NULL DEFAULT 0,
            tokens_usd REAL NOT NULL DEFAULT 0,
            kamino_usd REAL NOT NULL DEFAULT 0,
            kalshi_usd REAL NOT NULL DEFAULT 0,
            total_usd REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_snap_ts ON portfolio_snapshots(timestamp);

        CREATE TABLE IF NOT EXISTS reentry_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT '',
            timeframe TEXT NOT NULL DEFAULT '',
            sl_exit_price REAL NOT NULL,
            target_price REAL NOT NULL,
            sl_distance REAL NOT NULL,
            atr REAL,
            sizing_pct REAL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            triggered_at TEXT,
            position_id INTEGER,
            source_position_id INTEGER,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reentry_status ON reentry_watches(status);

        """)
    # Migration: add trail_sl_price column if not exists
    try:
        conn.execute("SELECT trail_sl_price FROM positions LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE positions ADD COLUMN trail_sl_price REAL")
    # Migration: add amount_usd column to trades if not exists
    try:
        conn.execute("SELECT amount_usd FROM trades LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE trades ADD COLUMN amount_usd REAL DEFAULT 0")
    # Migration: add strategy column to positions if not exists
    try:
        conn.execute("SELECT strategy FROM positions LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy)")
    # Migration: add strategy column to trades if not exists
    try:
        conn.execute("SELECT strategy FROM trades LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT ''")
    # Migration: add reason column to trades if not exists
    try:
        conn.execute("SELECT reason FROM trades LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE trades ADD COLUMN reason TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_reason ON trades(reason)")
    # Migration: add disposition fields to signals_log (2026-05-13 audit trail fix)
    try:
        conn.execute("SELECT disposition FROM signals_log LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE signals_log ADD COLUMN disposition TEXT")
        conn.execute("ALTER TABLE signals_log ADD COLUMN disposition_reason TEXT")
        conn.execute("ALTER TABLE signals_log ADD COLUMN disposition_at TEXT")
        conn.execute("ALTER TABLE signals_log ADD COLUMN trade_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_disposition ON signals_log(disposition)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals_log(timestamp)")
    # Migration: add suggested_leverage and run_id columns to backtests
    try:
        conn.execute("SELECT suggested_leverage FROM backtests LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE backtests ADD COLUMN suggested_leverage REAL DEFAULT 1.0")
    try:
        conn.execute("SELECT run_id FROM backtests LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE backtests ADD COLUMN run_id TEXT")
    try:
        conn.execute("SELECT avg_rr FROM backtests LIMIT 1")
    except Exception:
        conn.execute("ALTER TABLE backtests ADD COLUMN avg_rr REAL DEFAULT 0.0")
    conn.commit()
    conn.close()


def insert_trade(trade: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO trades
        (timestamp, tx_id, signal_type, symbol, action, amount_sol, amount_usd, price_usd,
         fees_sol, leverage, wallet_address, confidence_score, claude_reasoning, pnl_usd, notes, strategy, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.get("timestamp", datetime.utcnow().isoformat()),
            trade.get("tx_id"),
            trade["signal_type"],
            trade["symbol"],
            trade["action"],
            trade.get("amount_sol", 0),
            trade.get("amount_usd", 0),
            trade.get("price_usd", 0),
            trade.get("fees_sol", 0),
            trade.get("leverage", 1),
            trade.get("wallet_address", ""),
            trade.get("confidence_score", 0),
            trade.get("claude_reasoning", ""),
            trade.get("pnl_usd"),
            trade.get("notes", ""),
            trade.get("strategy", ""),
            trade.get("reason"),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def log_signal(payload: str, source_ip: str = "") -> int:
    """Insert a webhook signal row with disposition='received'. Returns the
    new row id so the engine can update the disposition once processing
    completes.

    Audit-trail rule (2026-05-13): every webhook the bot receives gets a
    signals_log row, regardless of whether it executes, gets skipped, or
    fails. The disposition column carries the terminal state."""
    conn = get_db()
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        """INSERT INTO signals_log (timestamp, raw_payload, source_ip,
            disposition, disposition_at)
           VALUES (?, ?, ?, ?, ?)""",
        (now, payload, source_ip, "received", now),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_signal_disposition(signal_log_id: Optional[int], disposition: str,
                               reason: Optional[str] = None,
                               trade_id: Optional[int] = None) -> None:
    """Set the terminal state of a signals_log row. No-op when id is None
    (e.g., when the engine is invoked programmatically without a webhook
    origin). Silent on DB errors so engine flow isn't disrupted by audit
    writes."""
    if not signal_log_id:
        return
    try:
        conn = get_db()
        conn.execute(
            """UPDATE signals_log
               SET disposition = ?, disposition_reason = ?,
                   disposition_at = ?, trade_id = ?,
                   processed = 1
               WHERE id = ?""",
            (disposition, reason, datetime.utcnow().isoformat(),
             trade_id, signal_log_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"update_signal_disposition failed for id={signal_log_id}: {e}")


def get_recent_signals(limit: int = 50,
                        since: Optional[str] = None) -> list[dict]:
    """Return recent signals_log rows newest-first, enriched with the linked
    trade summary when disposition=executed. Used by the dashboard Signal
    Activity panel."""
    conn = get_db()
    args: list = []
    where = "1=1"
    if since:
        where += " AND s.timestamp >= ?"
        args.append(since)
    args.append(limit)
    rows = conn.execute(
        f"""SELECT s.id, s.timestamp, s.raw_payload, s.disposition,
                  s.disposition_reason, s.disposition_at, s.trade_id,
                  t.symbol AS trade_symbol, t.action AS trade_action,
                  t.amount_usd AS trade_amount_usd, t.price_usd AS trade_price_usd,
                  t.pnl_usd AS trade_pnl_usd
           FROM signals_log s
           LEFT JOIN trades t ON t.id = s.trade_id
           WHERE {where}
           ORDER BY s.timestamp DESC
           LIMIT ?""",
        args,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    """Aggregate trade stats. Excludes pre-Apr-24 untagged trades (empty
    strategy field) — those were the 'unknown' bucket whose -$10 JTO outlier
    skewed dashboard P&L. The underlying rows are preserved in the DB for
    audit; only the aggregated views ignore them."""
    conn = get_db()
    row = conn.execute(
        """SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
            COALESCE(AVG(amount_sol), 0) as avg_trade_size_sol,
            MAX(timestamp) as last_trade_time
        FROM trades
        WHERE action = 'EXECUTE'
          AND NULLIF(strategy, '') IS NOT NULL"""
    ).fetchone()

    today = date.today().isoformat()
    today_row = conn.execute(
        """SELECT COALESCE(SUM(pnl_usd), 0) as today_pnl
           FROM trades
           WHERE timestamp >= ?
             AND action = 'EXECUTE'
             AND NULLIF(strategy, '') IS NOT NULL""",
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


def insert_kamino_snapshot(deposited_usdc: float, supply_apy: float = None, source: str = "auto") -> int:
    """Record a Kamino balance snapshot for earnings tracking."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO kamino_snapshots (timestamp, deposited_usdc, supply_apy, source) VALUES (?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), deposited_usdc, supply_apy, source),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_kamino_snapshots(limit: int = 500) -> list[dict]:
    """Return Kamino balance snapshots, newest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM kamino_snapshots ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_kamino_snapshot() -> Optional[dict]:
    """Return the most recent Kamino snapshot, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM kamino_snapshots ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def compute_kamino_earnings(current_balance: Optional[float] = None) -> dict:
    """Compute Kamino yield since the first snapshot (tracking baseline).

    Identity: earnings_since_T0 = current_balance - baseline_balance
                                  - (deposits_since_T0 - withdrawals_since_T0)
    where T0 is the oldest kamino_snapshots row. Wiping kamino_snapshots and
    taking a fresh snapshot establishes a clean baseline — only flows AFTER
    that baseline count toward earnings, so prior wallet_transactions drift
    does not contaminate the number.

    Floored at 0 because Kamino is supply-only — principal can't decline from
    yield alone. A meaningfully negative raw value means wallet_transactions
    drifted from on-chain reality (retry double-counting or missed tx).
    """
    conn = get_db()
    baseline_row = conn.execute(
        "SELECT timestamp, deposited_usdc FROM kamino_snapshots ORDER BY timestamp ASC LIMIT 1"
    ).fetchone()
    if not baseline_row:
        conn.close()
        return {
            "earnings_total": 0.0,
            "raw_earnings": 0.0,
            "total_deposited": 0.0,
            "total_withdrawn": 0.0,
            "current_balance": round(current_balance or 0.0, 4),
            "baseline_balance": 0.0,
            "data_quality": "no_baseline",
            "first": None,
            "last": None,
        }

    baseline_ts = baseline_row["timestamp"]
    baseline_balance = baseline_row["deposited_usdc"] or 0.0

    row = conn.execute(
        """SELECT
            COALESCE(SUM(CASE WHEN tx_type = 'kamino_deposit' AND status = 'success' THEN amount ELSE 0 END), 0) AS deposited,
            COALESCE(SUM(CASE WHEN tx_type = 'kamino_withdraw' AND status = 'success' THEN amount ELSE 0 END), 0) AS withdrawn
           FROM wallet_transactions WHERE timestamp >= ?""",
        (baseline_ts,),
    ).fetchone()
    last_row = conn.execute(
        "SELECT MAX(timestamp) AS last FROM kamino_snapshots"
    ).fetchone()
    conn.close()

    total_deposited = row["deposited"] or 0.0
    total_withdrawn = row["withdrawn"] or 0.0

    if current_balance is None:
        latest = get_latest_kamino_snapshot()
        current_balance = (latest or {}).get("deposited_usdc", 0.0) or 0.0

    raw = current_balance - baseline_balance + total_withdrawn - total_deposited

    # Sanity-cap earnings: the raw reconciliation is vulnerable to retry
    # double-counting in wallet_transactions (a single on-chain tx logged
    # multiple times appears as inflated deposits-withdraws imbalance, which
    # the formula misattributes to yield). Cap at a realistic ceiling so the
    # display can't show more than plausible yield. Real Kamino USDC vault
    # APY has sat in 2-6% range; we use 12% as a generous ceiling × 1.5x
    # safety buffer = 18% effective cap.
    import datetime as _dt
    try:
        baseline_dt = _dt.datetime.fromisoformat(baseline_ts.replace("Z", "+00:00"))
        if baseline_dt.tzinfo is None:
            baseline_dt = baseline_dt.replace(tzinfo=_dt.timezone.utc)
        elapsed_days = max(
            0.0,
            (_dt.datetime.now(_dt.timezone.utc) - baseline_dt).total_seconds() / 86400.0,
        )
    except Exception:
        elapsed_days = 0.0
    CEILING_APY = 0.18  # 12% APY × 1.5 buffer — well above any realistic Kamino yield
    realistic_max = max(0.0, (current_balance or 0.0) * CEILING_APY * elapsed_days / 365.0)

    quality = "ok"
    if raw < -1.0:
        quality = "drift_detected"
    elif realistic_max > 0 and raw > realistic_max:
        quality = "capped"  # raw exceeds realistic yield — capping for display

    earnings = max(0.0, min(raw, realistic_max)) if realistic_max > 0 else max(0.0, raw)

    return {
        "earnings_total": round(earnings, 4),
        "raw_earnings": round(raw, 4),
        "total_deposited": round(total_deposited, 4),
        "total_withdrawn": round(total_withdrawn, 4),
        "current_balance": round(current_balance, 4),
        "baseline_balance": round(baseline_balance, 4),
        "elapsed_days": round(elapsed_days, 2),
        "realistic_max": round(realistic_max, 4),
        "data_quality": quality,
        "first": baseline_ts,
        "last": last_row["last"] if last_row else None,
    }


def insert_portfolio_snapshot(snap: dict) -> int:
    """Record a portfolio-wide snapshot for the equity curve."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO portfolio_snapshots
            (timestamp, sol_usd, usdc_usd, tokens_usd, kamino_usd, kalshi_usd, total_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            snap.get("timestamp", datetime.utcnow().isoformat()),
            snap.get("sol_usd", 0.0),
            snap.get("usdc_usd", 0.0),
            snap.get("tokens_usd", 0.0),
            snap.get("kamino_usd", 0.0),
            snap.get("kalshi_usd", 0.0),
            snap.get("total_usd", 0.0),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_portfolio_snapshots(days: int = 30) -> list[dict]:
    """Return portfolio snapshots for the last N days, oldest first."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM portfolio_snapshots
           WHERE timestamp >= datetime('now', ?)
           ORDER BY timestamp ASC""",
        (f"-{int(days)} days",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_portfolio_snapshot() -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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
        (created_at, symbol, direction, strategy, entry_price, amount_sol, amount_usdc,
         tp_price, sl_price, status, entry_tx, timeframe, confidence, atr, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pos.get("created_at", datetime.utcnow().isoformat()),
            pos["symbol"],
            pos.get("direction", "long"),
            pos.get("strategy", ""),
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


def get_open_position_for_signal(symbol: str, strategy: str) -> Optional[dict]:
    """Get the open position matching a specific symbol and strategy.

    Used by the trade engine to find which position a CLOSE signal should target.
    Returns the most recent matching open position, or None.
    """
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM positions
        WHERE status = 'open' AND symbol = ? AND strategy = ?
        ORDER BY created_at DESC LIMIT 1""",
        (symbol, strategy),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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


def insert_reentry_watch(watch: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO reentry_watches
        (created_at, symbol, strategy, timeframe, sl_exit_price, target_price,
         sl_distance, atr, sizing_pct, expires_at, status, source_position_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            watch["symbol"],
            watch.get("strategy", ""),
            watch.get("timeframe", ""),
            watch["sl_exit_price"],
            watch["target_price"],
            watch["sl_distance"],
            watch.get("atr"),
            watch.get("sizing_pct"),
            watch["expires_at"],
            watch.get("source_position_id"),
            watch.get("notes", ""),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_active_reentry_watches() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM reentry_watches
        WHERE status = 'active'
        ORDER BY created_at"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_reentry_watch(watch_id: int, status: str, **kwargs) -> None:
    conn = get_db()
    sets = ["status = ?"]
    vals = [status]
    if status == "triggered":
        sets.append("triggered_at = ?")
        vals.append(datetime.utcnow().isoformat())
    if "position_id" in kwargs:
        sets.append("position_id = ?")
        vals.append(kwargs["position_id"])
    vals.append(watch_id)
    conn.execute(
        f"UPDATE reentry_watches SET {', '.join(sets)} WHERE id = ?", vals
    )
    conn.commit()
    conn.close()


def expire_stale_reentry_watches() -> int:
    conn = get_db()
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "UPDATE reentry_watches SET status = 'expired' WHERE status = 'active' AND expires_at < ?",
        (now,),
    )
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def get_reentry_watches_by_position(position_ids: list[int]) -> dict[int, dict]:
    """Get re-entry watches keyed by source_position_id."""
    if not position_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" for _ in position_ids)
    rows = conn.execute(
        f"""SELECT * FROM reentry_watches
        WHERE source_position_id IN ({placeholders})
        ORDER BY created_at DESC""",
        position_ids,
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = dict(r)
        pid = d["source_position_id"]
        if pid not in result:
            result[pid] = d
    return result


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


def get_indicator_performance() -> dict:
    """Per-indicator/strategy performance breakdown.

    Groups positions by ``strategy`` column and returns:
    - closed trade counts (wins/losses/manual)
    - open position count and current unrealized exposure
    - realized P&L (total, avg, best/worst)
    - win rate, win/loss ratio, avg hold hours
    - recent closed-trade timeline per strategy for sparkline/chart
    """
    conn = get_db()

    # NOTE: pre-Apr-24 positions with empty `strategy` are filtered out here
    # (5 closed positions, -$9.82 total — the -$10 JTO loss was from this era).
    # Underlying rows preserved in DB for audit; only the dashboard aggregates ignore them.
    closed_rows = conn.execute(
        """
        SELECT
            strategy,
            COUNT(*) AS trades,
            SUM(CASE WHEN status = 'closed_tp' THEN 1 ELSE 0 END) AS tp_wins,
            SUM(CASE WHEN status = 'closed_sl' THEN 1 ELSE 0 END) AS sl_losses,
            SUM(CASE WHEN status = 'closed_manual' THEN 1 ELSE 0 END) AS manual_closes,
            SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(pnl_usdc), 0) AS total_pnl,
            COALESCE(AVG(pnl_usdc), 0) AS avg_pnl,
            COALESCE(AVG(pnl_percent), 0) AS avg_pnl_pct,
            COALESCE(AVG(CASE WHEN pnl_usdc > 0 THEN pnl_usdc END), 0) AS avg_win,
            COALESCE(AVG(CASE WHEN pnl_usdc < 0 THEN pnl_usdc END), 0) AS avg_loss,
            COALESCE(MAX(pnl_usdc), 0) AS best_trade,
            COALESCE(MIN(pnl_usdc), 0) AS worst_trade,
            COALESCE(AVG(
                CASE WHEN closed_at IS NOT NULL AND created_at IS NOT NULL
                THEN (julianday(closed_at) - julianday(created_at)) * 24
                END
            ), 0) AS avg_hold_hours
        FROM positions
        WHERE status != 'open'
          AND NULLIF(strategy, '') IS NOT NULL
        GROUP BY strategy
        """
    ).fetchall()

    open_rows = conn.execute(
        """
        SELECT
            strategy,
            COUNT(*) AS open_count,
            COALESCE(SUM(amount_usdc), 0) AS open_notional
        FROM positions
        WHERE status = 'open'
          AND NULLIF(strategy, '') IS NOT NULL
        GROUP BY strategy
        """
    ).fetchall()

    timeline = conn.execute(
        """
        SELECT
            strategy,
            closed_at AS ts,
            pnl_usdc,
            symbol,
            status
        FROM positions
        WHERE status != 'open'
          AND closed_at IS NOT NULL
          AND NULLIF(strategy, '') IS NOT NULL
        ORDER BY closed_at ASC
        """
    ).fetchall()

    conn.close()

    open_map: Dict[str, dict] = {
        r["strategy"]: {
            "open_count": r["open_count"] or 0,
            "open_notional_usdc": round(r["open_notional"] or 0, 2),
        }
        for r in open_rows
    }

    timeline_map: Dict[str, list] = {}
    cum_map: Dict[str, float] = {}
    for r in timeline:
        strat = r["strategy"]
        cum = cum_map.get(strat, 0.0) + (r["pnl_usdc"] or 0.0)
        cum_map[strat] = cum
        timeline_map.setdefault(strat, []).append({
            "ts": r["ts"],
            "pnl": round(r["pnl_usdc"] or 0.0, 2),
            "cumulative": round(cum, 2),
            "symbol": r["symbol"],
            "status": r["status"],
        })

    indicators = []
    seen = set()
    for r in closed_rows:
        strat = r["strategy"]
        seen.add(strat)
        trades = r["trades"] or 0
        wins = r["wins"] or 0
        avg_win = abs(r["avg_win"] or 0)
        avg_loss = abs(r["avg_loss"] or 0)
        wl = round(avg_win / avg_loss, 2) if avg_loss > 0 else (avg_win if avg_win > 0 else 0)
        o = open_map.get(strat, {"open_count": 0, "open_notional_usdc": 0.0})
        indicators.append({
            "strategy": strat,
            "trades": trades,
            "wins": wins,
            "losses": r["losses"] or 0,
            "tp_wins": r["tp_wins"] or 0,
            "sl_losses": r["sl_losses"] or 0,
            "manual_closes": r["manual_closes"] or 0,
            "win_rate": round((wins / trades) * 100, 1) if trades > 0 else 0.0,
            "total_pnl_usdc": round(r["total_pnl"] or 0, 2),
            "avg_pnl_usdc": round(r["avg_pnl"] or 0, 2),
            "avg_pnl_pct": round(r["avg_pnl_pct"] or 0, 2),
            "avg_win_usdc": round(avg_win, 2),
            "avg_loss_usdc": round(-avg_loss, 2),
            "win_loss_ratio": wl,
            "best_trade_usdc": round(r["best_trade"] or 0, 2),
            "worst_trade_usdc": round(r["worst_trade"] or 0, 2),
            "avg_hold_hours": round(r["avg_hold_hours"] or 0, 1),
            "open_count": o["open_count"],
            "open_notional_usdc": o["open_notional_usdc"],
            "timeline": timeline_map.get(strat, []),
        })

    # Strategies that only have open positions (no closed history yet)
    for strat, o in open_map.items():
        if strat in seen:
            continue
        indicators.append({
            "strategy": strat,
            "trades": 0, "wins": 0, "losses": 0,
            "tp_wins": 0, "sl_losses": 0, "manual_closes": 0,
            "win_rate": 0.0,
            "total_pnl_usdc": 0.0, "avg_pnl_usdc": 0.0, "avg_pnl_pct": 0.0,
            "avg_win_usdc": 0.0, "avg_loss_usdc": 0.0, "win_loss_ratio": 0.0,
            "best_trade_usdc": 0.0, "worst_trade_usdc": 0.0,
            "avg_hold_hours": 0.0,
            "open_count": o["open_count"],
            "open_notional_usdc": o["open_notional_usdc"],
            "timeline": [],
        })

    indicators.sort(key=lambda x: x["total_pnl_usdc"], reverse=True)

    return {
        "indicators": indicators,
        "strategy_count": len(indicators),
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
         source_file, notes, status,
         suggested_leverage, run_id, avg_rr)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            bt.get("suggested_leverage", 1.0),
            bt.get("run_id"),
            bt.get("avg_rr", 0.0),
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


def sync_kalshi_positions(positions: list[dict]) -> dict:
    """Idempotent merge of live Kalshi positions into kalshi_positions table.

    - Open rows (position != 0): mirror current snapshot. Old open rows are
      cleared and re-inserted each cycle.
    - Closed rows (position == 0 with non-zero realized_pnl): persisted as
      history so per-category sub-breakers can query realized P&L by day.
      Upserted on realized_pnl change for the same ticker.
    """
    conn = get_db()
    inserted = 0
    closed_new = 0
    closed_updated = 0
    now_iso = datetime.utcnow().isoformat()

    # Wipe stale open rows only; preserve closed history.
    conn.execute("DELETE FROM kalshi_positions WHERE status = 'open'")

    for p in positions:
        try:
            ticker = p.get("ticker", "") or ""
            if not ticker:
                continue
            pos_count = int(p.get("position", 0) or 0)
            invested_cents = int(p.get("market_exposure", 0) or 0)
            traded = float(p.get("total_traded_dollars", 0) or 0)
            realized_cents = int(round(float(p.get("realized_pnl_dollars", 0) or 0) * 100))
            event_ticker = p.get("event_ticker", "") or ""
            title = p.get("title", "") or ""
            ts = p.get("last_updated_ts", now_iso)

            if pos_count != 0:
                side = "yes" if pos_count > 0 else "no"
                abs_count = abs(pos_count)
                avg_price_cents = int(round(traded * 100 / abs_count)) if abs_count > 0 else 0
                conn.execute(
                    """INSERT INTO kalshi_positions
                    (opened_at, ticker, event_ticker, title, side, count,
                     avg_price_cents, invested_cents, pnl_cents, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, ticker, event_ticker, title, side, abs_count,
                     avg_price_cents, invested_cents, realized_cents,
                     "open", "synced"),
                )
                inserted += 1
                continue

            if realized_cents == 0:
                continue  # never opened or zero-PnL settle — nothing to track

            # Closed position with realized P&L — preserve as history.
            existing = conn.execute(
                "SELECT id, pnl_cents FROM kalshi_positions "
                "WHERE ticker = ? AND status = 'closed' "
                "ORDER BY closed_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if existing and int(existing["pnl_cents"] or 0) == realized_cents:
                continue
            if existing:
                conn.execute(
                    """UPDATE kalshi_positions
                       SET pnl_cents = ?, closed_at = ?, notes = ?
                       WHERE id = ?""",
                    (realized_cents, ts, "synced-closed", existing["id"]),
                )
                closed_updated += 1
            else:
                conn.execute(
                    """INSERT INTO kalshi_positions
                    (opened_at, closed_at, ticker, event_ticker, title, side, count,
                     avg_price_cents, invested_cents, pnl_cents, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, ts, ticker, event_ticker, title, "yes", 0,
                     0, invested_cents, realized_cents, "closed", "synced-closed"),
                )
                closed_new += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return {
        "inserted": inserted,
        "closed_new": closed_new,
        "closed_updated": closed_updated,
        "total": len(positions),
    }


def insert_kalshi_whale(whale: dict) -> bool:
    """Persist a detected whale trade. Returns True if a new row was inserted,
    False if it was a duplicate (dedupe on trade_id). Silent on DB errors so
    the live whale tracker keeps scanning even if persistence breaks.

    Expected fields: ts, ticker, event_ticker, side, count, price_cents,
    cost_cents, trade_id. Caller supplies detected_at or it defaults to now.
    """
    try:
        conn = get_db()
        cur = conn.execute(
            """INSERT OR IGNORE INTO kalshi_whales
            (ts, ticker, event_ticker, side, count, price_cents, cost_cents,
             trade_id, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(whale.get("ts", "")),
                whale["ticker"],
                whale.get("event_ticker", ""),
                whale["side"],
                int(whale.get("count", 0) or 0),
                int(whale.get("price_cents", 0) or 0),
                int(whale.get("cost_cents", 0) or 0),
                whale["trade_id"],
                whale.get("detected_at", datetime.utcnow().isoformat()),
            ),
        )
        inserted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        logger.debug(f"insert_kalshi_whale failed: {e}")
        return False


def get_whale_flow_by_ticker(series_prefixes: list[str], lookback_minutes: int = 120) -> list[dict]:
    """Aggregate recent whale flow per ticker for whale-follow signals.

    Returns rows sorted by total YES contracts descending:
    [{ticker, event_ticker, yes_contracts, no_contracts, yes_cost_cents,
      no_cost_cents, avg_yes_price_cents, trade_count}]
    """
    try:
        conn = get_db()
        placeholders = " OR ".join(f"ticker LIKE ?" for _ in series_prefixes)
        like_args = [f"{p}%" for p in series_prefixes]
        rows = conn.execute(
            f"""SELECT ticker, event_ticker,
                SUM(CASE WHEN side='yes' THEN count ELSE 0 END) as yes_contracts,
                SUM(CASE WHEN side='no'  THEN count ELSE 0 END) as no_contracts,
                SUM(CASE WHEN side='yes' THEN cost_cents ELSE 0 END) as yes_cost_cents,
                SUM(CASE WHEN side='no'  THEN cost_cents ELSE 0 END) as no_cost_cents,
                AVG(CASE WHEN side='yes' THEN price_cents END) as avg_yes_price_cents,
                COUNT(*) as trade_count
            FROM kalshi_whales
            WHERE ({placeholders})
              AND detected_at >= datetime('now', ?)
            GROUP BY ticker
            ORDER BY yes_contracts DESC""",
            (*like_args, f"-{lookback_minutes} minutes"),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"get_whale_flow_by_ticker failed: {e}")
        return []


def sync_kalshi_settlements(settlements: list[dict]) -> dict:
    """Upsert settlement records from Kalshi's /portfolio/settlements endpoint.

    Necessary complement to sync_kalshi_positions because Kalshi's
    get_positions() garbage-collects tickers once they fully settle and
    drain from the account. If sync runs even a few minutes late we lose
    the realized P&L. The /settlements endpoint keeps a window of history
    we can reconcile against.

    Each settlement record: {ticker, revenue, settled_time}.
    - revenue == 0 → NO side won (we held YES, so payout = 0)
    - revenue > 0  → YES side won (per-contract value in cents; reconciled
      against cash flow on 2026-05-12 — multi-contract holdings produced
      cash deltas matching `revenue * held_count`).

    Cost basis is derived from kalshi_trades buys minus sells for the ticker.

    Idempotent — skips tickers that already have a closed kalshi_positions
    row, so re-running can't double-count.
    """
    if not settlements:
        return {"inserted": 0, "skipped": 0, "no_holdings": 0, "total": 0}

    conn = get_db()
    inserted = 0
    skipped = 0
    no_holdings = 0

    for s in settlements:
        try:
            ticker = s.get("ticker", "") or ""
            if not ticker:
                continue
            revenue_per_contract = int(s.get("revenue", 0) or 0)
            settled_time = s.get("settled_time")
            if hasattr(settled_time, "isoformat"):
                settled_iso = settled_time.isoformat()
            else:
                settled_iso = str(settled_time or "")

            # Skip if we've already recorded this ticker's closure
            existing = conn.execute(
                "SELECT id FROM kalshi_positions WHERE ticker = ? AND status = 'closed' LIMIT 1",
                (ticker,),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Cost basis from kalshi_trades (sum BUYs, subtract SELLs)
            buys = conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS qty, COALESCE(SUM(total_cost_cents), 0) AS cost "
                "FROM kalshi_trades WHERE ticker = ? AND action = 'buy'",
                (ticker,),
            ).fetchone()
            sells = conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS qty, COALESCE(SUM(total_cost_cents), 0) AS rev "
                "FROM kalshi_trades WHERE ticker = ? AND action = 'sell'",
                (ticker,),
            ).fetchone()

            buy_qty = int(buys["qty"] or 0)
            buy_cost = int(buys["cost"] or 0)
            sell_qty = int(sells["qty"] or 0)
            sell_rev = int(sells["rev"] or 0)

            held_at_settle = buy_qty - sell_qty
            if held_at_settle <= 0:
                # No outstanding contracts at settlement (likely manually
                # closed before settle, or trades not logged) — skip rather
                # than fabricate a P&L row.
                no_holdings += 1
                continue

            net_cost = buy_cost - sell_rev
            payout_cents = revenue_per_contract * held_at_settle
            pnl_cents = payout_cents - net_cost
            avg_price = int(round(buy_cost / buy_qty)) if buy_qty > 0 else 0

            conn.execute(
                """INSERT INTO kalshi_positions
                (opened_at, closed_at, ticker, event_ticker, title, side, count,
                 avg_price_cents, invested_cents, pnl_cents, settled_payout_cents,
                 status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (settled_iso, settled_iso, ticker, "", "", "yes", held_at_settle,
                 avg_price, net_cost, pnl_cents, payout_cents,
                 "closed", "settlement_api"),
            )
            inserted += 1
        except Exception as e:
            logger.debug(f"Settlement sync error for {ticker}: {e}")
            continue

    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped,
            "no_holdings": no_holdings, "total": len(settlements)}


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
