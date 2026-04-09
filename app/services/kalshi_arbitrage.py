"""Kalshi arbitrage scanner — detects mispriced markets for risk-free profit.

Strategies:
1. Yes/No Spread: Buy both sides when yes_ask + no_ask < 100¢ (minus fees)
2. Bracket Sum: Multi-outcome event brackets that don't sum to 100%
3. Cross-event correlation: Related events with inconsistent pricing
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.arb")


class ArbitrageOpportunity:
    """A detected arbitrage opportunity."""

    def __init__(
        self,
        strategy: str,
        market_ticker: str,
        title: str,
        spread_cents: float,
        profit_per_contract_cents: float,
        max_contracts: int,
        max_profit_cents: float,
        details: dict,
    ):
        self.strategy = strategy
        self.market_ticker = market_ticker
        self.title = title
        self.spread_cents = spread_cents
        self.profit_per_contract_cents = profit_per_contract_cents
        self.max_contracts = max_contracts
        self.max_profit_cents = max_profit_cents
        self.details = details
        self.detected_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "market_ticker": self.market_ticker,
            "title": self.title,
            "spread_cents": self.spread_cents,
            "profit_per_contract_cents": self.profit_per_contract_cents,
            "max_contracts": self.max_contracts,
            "max_profit_cents": self.max_profit_cents,
            "details": self.details,
            "detected_at": self.detected_at,
        }


class KalshiArbitrageScanner:
    """Scans Kalshi markets for arbitrage opportunities."""

    def __init__(self):
        cfg = get("kalshi") or {}
        arb_cfg = cfg.get("arbitrage", {})
        self.enabled = arb_cfg.get("enabled", False)
        self.scan_interval = arb_cfg.get("scan_interval_seconds", 120)
        self.min_spread_cents = arb_cfg.get("min_spread_cents", 3)
        self.min_profit_cents = arb_cfg.get("min_profit_cents", 5)
        self.fee_per_contract_cents = arb_cfg.get("fee_per_contract_cents", 2)
        self.auto_execute = arb_cfg.get("auto_execute", False)
        self.max_auto_cost_cents = arb_cfg.get("max_auto_cost_cents", 500)
        self.telegram_alerts = arb_cfg.get("telegram_alerts", True)
        self.categories = cfg.get("categories", ["economics", "crypto", "politics", "finance"])

        self._opportunities: list[ArbitrageOpportunity] = []
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_scan: Optional[str] = None
        self._scan_count = 0
        self._total_found = 0

    def start(self) -> asyncio.Task:
        """Start the background scanning loop."""
        if self._scan_task and not self._scan_task.done():
            logger.warning("Arbitrage scanner already running")
            return self._scan_task

        self._running = True
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info(
            f"Kalshi arbitrage scanner started "
            f"(interval={self.scan_interval}s, min_spread={self.min_spread_cents}¢)"
        )
        return self._scan_task

    def stop(self):
        """Stop the scanner."""
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        logger.info("Kalshi arbitrage scanner stopped")

    async def _scan_loop(self):
        """Background loop that scans for opportunities."""
        while self._running:
            try:
                await self.scan_all()
            except Exception as e:
                logger.error(f"Arbitrage scan error: {e}")

            await asyncio.sleep(self.scan_interval)

    async def scan_all(self) -> list[dict]:
        """Run all arbitrage scans and return opportunities."""
        from app.services.kalshi_client import get_async_kalshi_client

        client = get_async_kalshi_client()
        if not client.enabled:
            return []

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        new_opps = []

        try:
            # Fetch all open markets with full pricing data (direct API)
            markets = await client.discover_active_markets(min_volume=10)
            logger.info(f"Arbitrage scan #{self._scan_count}: checking {len(markets)} markets")

            # Strategy 1: Yes/No spread arbitrage
            spread_opps = await self._scan_yes_no_spreads(client, markets)
            new_opps.extend(spread_opps)

            # Strategy 2: Bracket sum arbitrage
            bracket_opps = await self._scan_bracket_sums(client, markets)
            new_opps.extend(bracket_opps)

            # Strategy 3: Orderbook depth arbitrage (crossed books)
            crossed_opps = await self._scan_crossed_orderbooks(client, markets)
            new_opps.extend(crossed_opps)

            self._opportunities = new_opps
            self._total_found += len(new_opps)

            if new_opps:
                logger.info(
                    f"Found {len(new_opps)} arbitrage opportunities "
                    f"(best: {new_opps[0].profit_per_contract_cents:.1f}¢/contract)"
                )
                # Send Telegram alerts for significant opportunities
                if self.telegram_alerts:
                    await self._send_alerts(new_opps)

                # Auto-execute if enabled
                if self.auto_execute:
                    await self._auto_execute(new_opps, client)

        except Exception as e:
            logger.error(f"Scan error: {e}")

        return [o.to_dict() for o in new_opps]

    async def _scan_yes_no_spreads(self, client, markets: list) -> list[ArbitrageOpportunity]:
        """Find markets where yes_ask + no_ask < 100¢.

        On Kalshi, buying YES at yes_ask and NO at no_ask on the same market
        guarantees one side pays $1.00 at settlement. If the combined cost
        is less than $1.00 minus fees, it's a risk-free profit.
        """
        opportunities = []

        for m in markets:
            # API returns dollar-denominated strings
            yes_ask_str = m.get("yes_ask_dollars") or "0"
            no_ask_str = m.get("no_ask_dollars") or "0"
            yes_ask = int(round(float(yes_ask_str) * 100))
            no_ask = int(round(float(no_ask_str) * 100))

            if yes_ask <= 0 or no_ask <= 0:
                continue

            total_cost = yes_ask + no_ask
            # Settlement pays 100¢, minus fees on both sides
            net_profit = 100 - total_cost - self.fee_per_contract_cents

            if net_profit >= self.min_spread_cents:
                # Check liquidity — how many contracts available at these prices
                volume = int(float(m.get("volume_fp", "0") or "0"))
                # Conservative: assume we can get min(100, volume/10) contracts
                max_contracts = min(100, max(1, volume // 10)) if volume > 0 else 1
                max_profit = net_profit * max_contracts

                if max_profit >= self.min_profit_cents:
                    opp = ArbitrageOpportunity(
                        strategy="yes_no_spread",
                        market_ticker=m.get("ticker", ""),
                        title=m.get("title", m.get("ticker", "")),
                        spread_cents=100 - total_cost,
                        profit_per_contract_cents=net_profit,
                        max_contracts=max_contracts,
                        max_profit_cents=max_profit,
                        details={
                            "yes_ask": yes_ask,
                            "no_ask": no_ask,
                            "total_cost": total_cost,
                            "fee": self.fee_per_contract_cents,
                            "volume": volume,
                            "close_time": m.get("close_time", ""),
                            "event_ticker": m.get("event_ticker", ""),
                        },
                    )
                    opportunities.append(opp)

        # Sort by profit per contract descending
        opportunities.sort(key=lambda o: o.profit_per_contract_cents, reverse=True)
        return opportunities

    async def _scan_bracket_sums(self, client, markets: list) -> list[ArbitrageOpportunity]:
        """Find bracket events where the sum of all yes_ask prices != 100¢.

        Bracket markets (e.g., "BTC price 90K-95K", "95K-100K", "100K-105K"...)
        must sum to exactly 100¢ since one outcome is guaranteed. If the sum of
        ask prices is < 100¢, buying all brackets guarantees profit.
        """
        opportunities = []

        # Group markets by event_ticker
        events: dict[str, list] = {}
        for m in markets:
            event_ticker = m.get("event_ticker", "")
            if event_ticker:
                events.setdefault(event_ticker, []).append(m)

        for event_ticker, event_markets in events.items():
            # Only look at events with multiple markets (bracket-style)
            if len(event_markets) < 3:
                continue

            # Sum of yes_ask across all brackets
            yes_asks = []
            valid = True
            for m in event_markets:
                ask_str = m.get("yes_ask_dollars") or "0"
                ask = int(round(float(ask_str) * 100))
                if ask <= 0:
                    valid = False
                    break
                yes_asks.append(ask)

            if not valid:
                continue

            total_ask_sum = sum(yes_asks)
            # If sum < 100, buying all brackets costs less than the guaranteed $1 payout
            spread = 100 - total_ask_sum
            # Fees: we pay fee on settlement of the winning bracket only
            net_profit = spread - self.fee_per_contract_cents

            if net_profit >= self.min_spread_cents:
                min_volume = min(int(float(m.get("volume_fp", "0") or "0")) for m in event_markets)
                max_contracts = min(50, max(1, min_volume // 10)) if min_volume > 0 else 1
                max_profit = net_profit * max_contracts

                if max_profit >= self.min_profit_cents:
                    event_title = event_markets[0].get("event_ticker", event_ticker)
                    for m in event_markets:
                        if m.get("subtitle"):
                            event_title = m["subtitle"]
                            break

                    bracket_details = [
                        {"ticker": m.get("ticker", ""), "title": m.get("title", ""),
                         "yes_ask": int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))}
                        for m in event_markets
                    ]

                    opp = ArbitrageOpportunity(
                        strategy="bracket_sum",
                        market_ticker=event_ticker,
                        title=f"Bracket: {event_title} ({len(event_markets)} markets)",
                        spread_cents=spread,
                        profit_per_contract_cents=net_profit,
                        max_contracts=max_contracts,
                        max_profit_cents=max_profit,
                        details={
                            "event_ticker": event_ticker,
                            "bracket_count": len(event_markets),
                            "total_ask_sum": total_ask_sum,
                            "brackets": bracket_details,
                            "fee": self.fee_per_contract_cents,
                            "min_volume": min_volume,
                        },
                    )
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.profit_per_contract_cents, reverse=True)
        return opportunities

    async def _scan_crossed_orderbooks(self, client, markets: list) -> list[ArbitrageOpportunity]:
        """Find markets with crossed orderbooks (yes_bid > yes_ask or similar anomalies).

        A crossed book means someone is willing to buy at a higher price than
        the current ask — instant fill at a profit. These are rare but happen
        during fast-moving events.
        """
        opportunities = []

        # Only check high-volume markets (crossed books in illiquid markets are usually stale)
        active_markets = [m for m in markets if int(float(m.get("volume_fp", "0") or "0")) > 50]

        for m in active_markets:
            yes_bid = int(round(float(m.get("yes_bid_dollars", "0") or "0") * 100))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            no_bid = int(round(float(m.get("no_bid_dollars", "0") or "0") * 100))
            no_ask = int(round(float(m.get("no_ask_dollars", "0") or "0") * 100))
            volume = int(float(m.get("volume_fp", "0") or "0"))

            # Crossed: yes_bid > yes_ask (can buy at ask, immediately sell at bid)
            if yes_bid > yes_ask > 0:
                profit = yes_bid - yes_ask - self.fee_per_contract_cents
                if profit >= self.min_spread_cents:
                    max_c = min(50, max(1, volume // 20))
                    opp = ArbitrageOpportunity(
                        strategy="crossed_book",
                        market_ticker=m.get("ticker", ""),
                        title=m.get("title", ""),
                        spread_cents=yes_bid - yes_ask,
                        profit_per_contract_cents=profit,
                        max_contracts=max_c,
                        max_profit_cents=profit * max_c,
                        details={
                            "type": "yes_crossed",
                            "yes_bid": yes_bid,
                            "yes_ask": yes_ask,
                            "volume": volume,
                        },
                    )
                    opportunities.append(opp)

            # Also check: no_bid > no_ask
            if no_bid > no_ask > 0:
                profit = no_bid - no_ask - self.fee_per_contract_cents
                if profit >= self.min_spread_cents:
                    max_c = min(50, max(1, volume // 20))
                    opp = ArbitrageOpportunity(
                        strategy="crossed_book",
                        market_ticker=m.get("ticker", ""),
                        title=m.get("title", ""),
                        spread_cents=no_bid - no_ask,
                        profit_per_contract_cents=profit,
                        max_contracts=max_c,
                        max_profit_cents=profit * max_c,
                        details={
                            "type": "no_crossed",
                            "no_bid": no_bid,
                            "no_ask": no_ask,
                            "volume": volume,
                        },
                    )
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.profit_per_contract_cents, reverse=True)
        return opportunities

    async def _send_alerts(self, opportunities: list[ArbitrageOpportunity]):
        """Send Telegram alerts for significant opportunities."""
        from app.services.telegram_service import TelegramService

        tg = TelegramService()
        # Only alert on opportunities above the minimum profit threshold
        significant = [o for o in opportunities if o.max_profit_cents >= self.min_profit_cents * 2]
        if not significant:
            return

        lines = ["<b>🎯 Kalshi Arbitrage Alert</b>\n"]
        for opp in significant[:5]:  # Top 5 max
            emoji = {"yes_no_spread": "📊", "bracket_sum": "📐", "crossed_book": "⚡"}.get(opp.strategy, "💰")
            lines.append(
                f"{emoji} <b>{opp.strategy.replace('_', ' ').title()}</b>\n"
                f"   {opp.title[:50]}\n"
                f"   Spread: {opp.spread_cents}¢ | Profit: {opp.profit_per_contract_cents}¢/contract\n"
                f"   Max profit: ${opp.max_profit_cents/100:.2f} ({opp.max_contracts} contracts)\n"
            )

        lines.append(f"\n<i>Scan #{self._scan_count} | {len(opportunities)} total opportunities</i>")
        await tg.send_message("\n".join(lines))

    async def _auto_execute(self, opportunities: list[ArbitrageOpportunity], client):
        """Auto-execute the best opportunity if within risk limits."""
        from app.database import insert_kalshi_trade

        if not opportunities:
            return

        best = opportunities[0]
        cost = best.details.get("total_cost", 0) * best.max_contracts
        if cost > self.max_auto_cost_cents:
            logger.info(
                f"Skipping auto-execute: cost {cost}¢ exceeds max {self.max_auto_cost_cents}¢"
            )
            return

        if best.strategy == "yes_no_spread":
            try:
                yes_price = best.details["yes_ask"]
                no_price = best.details["no_ask"]
                count = min(best.max_contracts, 10)  # Conservative: max 10 contracts auto

                # Buy YES side
                yes_result = await client.place_order(
                    ticker=best.market_ticker,
                    side="yes",
                    action="buy",
                    yes_price=yes_price,
                    count=count,
                )

                # Buy NO side
                no_result = await client.place_order(
                    ticker=best.market_ticker,
                    side="no",
                    action="buy",
                    no_price=no_price,
                    count=count,
                )

                # Log both trades
                for side, result, price in [("yes", yes_result, yes_price), ("no", no_result, no_price)]:
                    insert_kalshi_trade({
                        "order_id": result.get("order", {}).get("order_id", ""),
                        "ticker": best.market_ticker,
                        "title": best.title,
                        "side": side,
                        "action": "buy",
                        "count": count,
                        "price_cents": price,
                        "total_cost_cents": price * count,
                        "status": "executed",
                        "notes": f"Auto-arb: {best.strategy}, spread={best.spread_cents}¢",
                    })

                logger.info(
                    f"Auto-executed arb: {best.market_ticker} "
                    f"YES@{yes_price}¢ + NO@{no_price}¢ x{count} "
                    f"= {best.profit_per_contract_cents * count}¢ profit"
                )

                # Notify via Telegram
                from app.services.telegram_service import TelegramService
                tg = TelegramService()
                await tg.send_message(
                    f"<b>⚡ Kalshi Arb Executed</b>\n"
                    f"{best.title[:50]}\n"
                    f"YES @{yes_price}¢ + NO @{no_price}¢ × {count}\n"
                    f"Expected profit: ${best.profit_per_contract_cents * count / 100:.2f}"
                )

            except Exception as e:
                logger.error(f"Auto-execute failed: {e}")

    def get_opportunities(self) -> list[dict]:
        """Return current opportunities."""
        return [o.to_dict() for o in self._opportunities]

    def get_status(self) -> dict:
        """Return scanner status."""
        return {
            "enabled": self.enabled,
            "running": self._running,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan,
            "total_found": self._total_found,
            "current_opportunities": len(self._opportunities),
            "auto_execute": self.auto_execute,
            "min_spread_cents": self.min_spread_cents,
            "scan_interval_seconds": self.scan_interval,
        }


# Singleton
_scanner: Optional[KalshiArbitrageScanner] = None


def get_arbitrage_scanner() -> KalshiArbitrageScanner:
    global _scanner
    if _scanner is None:
        _scanner = KalshiArbitrageScanner()
    return _scanner
