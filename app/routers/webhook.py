"""Webhook endpoint for TradingView alerts."""

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
    """Receive and process TradingView webhook alerts."""
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

    # Process via trade engine (imported at runtime to avoid circular imports)
    from app.services.trade_engine import TradeEngine

    engine = TradeEngine()
    result = await engine.process_signal(signal, source_ip=request.client.host or "")

    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "result": result,
    }
