"""Daily backup scheduler: trades CSV (human-readable) + full-DB snapshot.

The 2026-05-30 power-off corrupted `data/trades.db`; the only backup at the
time was a trades-only CSV, so `positions` (and every other table) could not be
restored and had to be rebuilt by hand from wallet balances. The full-DB
snapshot below captures ALL tables so any future recovery is a one-file copy.
"""

import logging
from datetime import date
from pathlib import Path

from app.config import get
from app.database import export_csv, get_db

logger = logging.getLogger("bot.backup")


def run_daily_backup():
    """Run the daily trades CSV export and the full-DB snapshot (idempotent per day)."""
    if not get("database", "csv_backup_daily", True):
        return

    # 1. Trades CSV (human-readable, kept for convenience / spot checks)
    backup_dir = Path(get("database", "csv_backup_dir", "data/csv_backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    output = backup_dir / f"trades_{date.today().isoformat()}.csv"
    if output.exists():
        logger.debug(f"CSV backup already exists: {output}")
    else:
        path = export_csv(str(output))
        logger.info(f"Daily CSV backup: {path}")

    # 2. Full-DB snapshot — the authoritative recovery artifact (all tables)
    run_full_db_snapshot()


def run_full_db_snapshot(keep_days: int = 14):
    """Write a consistent, compacted copy of the ENTIRE database (all tables).

    Uses VACUUM INTO so the result is a single self-contained .db file with no
    WAL sidecar needed — restore is just `cp snapshot.db data/trades.db`.
    """
    snap_dir = Path(get("database", "db_snapshot_dir", "data/db_snapshots"))
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / f"trades_{date.today().isoformat()}.db"

    if snap.exists():
        logger.debug(f"DB snapshot already exists: {snap}")
    else:
        conn = get_db()
        try:
            conn.isolation_level = None  # VACUUM cannot run inside a transaction
            conn.execute("PRAGMA wal_checkpoint(FULL)")  # fold WAL into the snapshot
            safe = str(snap).replace("'", "''")
            conn.execute(f"VACUUM INTO '{safe}'")
            logger.info(f"Full DB snapshot: {snap}")
        except Exception as e:
            logger.error(f"Full DB snapshot failed: {e}")
            # leave any partial file out of the way
            try:
                if snap.exists():
                    snap.unlink()
            except OSError:
                pass
        finally:
            conn.close()

    _prune_snapshots(snap_dir, keep_days)


def _prune_snapshots(snap_dir: Path, keep_days: int):
    """Keep only the most recent `keep_days` snapshots."""
    snaps = sorted(snap_dir.glob("trades_*.db"))
    if len(snaps) <= keep_days:
        return
    for old in snaps[:-keep_days]:
        try:
            old.unlink()
            logger.info(f"Pruned old DB snapshot: {old.name}")
        except OSError as e:
            logger.warning(f"Could not prune {old}: {e}")
