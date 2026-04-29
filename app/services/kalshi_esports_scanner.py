"""Kalshi Esports Scanner — monitors esports event markets.

Scans DotA 2, CS2, League of Legends, Valorant, and other esports
markets on Kalshi. Tracks odds, detects value, and alerts on opportunities.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.esports")

# Esports games and their Kalshi search terms
ESPORTS_GAMES = {
    "CS2": {"keywords": ["counter-strike", "cs2", "csgo", "cs:go", "counter strike", "blast premier", "esl pro", "iem "], "emoji": "\U0001F52B"},
    "DotA2": {"keywords": ["dota", "dota 2", "dota2", "the international", "ti1", "esl dota"], "emoji": "\u2694\ufe0f"},
    "LoL": {"keywords": ["league of legends", "lol ", "lol:", "lck", "lec", "lcs", "worlds 202", "msi 202"], "emoji": "\U0001F3AE"},
    "Valorant": {"keywords": ["valorant", "vct", "champions tour"], "emoji": "\U0001F3AF"},
    "Overwatch": {"keywords": ["overwatch", "owl ", "overwatch league"], "emoji": "\U0001F31F"},
}


class EsportsMarket:
    """A detected esports market with odds data."""

    def __init__(self, ticker: str, event_ticker: str, title: str, game: str,
                 yes_price: int, no_price: int, volume: int, close_time: str):
        self.ticker = ticker
        self.event_ticker = event_ticker
        self.title = title
        self.game = game
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
            "game": self.game,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "yes_pct": f"{self.yes_price}%",
            "volume": self.volume,
            "close_time": self.close_time,
            "prev_yes_price": self.prev_yes_price,
            "price_change": self.price_change,
            "detected_at": self.detected_at,
        }


class EsportsValueBet:
    """A detected esports value bet opportunity."""

    def __init__(self, market: EsportsMarket, reason: str, edge_cents: int):
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


class KalshiEsportsScanner:
    """Scans Kalshi esports markets across multiple games."""

    def __init__(self):
        cfg = get("kalshi") or {}
        esports_cfg = cfg.get("esports_scanner", {})

        self.enabled = esports_cfg.get("enabled", False)
        self.scan_interval = esports_cfg.get("scan_interval_seconds", 120)
        self.games = esports_cfg.get("games", list(ESPORTS_GAMES.keys()))
        self.min_volume = esports_cfg.get("min_volume", 5)
        self.max_days_to_close = esports_cfg.get("max_days_to_close", 0)
        self.odds_move_alert_cents = esports_cfg.get("odds_move_alert_cents", 10)
        self.value_threshold_cents = esports_cfg.get("value_threshold_cents", 5)
        self.max_markets_per_game = esports_cfg.get("max_markets_per_game", 20)
        self.auto_trade = esports_cfg.get("auto_trade", False)
        self.contracts_per_trade = esports_cfg.get("contracts_per_trade", 5)
        self.max_cost_per_trade_cents = esports_cfg.get("max_cost_per_trade_cents", 500)
        self.max_positions = esports_cfg.get("max_positions", 5)
        self.telegram_alerts = esports_cfg.get("telegram_alerts", True)

        # State
        self._markets: dict[str, EsportsMarket] = {}
        self._value_bets: list[EsportsValueBet] = []
        self._game_counts: dict[str, int] = {game: 0 for game in ESPORTS_GAMES}
        self._owned_tickers: set[str] = set()  # Tickers this scanner opened
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
        logger.info(f"Esports scanner started — games: {', '.join(self.games)}")
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Esports scanner stopped")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.scan()
            except Exception as e:
                logger.error(f"Esports scan error: {e}")
            await asyncio.sleep(self.scan_interval)

    # Series ticker → game mapping for reliable classification
    SERIES_TO_GAME = {
        "KXLOLGAME": "LoL",
        "KXCS2GAME": "CS2", "KXCS2MATCH": "CS2",
        "KXDOTAGAME": "DotA2",
        "KXVALGAME": "Valorant", "KXVALMATCH": "Valorant",
        "KXOWGAME": "Overwatch",
    }

    def _classify_game(self, title: str, series_ticker: str = "") -> Optional[str]:
        """Match a market to an esports game via series ticker or title keywords."""
        # Fast path: series ticker gives definitive game
        if series_ticker:
            game = self.SERIES_TO_GAME.get(series_ticker)
            if game and game in self.games:
                return game

        # Fallback: keyword matching on title
        title_lower = title.lower()
        for game, info in ESPORTS_GAMES.items():
            if game not in self.games:
                continue
            for kw in info["keywords"]:
                if kw in title_lower:
                    return game
        return None

    async def scan(self) -> dict:
        """Scan all active esports markets."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return {"error": "Kalshi client not enabled"}

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        new_value_bets = []

        try:
            all_markets = await client.discover_active_markets(min_volume=0, max_days_to_close=self.max_days_to_close)
            game_counts = {game: 0 for game in ESPORTS_GAMES}

            for m in all_markets:
                title = m.get("title", "") + " " + m.get("subtitle", "")
                series_ticker = m.get("_series", m.get("series_ticker", ""))
                game = self._classify_game(title, series_ticker)
                if not game:
                    continue

                ticker = m.get("ticker", "")
                event_ticker = m.get("event_ticker", "")
                yes_price = int(round(float(m.get("yes_bid_dollars", "0") or m.get("last_price_dollars", "0") or "0") * 100))
                no_price = int(round(float(m.get("no_bid_dollars", "0") or "0") * 100)) or (100 - yes_price if yes_price else 0)
                volume = int(float(m.get("volume_fp", "0") or "0"))
                close_time = m.get("close_time", "") or m.get("expiration_time", "")

                game_counts[game] = game_counts.get(game, 0) + 1

                if volume < self.min_volume:
                    continue

                esports_market = EsportsMarket(
                    ticker=ticker,
                    event_ticker=event_ticker,
                    title=m.get("title", ticker),
                    game=game,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    close_time=close_time,
                )

                # Track odds movement
                if ticker in self._markets:
                    prev = self._markets[ticker]
                    esports_market.prev_yes_price = prev.yes_price
                    esports_market.price_change = yes_price - prev.yes_price

                    if abs(esports_market.price_change) >= self.odds_move_alert_cents:
                        direction = "UP" if esports_market.price_change > 0 else "DOWN"
                        vb = EsportsValueBet(
                            market=esports_market,
                            reason=f"Odds moved {direction} {abs(esports_market.price_change)}\u00a2",
                            edge_cents=abs(esports_market.price_change),
                        )
                        new_value_bets.append(vb)

                # Check for value: yes + no spread
                spread = yes_price + no_price
                if spread > 0 and spread < (100 - self.value_threshold_cents):
                    vb = EsportsValueBet(
                        market=esports_market,
                        reason=f"Spread underpriced: YES({yes_price}\u00a2) + NO({no_price}\u00a2) = {spread}\u00a2",
                        edge_cents=100 - spread,
                    )
                    new_value_bets.append(vb)

                self._markets[ticker] = esports_market

            self._game_counts = game_counts

            if new_value_bets:
                self._value_bets = new_value_bets + self._value_bets
                self._value_bets = self._value_bets[:100]

                if self.auto_trade:
                    await self._auto_execute(new_value_bets)

        except Exception as e:
            logger.error(f"Esports scan error: {e}")

        return {
            "scan_count": self._scan_count,
            "markets_found": len(self._markets),
            "new_value_bets": len(new_value_bets),
            "game_counts": game_counts,
        }

    async def _send_alert(self, value_bets: list[EsportsValueBet]):
        from app.services.telegram_service import TelegramService
        tg = TelegramService()

        lines = ["<b>\U0001F3AE Esports Scanner ({} signals)</b>\n".format(len(value_bets))]
        for vb in value_bets[:5]:
            emoji = ESPORTS_GAMES.get(vb.market.game, {}).get("emoji", "\U0001F3AE")
            lines.append(
                f"{emoji} <b>{vb.market.game}</b> \u2014 {vb.market.title[:50]}\n"
                f"   {vb.reason} | Vol: {vb.market.volume}\n"
            )
        await tg.send_message("\n".join(lines))

    async def _auto_execute(self, value_bets: list[EsportsValueBet]):
        """Auto-trade on the best value bet."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return

        best = max(value_bets, key=lambda vb: vb.edge_cents)
        if best.edge_cents < self.value_threshold_cents:
            return

        # Count only positions this scanner opened (not other bots holding esports markets).
        # Reconcile self._owned_tickers against live positions: drop tickers that closed.
        try:
            positions = await client.get_positions()
            live_open = {
                p.get("ticker", "").upper()
                for p in positions
                if abs(p.get("count", 0) or 0) > 0
            }
            self._owned_tickers = {t for t in self._owned_tickers if t.upper() in live_open}
            owned_count = len(self._owned_tickers)
            if owned_count >= self.max_positions:
                logger.info(
                    f"Esports auto-trade skipped: {owned_count}/{self.max_positions} scanner-owned positions"
                )
                return
        except Exception as e:
            logger.warning(f"Esports position reconcile failed (allowing trade): {e}")

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
                    bot_name="esports", title=best.market.title,
                )
                if not audit["approved"]:
                    logger.info(f"Esports trade BLOCKED by auditor: {audit['reason']}")
                    return
                if audit.get("adjustments", {}).get("count"):
                    count = audit["adjustments"]["count"]
        except Exception as e:
            logger.warning(f"Esports risk audit failed (allowing trade): {e}")

        try:
            client_order_id = f"esports-{uuid.uuid4().hex[:8]}"
            await client.place_order(
                ticker=best.market.ticker,
                side=side,
                action="buy",
                count=count,
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
                order_type="limit",
                client_order_id=client_order_id,
            )
            self._trades_executed += 1
            self._owned_tickers.add(best.market.ticker)
            logger.info(f"Esports auto-trade: {side.upper()} {count}x @{price}¢ on {best.market.ticker}")
        except Exception as e:
            logger.error(f"Esports auto-trade failed: {e}")

    def get_markets_by_game(self, game: Optional[str] = None) -> list[dict]:
        """Get tracked esports markets, optionally filtered by game."""
        markets = sorted(self._markets.values(), key=lambda m: m.volume, reverse=True)
        if game:
            markets = [m for m in markets if m.game == game]
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
            "game_counts": self._game_counts,
            "value_bets": len(self._value_bets),
            "trades_executed": self._trades_executed,
            "auto_trade": self.auto_trade,
            "games": self.games,
            "max_positions": self.max_positions,
            "owned_tickers": sorted(self._owned_tickers),
        }


_scanner: Optional[KalshiEsportsScanner] = None


def get_esports_scanner() -> KalshiEsportsScanner:
    global _scanner
    if _scanner is None:
        _scanner = KalshiEsportsScanner()
    return _scanner
