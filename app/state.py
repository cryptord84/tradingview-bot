"""Global bot runtime state — start/stop flag and shared status."""

from datetime import datetime
from typing import Optional

# Bot can be paused via dashboard without killing the process
_bot_active: bool = True
_stopped_at: Optional[datetime] = None
_started_at: datetime = datetime.utcnow()


def is_active() -> bool:
    return _bot_active


def stop_bot() -> dict:
    global _bot_active, _stopped_at
    _bot_active = False
    _stopped_at = datetime.utcnow()
    return {"active": False, "stopped_at": _stopped_at.isoformat()}


def start_bot() -> dict:
    global _bot_active, _started_at, _stopped_at
    _bot_active = True
    _started_at = datetime.utcnow()
    _stopped_at = None
    return {"active": True, "started_at": _started_at.isoformat()}


def get_uptime() -> str:
    since = _started_at if _bot_active else _stopped_at
    if not since:
        return "unknown"
    delta = datetime.utcnow() - _started_at
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
