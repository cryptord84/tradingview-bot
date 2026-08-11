"""Exception formatting that survives empty-message exceptions.

Why (2026-08-11): the timeout family — `asyncio.TimeoutError`, builtin
`TimeoutError`, `httpx.ReadTimeout`, `httpx.ConnectTimeout` — all carry an
EMPTY message, so `str(e)` is `''`. Every `f"...: {e}"` in the codebase
therefore renders a timeout as nothing at all, and the reason for the failure
is destroyed at the point it is recorded.

That is not cosmetic. It cost a real diagnosis:

  VWAP Dev / LDO / 1D is one of only 2 combos (out of 1080) that passed
  walk-forward. The single time it fired live (2026-08-06) it ran 86s and then
  failed with `failed_engine_error` and a BLANK reason — `str(e)[:200]` on an
  empty-message timeout. By the time anyone looked, the logs had rotated and
  the cause was unrecoverable. The best-validated strategy in the system was
  silently broken and undiagnosable.

At the time of writing there were 64 blank-error lines in a single bot.log.

Use `describe(e)` anywhere an exception is logged, recorded to the DB, or sent
to Telegram.
"""

from __future__ import annotations


def describe(exc: BaseException, *, max_len: int = 200) -> str:
    """Human-readable exception text that is never empty.

    Falls back to the exception's type name when the message is blank, which is
    the whole point — `TimeoutError()` renders as "TimeoutError" instead of "".
    Qualifies the type with its module when that disambiguates (asyncio vs
    builtins vs httpx), since "which timeout" is usually the useful bit.
    """
    if exc is None:  # defensive: some call sites pass a possibly-None error
        return "unknown error"

    msg = str(exc).strip()
    cls = type(exc).__name__
    module = type(exc).__module__

    if not msg:
        # Empty message: the type IS the information.
        if module and module not in ("builtins", "__main__"):
            text = f"{module}.{cls} (no message)"
        else:
            text = f"{cls} (no message)"
    else:
        # Prefix the type so "connection reset" style messages stay attributable.
        text = f"{cls}: {msg}"

    return text[:max_len]
