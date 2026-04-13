"""Cancel all stale resting Kalshi orders from the database.

Run with: python cancel_stale_orders.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.config import load_config
from app.database import get_db


async def cancel_all_resting():
    load_config()

    from app.services.kalshi_client import get_async_kalshi_client
    client = get_async_kalshi_client()

    if not client.enabled:
        print("Kalshi client not enabled — check config/credentials")
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT id, order_id, ticker, side, timestamp FROM kalshi_trades "
        "WHERE status='resting' AND order_id != '' ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()

    print(f"Found {len(rows)} resting orders to cancel")

    cancelled = 0
    already_gone = 0
    failed = 0

    for row in rows:
        row_id, order_id, ticker, side, ts = row[0], row[1], row[2], row[3], row[4]
        try:
            await client.cancel_order(order_id)
            conn = get_db()
            conn.execute("UPDATE kalshi_trades SET status='cancelled' WHERE id=?", (row_id,))
            conn.commit()
            conn.close()
            cancelled += 1
            print(f"  ✓ Cancelled {ticker} {side} ({order_id[:8]}...)")
        except Exception as e:
            err = str(e).lower()
            if "not found" in err or "404" in err or "already" in err or "cancel" in err:
                conn = get_db()
                conn.execute("UPDATE kalshi_trades SET status='cancelled' WHERE id=?", (row_id,))
                conn.commit()
                conn.close()
                already_gone += 1
                print(f"  ~ Already gone {ticker} {side} ({order_id[:8]}...) — DB updated")
            else:
                failed += 1
                print(f"  ✗ Failed {ticker} {side} ({order_id[:8]}...): {e}")
        await asyncio.sleep(0.2)  # Rate limit: 5 req/sec

    print(f"\nDone: {cancelled} cancelled, {already_gone} already gone, {failed} failed")


if __name__ == "__main__":
    asyncio.run(cancel_all_resting())
