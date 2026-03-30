"""TradeBot Scout — daily X.com scan for TradingView bot intelligence."""

import asyncio
import json
import logging
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from urllib.parse import unquote, urlparse, parse_qs
from html.parser import HTMLParser

from app.config import get

logger = logging.getLogger("bot.scout")

SCOUT_LOG_DIR = Path("data/scout_logs")

SEARCH_QUERIES = [
    '"TradingView bot" OR "TV bot" OR "Pine Script bot" OR "TradingView automated trading"',
    '"TradingView indicators for bot" OR "best indicator bot" OR "Pine Script strategy bot"',
    '"TradingView webhook" OR "TradingView alert bot" OR "Pine Script v5 bot"',
]


class _DDGResultParser(HTMLParser):
    """Parse DuckDuckGo HTML search results."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self.in_title = False
        self.in_snippet = False
        self.current: dict = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            self.in_title = True
            raw_url = a.get("href", "")
            parsed = parse_qs(urlparse(raw_url).query)
            url = unquote(parsed.get("uddg", [raw_url])[0])
            self.current = {"url": url, "title": "", "content": ""}
        if tag == "a" and "result__snippet" in cls:
            self.in_snippet = True

    def handle_data(self, data):
        if self.in_title:
            self.current["title"] += data
        if self.in_snippet:
            self.current["content"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.in_title:
            self.in_title = False
        if tag == "a" and self.in_snippet:
            self.in_snippet = False
            if self.current.get("title"):
                self.results.append(self.current)
            self.current = {}


def _parse_ddg_results(html: str) -> list[dict]:
    """Extract search results from DuckDuckGo HTML response."""
    parser = _DDGResultParser()
    parser.feed(html)
    return parser.results


class ScoutService:
    """Searches X.com via Tavily for TradingView bot intelligence."""

    def __init__(self):
        news_cfg = get("news")
        self.tavily_key = news_cfg.get("tavily_api_key", "")
        self._client = httpx.AsyncClient(timeout=30)

    async def search_x(self) -> list[dict]:
        """Search X.com for TradingView bot posts. Tries Tavily first, then DuckDuckGo."""
        all_results = []
        seen_urls = set()

        for query in SEARCH_QUERIES:
            try:
                results = await self._search_tavily(query)
                if not results:
                    results = await self._search_ddg(query)

                for r in results:
                    url = r["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_results.append(r)
            except Exception as e:
                logger.warning(f"Scout search failed: {e}")

            await asyncio.sleep(2)

        return all_results[:15]

    async def _search_tavily(self, query: str) -> list[dict]:
        """Search via Tavily API (if key is valid and within limits)."""
        if not self.tavily_key:
            return []
        try:
            resp = await self._client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.tavily_key,
                    "query": query,
                    "max_results": 5,
                    "search_depth": "advanced",
                    "include_domains": ["x.com", "twitter.com"],
                },
            )
            resp.raise_for_status()
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")[:300]}
                for r in resp.json().get("results", [])
                if r.get("url")
            ]
        except Exception as e:
            logger.debug(f"Tavily search failed: {e}")
            return []

    async def _search_ddg(self, query: str) -> list[dict]:
        """Search via DuckDuckGo HTML using curl subprocess.

        Uses create_subprocess_exec — args passed as list, injection-safe.
        """
        try:
            ddg_query = f"site:x.com {query}"
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-L", "--max-time", "15",
                "--data-urlencode", f"q={ddg_query}",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "-H", "Accept: text/html",
                "https://html.duckduckgo.com/html/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            return _parse_ddg_results(stdout.decode())
        except Exception as e:
            logger.debug(f"DDG search failed: {e}")
            return []

    async def generate_report(self) -> dict:
        """Search X.com and generate a structured scout report."""
        logger.info("Scout: running daily X.com scan")
        results = await self.search_x()

        today = date.today().isoformat()
        report = {
            "date": today,
            "timestamp": datetime.utcnow().isoformat(),
            "result_count": len(results),
            "posts": results,
        }

        # Save to log file
        SCOUT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = SCOUT_LOG_DIR / f"scout_{today}.json"
        with open(log_path, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Scout: found {len(results)} posts, saved to {log_path}")
        return report

    async def analyze_with_claude(self, posts: list[dict], today: str) -> str:
        """Use Claude Sonnet to analyze posts and generate the formatted report."""
        if not posts:
            return f"🚀 <b>Daily TradingView Bot Update – {today}</b>\n\nNo new posts found in the last 24 hours."

        raw = "\n\n".join(
            f"[{i+1}] {p.get('title','')}\n{p.get('content','')[:400]}\nURL: {p.get('url','')}"
            for i, p in enumerate(posts)
        )

        system_prompt = (
            "You are TradeBot Scout analyzing X.com posts about TradingView bots and Pine Script. "
            "Produce a concise Telegram update using HTML formatting (bold with <b>, code with <code>). "
            "Maximum 350 words. Use this exact structure:\n\n"
            f"🚀 <b>Daily TradingView Bot Update – {today}</b>\n\n"
            "<b>Overview:</b> [1-2 sentence summary of hottest topics]\n\n"
            "🔥 <b>Top Indicator Suggestions:</b>\n"
            "• [Indicator] – [one-line why useful for bots] (up to 5)\n\n"
            "<b>Other Key Insights:</b>\n"
            "• [bullet] (up to 3)\n\n"
            "📌 <b>Posts Worth Reading:</b>\n"
            "1. [one-sentence summary] → [URL]\n"
            "(3-5 highest-value posts with actual URLs from the data)\n\n"
            "Rules: be concise and actionable, include only real URLs from the provided data, "
            "prioritize real trader discussions over hype."
        )

        cfg = get("claude")
        mode = cfg.get("mode", "cli")

        try:
            if mode == "api":
                import anthropic
                api_key = cfg.get("api_key", "")
                if not api_key:
                    raise ValueError("No API key")
                client = anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model="claude-sonnet-4-6-20250514",
                    max_tokens=1024,
                    temperature=0.5,
                    system=system_prompt,
                    messages=[{"role": "user", "content": f"Analyze these posts:\n\n{raw}"}],
                )
                return msg.content[0].text.strip()
            else:
                # CLI mode — create_subprocess_exec passes args as a list (no shell expansion, injection-safe)
                import shutil
                cli_path = cfg.get("cli_path", "claude")
                resolved = shutil.which(cli_path)
                if not resolved:
                    raise FileNotFoundError("Claude CLI not found")

                full_prompt = f"{system_prompt}\n\nAnalyze these posts:\n\n{raw}"
                proc = await asyncio.create_subprocess_exec(
                    resolved, "--print", "--model", "claude-sonnet-4-6-20250514", full_prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
                return stdout.decode().strip()
        except Exception as e:
            logger.warning(f"Scout: Claude analysis failed ({e}), using plain format")
            return self._format_plain(posts, today)

    def _format_plain(self, posts: list[dict], today: str) -> str:
        """Fallback plain formatter if Claude is unavailable."""
        lines = [f"🚀 <b>Daily TradingView Bot Update – {today}</b>\n", "📌 <b>Posts Found:</b>"]
        for p in posts[:8]:
            title = (p.get("title") or "")[:100]
            url = p.get("url", "")
            line = f"• <a href='{url}'>{title}</a>" if url else f"• {title}"
            lines.append(line)
        return "\n".join(lines)

    async def format_telegram_message(self, report: dict) -> str:
        """Analyze posts with Claude Sonnet and format as Telegram message."""
        posts = report.get("posts", [])
        today = report.get("date", date.today().isoformat())
        msg = await self.analyze_with_claude(posts, today)
        if len(msg) > 3900:
            msg = msg[:3900] + "\n...(truncated)"
        return msg

    async def run_and_notify(self):
        """Run scout scan and send results to Telegram."""
        from app.services.telegram_service import TelegramService

        try:
            report = await self.generate_report()
            msg = await self.format_telegram_message(report)

            tg = TelegramService()
            await tg.send_message(msg)
            await tg.close()

            logger.info("Scout: Telegram notification sent")
        except Exception as e:
            logger.error(f"Scout: failed to send notification: {e}")

    async def close(self):
        await self._client.aclose()


def get_latest_report() -> Optional[dict]:
    """Get the most recent scout report from disk."""
    SCOUT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SCOUT_LOG_DIR.glob("scout_*.json"), reverse=True)
    if not files:
        return None
    try:
        with open(files[0]) as f:
            return json.load(f)
    except Exception:
        return None


def get_all_reports(limit: int = 7) -> list[dict]:
    """Get recent scout reports."""
    SCOUT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SCOUT_LOG_DIR.glob("scout_*.json"), reverse=True)[:limit]
    reports = []
    for f in files:
        try:
            with open(f) as fh:
                reports.append(json.load(fh))
        except Exception:
            pass
    return reports


async def scout_scheduler():
    """Background task that runs the scout at 3:30 AM daily."""
    logger.info("Scout scheduler started — runs daily at 3:30 AM")
    target_time = dtime(3, 30)  # 3:30 AM local time

    while True:
        now = datetime.now()
        target = datetime.combine(now.date(), target_time)

        # If we've already passed 3:30 AM today, schedule for tomorrow
        if now >= target:
            target = datetime.combine(now.date(), target_time) + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Scout: next run at {target.strftime('%Y-%m-%d %H:%M')}, sleeping {wait_seconds/3600:.1f}h")

        await asyncio.sleep(wait_seconds)

        # Run the scout
        scout = ScoutService()
        try:
            await scout.run_and_notify()
        except Exception as e:
            logger.error(f"Scout scheduler error: {e}")
        finally:
            await scout.close()
