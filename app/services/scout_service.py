"""TradeBot Scout — multi-source intelligence scan for TradingView bot insights."""

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

# Expanded source categories with tailored search queries
SOURCE_CATEGORIES = {
    "social": {
        "label": "X.com / Social",
        "queries": [
            '"TradingView bot" OR "TV bot" OR "Pine Script bot"',
            '"TradingView webhook" OR "TradingView alert bot"',
        ],
        "tavily_domains": ["x.com", "twitter.com", "reddit.com"],
    },
    "blogs": {
        "label": "Trading Blogs",
        "queries": [
            '"TradingView strategy" bot automated trading tutorial',
            '"Pine Script" indicator strategy backtest 2026',
        ],
        "tavily_domains": ["medium.com", "tradingview.com", "babypips.com", "investopedia.com"],
    },
    "onchain": {
        "label": "On-Chain Analytics",
        "queries": [
            "on-chain analytics bot trading signals crypto",
            "whale alert smart money DeFi trading bot",
        ],
        "tavily_domains": ["glassnode.com", "dune.com", "nansen.ai", "debank.com", "defillama.com"],
    },
    "news": {
        "label": "Crypto News",
        "queries": [
            "TradingView bot crypto automated trading",
            "algorithmic trading crypto bot strategy",
        ],
        "tavily_domains": ["coindesk.com", "cointelegraph.com", "theblock.co", "decrypt.co"],
    },
    "bittensor": {
        "label": "Bittensor / TAO",
        "queries": [
            "Bittensor TAO price prediction subnet trading",
            "from:@const_anto OR from:@markjeffrey OR from:@bittensor_ OR from:@tao_dot_com TAO",
        ],
        "tavily_domains": ["x.com", "twitter.com", "taostats.io", "bittensor.com"],
    },
    "kalshi": {
        "label": "Kalshi / Prediction Markets",
        "queries": [
            "Kalshi prediction market strategy edge trading",
            "Kalshi bot automated trading binary events",
        ],
        "tavily_domains": ["kalshi.com", "x.com", "twitter.com", "reddit.com", "polymarket.com"],
    },
}

# Legacy flat list for DDG fallback
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
    """Multi-source intelligence scanner for TradingView bot insights.

    Search priority: Firecrawl → Tavily → DuckDuckGo
    Sources: X.com, trading blogs, on-chain analytics, crypto news
    """

    def __init__(self):
        news_cfg = get("news")
        self.tavily_key = news_cfg.get("tavily_api_key", "")

        fc_cfg = get("firecrawl") or {}
        self.firecrawl_key = fc_cfg.get("api_key", "")
        self._firecrawl_app = None

        self._client = httpx.AsyncClient(timeout=30)

    @property
    def firecrawl(self):
        """Lazy-init Firecrawl client."""
        if self._firecrawl_app is None and self.firecrawl_key:
            from firecrawl import FirecrawlApp
            self._firecrawl_app = FirecrawlApp(api_key=self.firecrawl_key)
        return self._firecrawl_app

    # ── Search providers ──────────────────────────────────────────────

    async def _search_firecrawl(self, query: str, limit: int = 5) -> list[dict]:
        """Search via Firecrawl API."""
        if not self.firecrawl:
            return []
        try:
            result = await asyncio.to_thread(
                self.firecrawl.search, query, {"limit": limit}
            )
            data = result.data if hasattr(result, "data") else (result if isinstance(result, list) else [])
            out = []
            for r in data:
                if isinstance(r, dict):
                    url = r.get("url", "")
                    title = r.get("title", "")
                    content = (r.get("markdown", "") or r.get("description", ""))[:500]
                else:
                    url = getattr(r, "url", "")
                    title = getattr(r, "title", "")
                    content = (getattr(r, "markdown", "") or getattr(r, "description", ""))[:500]
                if url:
                    out.append({"title": title, "url": url, "content": content})
            return out
        except Exception as e:
            logger.debug(f"Firecrawl search failed: {e}")
            return []

    async def _scrape_url(self, url: str) -> Optional[str]:
        """Deep-scrape a URL for full content via Firecrawl."""
        if not self.firecrawl:
            return None
        try:
            result = await asyncio.to_thread(
                self.firecrawl.scrape_url, url, {"formats": ["markdown"]}
            )
            if isinstance(result, dict):
                md = result.get("markdown", "")
            else:
                md = getattr(result, "markdown", "")
            return md[:2000] if md else None
        except Exception as e:
            logger.debug(f"Firecrawl scrape failed for {url}: {e}")
            return None

    async def _search_tavily(self, query: str, domains: list[str] = None) -> list[dict]:
        """Search via Tavily API."""
        if not self.tavily_key:
            return []
        try:
            payload = {
                "api_key": self.tavily_key,
                "query": query,
                "max_results": 5,
                "search_depth": "advanced",
            }
            if domains:
                payload["include_domains"] = domains
            resp = await self._client.post("https://api.tavily.com/search", json=payload)
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
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-L", "--max-time", "15",
                "--data-urlencode", f"q={query}",
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

    # ── Result filtering ────────────────────────────────────────────

    @staticmethod
    def _is_ad_or_spam(result: dict) -> bool:
        """Filter out advertisements, paid indicators, and spam."""
        text = (
            (result.get("title", "") + " " + result.get("content", ""))
            .lower()
        )
        url = result.get("url", "").lower()

        ad_phrases = [
            "buy now", "purchase now", "limited time", "discount code",
            "use coupon", "promo code", "free trial", "sign up today",
            "get started for", "premium indicator", "paid indicator",
            "join our vip", "vip group", "exclusive access", "subscribe now",
            "only $", "just $", "starting at $", "per month",
            "lifetime access", "money back guarantee", "act now",
            "don't miss out", "hurry", "sale ends", "special offer",
            "click here to buy", "order now", "unlock premium",
        ]
        ad_domains = [
            "gumroad.com", "patreon.com", "clickbank.com",
            "shopify.com", "etsy.com", "fiverr.com",
        ]

        for phrase in ad_phrases:
            if phrase in text:
                return True
        for domain in ad_domains:
            if domain in url:
                return True
        return False

    # ── Main search pipeline ──────────────────────────────────────────

    async def search_all_sources(self) -> list[dict]:
        """Search all source categories. Firecrawl → Tavily → DDG fallback."""
        all_results = []
        seen_urls = set()

        for cat_key, cat in SOURCE_CATEGORIES.items():
            for query in cat["queries"]:
                results = []
                try:
                    results = await self._search_firecrawl(query, limit=5)
                    if not results:
                        results = await self._search_tavily(query, domains=cat.get("tavily_domains"))
                    if not results:
                        results = await self._search_ddg(query)
                except Exception as e:
                    logger.warning(f"Scout search failed for {cat_key}: {e}")

                for r in results:
                    url = r["url"]
                    if url not in seen_urls and not self._is_ad_or_spam(r):
                        seen_urls.add(url)
                        r["source_category"] = cat["label"]
                        all_results.append(r)

                await asyncio.sleep(1.5)

        return all_results[:25]

    # Backward compat alias
    async def search_x(self) -> list[dict]:
        return await self.search_all_sources()

    # ── Report generation ─────────────────────────────────────────────

    async def _get_kalshi_snapshot(self) -> Optional[dict]:
        """Pull live Kalshi market data: balance, positions, top movers, hot markets."""
        try:
            from app.services.kalshi_client import get_async_kalshi_client
            client = get_async_kalshi_client()

            balance = await client.get_balance()
            balance_cents = balance.get("balance", 0) if isinstance(balance, dict) else balance
            positions = await client.get_positions()
            fills = await client.get_fills(limit=20)
            settlements = await client.get_settlements(limit=20)

            # Get active positions with non-zero count
            active_positions = [p for p in positions if p.get("position", 0) != 0]

            # Calculate P&L from settlements
            total_payout = sum(s.get("revenue", 0) for s in settlements)
            total_cost = sum(s.get("cost", 0) for s in settlements if s.get("cost"))

            # Try to get high-volume markets for opportunity scan
            hot_markets = []
            try:
                from app.services.kalshi_client import KalshiTradingClient
                sync_client = KalshiTradingClient()
                events = sync_client.get_events(limit=10, status="open")
                for event in events[:10]:
                    markets = event.get("markets", [])
                    for m in markets[:3]:
                        vol = m.get("volume", 0) or 0
                        yes_price = m.get("yes_ask", 0) or 0
                        no_price = m.get("no_ask", 0) or 0
                        if vol > 50:
                            hot_markets.append({
                                "ticker": m.get("ticker", ""),
                                "title": m.get("title", "")[:100],
                                "yes_price": yes_price,
                                "no_price": no_price,
                                "volume": vol,
                                "close_time": m.get("close_time", ""),
                            })
                hot_markets.sort(key=lambda x: x["volume"], reverse=True)
                hot_markets = hot_markets[:10]
            except Exception as e:
                logger.debug(f"Scout: couldn't fetch hot Kalshi markets: {e}")

            return {
                "balance_cents": balance_cents,
                "balance_usd": round(balance_cents / 100, 2) if isinstance(balance_cents, (int, float)) else 0,
                "active_positions": len(active_positions),
                "positions": active_positions[:10],
                "recent_fills": len(fills),
                "recent_settlements": len(settlements),
                "settlement_payout_cents": total_payout,
                "settlement_cost_cents": total_cost,
                "hot_markets": hot_markets,
            }
        except Exception as e:
            logger.warning(f"Scout: Kalshi snapshot failed: {e}")
            return None

    async def generate_report(self) -> dict:
        """Search all sources, deep-scrape top articles, and generate report."""
        logger.info("Scout: running multi-source intelligence scan")
        results = await self.search_all_sources()

        # Deep scrape top blog/news/analytics URLs (skip social)
        social_domains = {"x.com", "twitter.com", "reddit.com"}
        scrape_candidates = [
            r for r in results
            if not any(d in r.get("url", "") for d in social_domains)
        ][:5]

        for r in scrape_candidates:
            content = await self._scrape_url(r["url"])
            if content:
                r["full_content"] = content
                r["scraped"] = True

        # Pull live Kalshi market data
        kalshi_snapshot = await self._get_kalshi_snapshot()

        today = date.today().isoformat()
        scan_ts = datetime.utcnow().isoformat()
        for r in results:
            r["scanned_at"] = scan_ts

        report = {
            "date": today,
            "timestamp": scan_ts,
            "result_count": len(results),
            "sources_searched": list(SOURCE_CATEGORIES.keys()),
            "posts": results,
            "kalshi_snapshot": kalshi_snapshot,
        }

        SCOUT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = SCOUT_LOG_DIR / f"scout_{today}.json"
        with open(log_path, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Scout: found {len(results)} results across {len(SOURCE_CATEGORIES)} categories, saved to {log_path}")
        return report

    async def analyze_with_claude(self, posts: list[dict], today: str, kalshi_snapshot: dict = None) -> str:
        """Use Claude Sonnet to analyze posts and generate the formatted report."""
        if not posts and not kalshi_snapshot:
            return f"🚀 <b>Daily TradingView Bot Intelligence – {today}</b>\n\nNo new posts found."

        raw = "\n\n".join(
            f"[{i+1}] [{p.get('source_category', 'Unknown')}] {p.get('title', '')}\n"
            f"{p.get('full_content', p.get('content', ''))[:800]}\nURL: {p.get('url', '')}"
            for i, p in enumerate(posts)
        )

        # Append Kalshi market data if available
        kalshi_section = ""
        if kalshi_snapshot:
            kalshi_section = "\n\n--- KALSHI PREDICTION MARKET DATA ---\n"
            kalshi_section += f"Balance: ${kalshi_snapshot.get('balance_usd', 0)}\n"
            kalshi_section += f"Active positions: {kalshi_snapshot.get('active_positions', 0)}\n"
            kalshi_section += f"Recent fills: {kalshi_snapshot.get('recent_fills', 0)}\n"
            kalshi_section += f"Recent settlements: {kalshi_snapshot.get('recent_settlements', 0)}\n"
            hot = kalshi_snapshot.get("hot_markets", [])
            if hot:
                kalshi_section += "\nTop markets by volume:\n"
                for m in hot[:10]:
                    kalshi_section += (
                        f"  {m['ticker']}: {m['title']} | "
                        f"Yes: {m['yes_price']}¢ No: {m['no_price']}¢ | "
                        f"Vol: {m['volume']}\n"
                    )

        system_prompt = (
            "You are TradeBot Scout analyzing posts from multiple sources about TradingView bots, "
            "Pine Script, crypto trading signals, on-chain analytics, and Kalshi prediction markets. "
            "Sources include X.com, trading blogs, on-chain analytics sites, crypto news, and live Kalshi data. "
            "Produce a concise Telegram update using HTML formatting (bold with <b>, code with <code>). "
            "Maximum 550 words. Use this exact structure:\n\n"
            f"🚀 <b>Daily TradingView Bot Intelligence – {today}</b>\n\n"
            "<b>Overview:</b> [1-2 sentence summary of hottest topics]\n\n"
            "🔥 <b>Top Indicator & Strategy Suggestions:</b>\n"
            "• [Indicator/Strategy] – [one-line why useful for bots] (up to 5)\n\n"
            "📊 <b>On-Chain & Market Signals:</b>\n"
            "• [signal/insight] (up to 3)\n\n"
            "🎲 <b>Kalshi Prediction Markets:</b>\n"
            "• Account: [balance, positions, recent activity]\n"
            "• Hot markets: [top 3-5 interesting markets with prices and why they matter]\n"
            "• Edge opportunities: [any mispriced contracts, high-volume movers, or tail bets]\n\n"
            "<b>Other Key Insights:</b>\n"
            "• [bullet] (up to 3)\n\n"
            "📌 <b>Worth Reading:</b>\n"
            "1. [one-sentence summary] → [URL]\n"
            "(3-5 highest-value items with actual URLs from the data)\n\n"
            "Rules: be concise and actionable, include only real URLs from the provided data, "
            "prioritize real trader discussions and data-backed insights over hype. "
            "For Kalshi, highlight tail bets (1-20¢ or 80-99¢) and high-volume markets."
        )

        cfg = get("claude")
        mode = cfg.get("mode", "cli")
        model = cfg.get("model", "sonnet")

        try:
            if mode == "api":
                import anthropic
                api_key = cfg.get("api_key", "")
                if not api_key:
                    raise ValueError("No API key")
                client = anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    temperature=0.5,
                    system=system_prompt,
                    messages=[{"role": "user", "content": f"Analyze these posts:\n\n{raw}{kalshi_section}"}],
                )
                return msg.content[0].text.strip()
            else:
                import shutil
                cli_path = cfg.get("cli_path", "claude")
                resolved = shutil.which(cli_path)
                if not resolved:
                    raise FileNotFoundError("Claude CLI not found")

                full_prompt = f"{system_prompt}\n\nAnalyze these posts:\n\n{raw}{kalshi_section}"
                proc = await asyncio.create_subprocess_exec(
                    resolved, "--print", "--model", model, full_prompt,
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
        lines = [f"🚀 <b>Daily TradingView Bot Intelligence – {today}</b>\n", "📌 <b>Results Found:</b>"]
        for p in posts[:10]:
            title = (p.get("title") or "")[:100]
            url = p.get("url", "")
            cat = p.get("source_category", "")
            prefix = f"[{cat}] " if cat else ""
            line = f"• {prefix}<a href='{url}'>{title}</a>" if url else f"• {prefix}{title}"
            lines.append(line)
        return "\n".join(lines)

    async def format_telegram_message(self, report: dict) -> str:
        """Analyze posts with Claude Sonnet and format as Telegram message."""
        posts = report.get("posts", [])
        today = report.get("date", date.today().isoformat())
        kalshi_snapshot = report.get("kalshi_snapshot")
        msg = await self.analyze_with_claude(posts, today, kalshi_snapshot=kalshi_snapshot)
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
    target_time = dtime(3, 30)

    while True:
        now = datetime.now()
        target = datetime.combine(now.date(), target_time)

        if now >= target:
            target = datetime.combine(now.date(), target_time) + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Scout: next run at {target.strftime('%Y-%m-%d %H:%M')}, sleeping {wait_seconds/3600:.1f}h")

        await asyncio.sleep(wait_seconds)

        scout = ScoutService()
        try:
            await scout.run_and_notify()
        except Exception as e:
            logger.error(f"Scout scheduler error: {e}")
        finally:
            await scout.close()
