"""News and geopolitical risk assessment service."""

import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.config import get

logger = logging.getLogger("bot.news")


class NewsService:
    """Fetch news headlines for geo-risk assessment."""

    def __init__(self):
        cfg = get("news")
        self.provider = cfg.get("provider", "newsapi")
        self.newsapi_key = cfg.get("newsapi_key", "")
        self.tavily_key = cfg.get("tavily_api_key", "")
        self.keywords = cfg.get("keywords", [])
        self._client = httpx.AsyncClient(timeout=15)
        self._cache: list[str] = []
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=cfg.get("check_interval_minutes", 15))
        self._backoff_until: Optional[datetime] = None
        self._backoff_minutes = cfg.get("failure_backoff_minutes", 60)
        self._failure_logged = False

    async def get_headlines(self, force_refresh: bool = False) -> list[str]:
        """Get recent headlines relevant to crypto/geopolitics."""
        now = datetime.utcnow()
        if (
            not force_refresh
            and self._cache
            and self._cache_time
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._cache

        if not force_refresh and self._backoff_until and now < self._backoff_until:
            return self._cache

        providers = []
        if self.provider == "newsapi":
            if self.newsapi_key:
                providers.append(("newsapi", self._fetch_newsapi))
            if self.tavily_key:
                providers.append(("tavily", self._fetch_tavily))
        else:
            if self.tavily_key:
                providers.append(("tavily", self._fetch_tavily))
            if self.newsapi_key:
                providers.append(("newsapi", self._fetch_newsapi))

        if not providers:
            logger.warning("No news provider configured")
            return []

        for name, fetch_fn in providers:
            try:
                headlines = await fetch_fn()
                self._cache = headlines
                self._cache_time = now
                self._backoff_until = None
                self._failure_logged = False
                logger.info(f"News fetched via {name} ({len(headlines)} headlines)")
                return headlines
            except Exception as e:
                logger.warning(f"{name} failed: {e}, trying next provider")

        self._backoff_until = now + timedelta(minutes=self._backoff_minutes)
        if not self._failure_logged:
            logger.warning(
                f"All news providers failed; pausing fetches for {self._backoff_minutes}m"
            )
            self._failure_logged = True
        return self._cache

    async def _fetch_newsapi(self) -> list[str]:
        query = " OR ".join(self.keywords[:5])
        resp = await self._client.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "sortBy": "publishedAt",
                "pageSize": 15,
                "language": "en",
                "apiKey": self.newsapi_key,
            },
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            f"[{a.get('source', {}).get('name', '?')}] {a['title']}"
            for a in articles
            if a.get("title")
        ]

    async def _fetch_tavily(self) -> list[str]:
        resp = await self._client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.tavily_key,
                "query": "crypto solana regulation geopolitical market impact",
                "max_results": 10,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [r["title"] for r in results if r.get("title")]

    async def close(self):
        await self._client.aclose()
