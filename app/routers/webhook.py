"""Webhook endpoint for TradingView alerts."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException

from app.config import get
from app.models import WebhookSignal

logger = logging.getLogger("bot.webhook")

router = APIRouter(tags=["webhook"])

# Rate limiting
_request_times: list[float] = []


def _check_rate_limit():
    """Simple in-memory rate limiter."""
    import time

    limit = get("webhook", "rate_limit_per_minute", 10)
    now = time.time()
    cutoff = now - 60

    # Remove old entries
    while _request_times and _request_times[0] < cutoff:
        _request_times.pop(0)

    if len(_request_times) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    _request_times.append(now)


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Receive and process TradingView webhook alerts.

    Returns immediately with 200 to avoid TradingView timeout errors.
    Signal processing happens in the background via the signal queue.
    """
    _check_rate_limit()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Validate secret
    expected_secret = get("webhook", "secret", "")
    if body.get("secret") != expected_secret:
        logger.warning(f"Invalid webhook secret from {request.client.host}")
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Parse signal
    try:
        signal = WebhookSignal(**body)
    except Exception as e:
        logger.error(f"Invalid signal payload: {e}")
        raise HTTPException(status_code=422, detail=f"Invalid signal: {str(e)}")

    # Check if bot is paused via dashboard
    from app.state import is_active
    if not is_active():
        return {"status": "paused", "message": "Bot is stopped. Resume from dashboard."}

    logger.info(
        f"Webhook received: {signal.signal_type.value} {signal.symbol} "
        f"confidence={signal.confidence_score}"
    )

    # Enqueue signal for background processing — return immediately so
    # TradingView doesn't get a timeout error (~3s limit)
    from app.services.trade_engine import get_signal_queue

    queue = get_signal_queue()
    asyncio.create_task(queue.enqueue(signal, source_ip=request.client.host or ""))

    return {
        "status": "queued",
        "timestamp": datetime.utcnow().isoformat(),
        "signal": signal.signal_type.value,
        "symbol": signal.symbol,
        "confidence": signal.confidence_score,
    }
