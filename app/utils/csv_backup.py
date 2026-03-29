"""Daily CSV backup scheduler."""

import logging
from datetime import date
from pathlib import Path

from app.config import get
from app.database import export_csv

logger = logging.getLogger("bot.backup")


def run_daily_backup():
    """Export trades to CSV if daily backup is enabled."""
    if not get("database", "csv_backup_daily", True):
        return

    backup_dir = Path(get("database", "csv_backup_dir", "data/csv_backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)

    output = backup_dir / f"trades_{date.today().isoformat()}.csv"
    if output.exists():
        logger.debug(f"Backup already exists: {output}")
        return

    path = export_csv(str(output))
    logger.info(f"Daily CSV backup: {path}")
