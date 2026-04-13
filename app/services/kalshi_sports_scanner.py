"""Kalshi Sports Scanner — monitors sports event markets across 7 leagues.

Scans MLB, NBA, NFL, NHL, Soccer, UFC, and WNBA markets on Kalshi.
Tracks odds movement, identifies value bets, and sends Telegram alerts.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.sports")

# Sports categories and their Kalshi event prefixes / search terms
SPORTS_LEAGUES = {
    "MLB": {"keywords": ["mlb", "baseball", "yankees", "dodgers", "mets", "astros", "braves", "phillies", "cubs", "red sox"], "emoji": "\u26be"},
    "NBA": {"keywords": ["nba", "basketball", "lakers", "celtics", "nuggets", "warriors", "bucks", "76ers", "knicks", "heat"], "emoji": "\ud83c\udfc0"},
    "NFL": {"keywords": ["nfl", "football", "super bowl", "chiefs", "eagles", "49ers", "ravens", "cowboys", "bills", "lions"], "emoji": "\ud83c\udfc8"},
    "NHL": {"keywords": ["nhl", "hockey", "stanley cup", "bruins", "rangers", "oilers", "panthers", "avalanche", "maple leafs"], "emoji": "\ud83c\udfd2"},
    "Soccer": {"keywords": ["soccer", "mls", "premier league", "world cup", "champions league", "la liga", "epl", "fifa"], "emoji": "\u26bd"},
    "UFC": {"keywords": ["ufc", "mma", "fight", "boxing", "ppv", "bellator"], "emoji": "\ud83e\udd4a"},
    "WNBA": {"keywords": ["wnba", "women's basketball", "aces", "liberty", "storm", "lynx", "sparks", "mercury"], "emoji": "\ud83c\udfc0"},
}


class SportsMarket:
    """A detected sports market with odds data."""

    def __init__(self, ticker: str, event_ticker: str, title: str, league: str,
                 yes_price: int, no_price: int, volume: int, close_time: str):
        self.ticker = ticker
        self.event_ticker = event_ticker
        self.title = title
        self.league = league
        self.yes_price = yes_price
        self.no_price = no_price
        self.volume = volume
        self.close_time = close_time
        self.prev_yes_price: Optional[int] = None
        self.price_change: int = 0
        self.detected_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "title": self.title,
            "league": self.league,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "yes_pct": f"{self.yes_price}%",
            "volume": self.volume,
            "close_time": self.close_time,
            "prev_yes_price": self.prev_yes_price,
            "price_change": self.price_change,
            "detected_at": self.detected_at,
        }


class ValueBet:
    """A detected value bet opportunity."""

    def __init__(self, market: SportsMarket, reason: str, edge_cents: int):
        self.market = market
        self.reason = reason
        self.edge_cents = edge_cents
        self.detected_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            **self.market.to_dict(),
            "reason": self.reason,
            "edge_cents": self.edge_cents,
            "edge_usd": round(self.edge_cents / 100, 2),
        }


class KalshiSportsScanner:
    """Scans Kalshi sports markets across multiple leagues."""

    def __init__(self):
        cfg = get("kalshi") or {}
        sports_cfg = cfg.get("sports_scanner", {})

        self.enabled = sports_cfg.get("enabled", False)
        self.scan_interval = sports_cfg.get("scan_interval_seconds", 120)
        self.leagues = sports_cfg.get("leagues", list(SPORTS_LEAGUES.keys()))
        self.min_volume = sports_cfg.get("min_volume", 10)
        self.max_days_to_close = sports_cfg.get("max_days_to_close", 0)
        self.odds_move_alert_cents = sports_cfg.get("odds_move_alert_cents", 10)
        self.value_threshold_cents = sports_cfg.get("value_threshold_cents", 5)
        self.max_markets_per_league = sports_cfg.get("max_markets_per_league", 20)
        self.auto_trade = sports_cfg.get("auto_trade", False)
        self.contracts_per_trade = sports_cfg.get("contracts_per_trade", 5)
        self.max_cost_per_trade_cents = sports_cfg.get("max_cost_per_trade_cents", 500)
        self.max_positions = sports_cfg.get("max_positions", 5)
        self.telegram_alerts = sports_cfg.get("telegram_alerts", True)

        # State
        self._markets: dict[str, SportsMarket] = {}  # ticker -> SportsMarket
        self._value_bets: list[ValueBet] = []
        self._league_counts: dict[str, int] = {league: 0 for league in SPORTS_LEAGUES}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._scan_count = 0
        self._last_scan: Optional[str] = None
        self._trades_executed = 0

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(f"Sports scanner started — leagues: {', '.join(self.leagues)}")
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Sports scanner stopped")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.scan()
            except Exception as e:
                logger.error(f"Sports scan error: {e}")
            await asyncio.sleep(self.scan_interval)

    def _classify_league(self, title: str) -> Optional[str]:
        """Match a market title to a sports league."""
        title_lower = title.lower()
        for league, info in SPORTS_LEAGUES.items():
            if league not in self.leagues:
                continue
            for kw in info["keywords"]:
                if kw in title_lower:
                    return league
        return None

    async def scan(self) -> dict:
        """Scan all active sports markets."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return {"error": "Kalshi client not enabled"}

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        new_value_bets = []

        try:
            # Fetch all open markets and filter for sports
            all_markets = await client.discover_active_markets(min_volume=0, max_days_to_close=self.max_days_to_close)
            league_counts = {league: 0 for league in SPORTS_LEAGUES}

            for m in all_markets:
                title = m.get("title", "") + " " + m.get("subtitle", "")
                league = self._classify_league(title)
                if not league:
                    continue

                ticker = m.get("ticker", "")
                event_ticker = m.get("event_ticker", "")
                yes_price = int(round(float(m.get("yes_bid_dollars", "0") or m.get("last_price_dollars", "0") or "0") * 100))
                no_price = int(round(float(m.get("no_bid_dollars", "0") or "0") * 100)) or (100 - yes_price if yes_price else 0)
                volume = int(float(m.get("volume_fp", "0") or "0"))
                close_time = m.get("close_time", "") or m.get("expiration_time", "")

                league_counts[league] = league_counts.get(league, 0) + 1

                if volume < self.min_volume:
                    continue

                sport_market = SportsMarket(
                    ticker=ticker,
                    event_ticker=event_ticker,
                    title=m.get("title", ticker),
                    league=league,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    close_time=close_time,
                )

                # Track odds movement
                if ticker in self._markets:
                    prev = self._markets[ticker]
                    sport_market.prev_yes_price = prev.yes_price
                    sport_market.price_change = yes_price - prev.yes_price

                    # Alert on large odds movement
                    if abs(sport_market.price_change) >= self.odds_move_alert_cents:
                        direction = "UP" if sport_market.price_change > 0 else "DOWN"
                        vb = ValueBet(
                            market=sport_market,
                            reason=f"Odds moved {direction} {abs(sport_market.price_change)}¢",
                            edge_cents=abs(sport_market.price_change),
                        )
                        new_value_bets.append(vb)

                # Check for value: yes + no spread
                spread = yes_price + no_price
                if spread > 0 and spread < (100 - self.value_threshold_cents):
                    vb = ValueBet(
                        market=sport_market,
                        reason=f"Spread underpriced: YES({yes_price}¢) + NO({no_price}¢) = {spread}¢",
                        edge_cents=100 - spread,
                    )
                    new_value_bets.append(vb)

                self._markets[ticker] = sport_market

            self._league_counts = league_counts

            # Store value bets
            if new_value_bets:
                self._value_bets = new_value_bets + self._value_bets
                self._value_bets = self._value_bets[:100]  # Keep last 100

                if self.telegram_alerts:
                    await self._send_alert(new_value_bets)

                if self.auto_trade:
                    await self._auto_execute(new_value_bets)

        except Exception as e:
            logger.error(f"Sports scan error: {e}")

        return {
            "scan_count": self._scan_count,
            "markets_found": len(self._markets),
            "new_value_bets": len(new_value_bets),
            "league_counts": league_counts,
        }

    async def _send_alert(self, value_bets: list[ValueBet]):
        from app.services.telegram_service import TelegramService
        tg = TelegramService()

        lines = [f"<b>\ud83c\udfc6 Sports Scanner ({len(value_bets)} signals)</b>\n"]
        for vb in value_bets[:5]:
            emoji = SPORTS_LEAGUES.get(vb.market.league, {}).get("emoji", "\ud83c\udfc6")
            lines.append(
                f"{emoji} <b>{vb.market.league}</b> — {vb.market.title[:50]}\n"
                f"   {vb.reason} | Vol: {vb.market.volume}\n"
            )
        await tg.send_message("\n".join(lines))

    async def _auto_execute(self, value_bets: list[ValueBet]):
        """Auto-trade on the best value bet."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return

        # Only trade the best edge
        best = max(value_bets, key=lambda vb: vb.edge_cents)
        if best.edge_cents < self.value_threshold_cents:
            return

        # Check position limits
        try:
            positions = await client.get_positions()
            open_count = len([p for p in positions if p.get("count", 0) > 0])
            if open_count >= self.max_positions:
                logger.info(f"Sports auto-trade skipped: {open_count}/{self.max_positions} positions")
                return
        except Exception:
            return

        # Buy YES if underpriced (price < 50), NO otherwise
        side = "yes" if best.market.yes_price < 50 else "no"
        price = best.market.yes_price if side == "yes" else best.market.no_price
        count = self.contracts_per_trade

        cost = count * price
        if cost > self.max_cost_per_trade_cents:
            return

        # Risk auditor gate — liquidity sizing, category limits, dead zone check
        try:
            from app.services.kalshi_risk_manager import get_risk_manager
            rm = get_risk_manager()
            if rm.enabled:
                audit = rm.audit_trade(
                    ticker=best.market.ticker, side=side, price_cents=price,
                    count=count, confidence=0.5,
                    bot_name="sports", title=best.market.title,
                )
                if not audit["approved"]:
                    logger.info(f"Sports trade BLOCKED by auditor: {audit['reason']}")
                    return
                if audit.get("adjustments", {}).get("count"):
                    count = audit["adjustments"]["count"]
        except Exception as e:
            logger.warning(f"Sports risk audit failed (allowing trade): {e}")

        try:
            result = await client.place_order(
                ticker=best.market.ticker,
                side=side,
                action="buy",
                count=count,
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
                order_type="limit",
            )
            self._trades_executed += 1
            logger.info(f"Sports auto-trade: {side.upper()} {count}x @{price}¢ on {best.market.ticker}")
        except Exception as e:
            logger.error(f"Sports auto-trade failed: {e}")

    def get_markets_by_league(self, league: Optional[str] = None) -> list[dict]:
        """Get tracked sports markets, optionally filtered by league."""
        markets = sorted(self._markets.values(), key=lambda m: m.volume, reverse=True)
        if league:
            markets = [m for m in markets if m.league == league]
        return [m.to_dict() for m in markets[:100]]

    def get_value_bets(self, limit: int = 50) -> list[dict]:
        return [vb.to_dict() for vb in self._value_bets[:limit]]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan,
            "total_markets": len(self._markets),
            "league_counts": self._league_counts,
            "value_bets": len(self._value_bets),
            "trades_executed": self._trades_executed,
            "auto_trade": self.auto_trade,
            "leagues": self.leagues,
            "max_positions": self.max_positions,
        }


_scanner: Optional[KalshiSportsScanner] = None


def get_sports_scanner() -> KalshiSportsScanner:
    global _scanner
    if _scanner is None:
        _scanner = KalshiSportsScanner()
    return _scanner
