"""Retry-with-backoff for transient network failures.

Why (2026-08-12): a single CoinGecko ConnectTimeout discarded a whole trade
signal. `jupiter_client.get_sol_price()` already falls back Binance ->
CoinGecko, but neither source was retried, so one blip on each killed the
signal outright:

    trade_engine.py:713   sol_price = await self.jupiter.get_sol_price()
    jupiter_client.py:241 return await self._get_sol_price_coingecko()
    httpx.ConnectTimeout

That surfaced as `failed_engine_error` on SOLUSDT at 04:01 and as the smoke
test's lane_sizing[SOLUSDT] failure — the same event. It is also the failure
class that killed the 2026-08-06 LDO signal, our best walk-forward combo.

Retries ONLY transient transport faults and 5xx. A 4xx, a parse error or a
business-logic failure is deterministic — retrying it just burns time inside a
trade path and delays the signal.

Delays are deliberately short. This runs while a signal is being priced, so the
budget is ~1.5s across all attempts, not the tens of seconds a background job
could afford.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger("bot.retry")

T = TypeVar("T")

# Transport-level faults: worth another go, the next attempt may well succeed.
TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,     # covers Connect/Read/Write/Pool timeouts
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    asyncio.TimeoutError,
    ConnectionError,
)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    # 5xx is the server failing, not the request being wrong. 4xx is on us.
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.4,
    label: str = "",
) -> T:
    """Await fn(), retrying transient network failures with exponential backoff.

    Re-raises the final exception if every attempt fails, so callers keep their
    existing error handling — this narrows the failure window, it does not
    swallow failures.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - re-raised below
            last = e
            if not _is_transient(e) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                f"{label or 'call'} failed with {type(e).__name__} "
                f"(attempt {attempt}/{attempts}) — retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
    assert last is not None  # unreachable; loop either returns or raises
    raise last
