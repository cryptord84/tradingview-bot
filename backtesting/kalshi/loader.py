"""DuckDB-backed loader for Becker's Kalshi parquet dataset.

Source: github.com/jon-becker/prediction-market-analysis (data.tar.zst, 36 GiB).
Parquets live at <repo>/Github/prediction-market-analysis/data/kalshi/{markets,trades}/.

Performance: shares one DuckDB connection across calls (closing per-call adds
~1s overhead). Per-ticker queries scan only the parquet files that contain that
ticker — DuckDB's parquet predicate pushdown does the file-skipping for us.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import duckdb

DEFAULT_DATA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "Github" / "prediction-market-analysis" / "data" / "kalshi"
)

# One persistent connection per process. DuckDB connections aren't goroutine-safe
# but we run sequentially here, so a module-level singleton works.
_con_lock = threading.Lock()
_con: Optional[duckdb.DuckDBPyConnection] = None


def _get_con() -> duckdb.DuckDBPyConnection:
    global _con
    with _con_lock:
        if _con is None:
            _con = duckdb.connect()
            # Tune for large parquet scans
            _con.execute("PRAGMA threads=4")
        return _con


@dataclass
class MarketMeta:
    ticker: str
    event_ticker: str
    title: str
    status: str
    result: str
    open_time: Optional[datetime]
    close_time: Optional[datetime]
    volume: int


@dataclass
class Trade:
    ts: datetime
    count: int
    yes_price: int  # cents 1-99
    no_price: int   # cents 1-99
    taker_side: str  # 'yes' or 'no'


def _markets_glob(root: Path) -> str:
    return str(root / "markets" / "*.parquet")


def _trades_glob(root: Path) -> str:
    return str(root / "trades" / "*.parquet")


def load_market(ticker: str, root: Path = DEFAULT_DATA_ROOT) -> Optional[MarketMeta]:
    """Fetch finalized-market metadata (or None if no row matches).

    Each call scans the markets parquet glob — for batch operations, prefer
    `find_finalized_markets` which returns MarketMeta directly so you can skip
    re-loading per ticker.
    """
    con = _get_con()
    row = con.execute(
        f"""
        SELECT ticker, event_ticker, title, status, result, open_time, close_time, volume
        FROM '{_markets_glob(root)}'
        WHERE ticker = ? AND status = 'finalized'
        LIMIT 1
        """,
        [ticker],
    ).fetchone()
    if not row:
        return None
    return MarketMeta(*row)


def load_trades(ticker: str, root: Path = DEFAULT_DATA_ROOT) -> list[Trade]:
    """Load all trades for a ticker, ordered by created_time."""
    con = _get_con()
    rows = con.execute(
        f"""
        SELECT created_time, count, yes_price, no_price, taker_side
        FROM '{_trades_glob(root)}'
        WHERE ticker = ?
        ORDER BY created_time
        """,
        [ticker],
    ).fetchall()
    return [Trade(ts=r[0], count=int(r[1]), yes_price=int(r[2]),
                  no_price=int(r[3]), taker_side=r[4]) for r in rows]


def find_finalized_markets(
    event_prefix: Optional[str] = None,
    min_volume: int = 1000,
    limit: int = 100,
    root: Path = DEFAULT_DATA_ROOT,
) -> list[MarketMeta]:
    """List finalized markets matching filters, ordered by volume desc.

    `event_prefix` filters on event_ticker LIKE <prefix>%.
    """
    con = _get_con()
    where = ["status = 'finalized'", "result IN ('yes','no')", "volume >= ?"]
    params: list = [int(min_volume)]
    if event_prefix:
        where.append("event_ticker LIKE ?")
        params.append(f"{event_prefix}%")
    rows = con.execute(
        f"""
        SELECT ticker, event_ticker, title, status, result, open_time, close_time, volume
        FROM '{_markets_glob(root)}'
        WHERE {" AND ".join(where)}
        ORDER BY volume DESC
        LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall()
    return [MarketMeta(*r) for r in rows]


def load_trades_for_many(
    tickers: list[str], root: Path = DEFAULT_DATA_ROOT
) -> dict[str, list[Trade]]:
    """Bulk-load trades for many tickers in a single parquet scan.

    Faster than per-ticker `load_trades` calls when running batch backtests:
    one parquet scan + group-by-ticker in DuckDB instead of N scans.
    """
    if not tickers:
        return {}
    con = _get_con()
    placeholders = ",".join(["?"] * len(tickers))
    rows = con.execute(
        f"""
        SELECT ticker, created_time, count, yes_price, no_price, taker_side
        FROM '{_trades_glob(root)}'
        WHERE ticker IN ({placeholders})
        ORDER BY ticker, created_time
        """,
        tickers,
    ).fetchall()
    out: dict[str, list[Trade]] = {t: [] for t in tickers}
    for r in rows:
        out[r[0]].append(Trade(
            ts=r[1], count=int(r[2]), yes_price=int(r[3]),
            no_price=int(r[4]), taker_side=r[5],
        ))
    return out
