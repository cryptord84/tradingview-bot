"""Monitor ngrok tunnel URL and detect changes."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("bot.ngrok")

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


class NgrokMonitor:
    """Polls ngrok local API to track the current tunnel URL."""

    def __init__(self, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self.current_url: Optional[str] = None
        self.previous_url: Optional[str] = None
        self.last_changed: Optional[datetime] = None
        self.last_checked: Optional[datetime] = None
        self.is_running = False
        self._task: Optional[asyncio.Task] = None

    async def _fetch_tunnel_url(self) -> Optional[str]:
        """Get the current public URL from ngrok's local API."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(NGROK_API)
                resp.raise_for_status()
                tunnels = resp.json().get("tunnels", [])
                for t in tunnels:
                    if t.get("proto") == "https":
                        return t["public_url"]
                # Fall back to first tunnel if no https
                if tunnels:
                    return tunnels[0].get("public_url")
        except Exception as e:
            logger.debug(f"ngrok API unreachable: {e}")
        return None

    async def check_once(self) -> dict:
        """Single check — returns current state."""
        url = await self._fetch_tunnel_url()
        self.last_checked = datetime.utcnow()

        if url and url != self.current_url:
            self.previous_url = self.current_url
            self.current_url = url
            self.last_changed = datetime.utcnow()
            if self.previous_url:
                logger.warning(
                    f"ngrok URL changed: {self.previous_url} -> {self.current_url}"
                )
            else:
                logger.info(f"ngrok URL detected: {self.current_url}")

        return self.get_status()

    def get_status(self) -> dict:
        """Return current ngrok state for the dashboard."""
        return {
            "current_url": self.current_url,
            "webhook_url": f"{self.current_url}/webhook" if self.current_url else None,
            "previous_url": self.previous_url,
            "last_changed": self.last_changed.isoformat() if self.last_changed else None,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
            "online": self.current_url is not None,
        }

    async def _poll_loop(self):
        """Background polling loop."""
        self.is_running = True
        # Initial check immediately
        await self.check_once()
        while self.is_running:
            await asyncio.sleep(self.poll_interval)
            await self.check_once()

    def start(self) -> asyncio.Task:
        """Start the background polling task."""
        self._task = asyncio.create_task(self._poll_loop())
        return self._task

    async def stop(self):
        """Stop polling."""
        self.is_running = False
        if self._task:
            self._task.cancel()


# Singleton instance
_monitor: Optional[NgrokMonitor] = None


def get_ngrok_monitor() -> NgrokMonitor:
    global _monitor
    if _monitor is None:
        _monitor = NgrokMonitor()
    return _monitor
