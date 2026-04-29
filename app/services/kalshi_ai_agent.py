"""Kalshi AI Agent Bot — Claude-powered prediction market analysis and trading.

Uses Claude (CLI or API) to analyze Kalshi markets by gathering context
(market data, orderbook, recent trades, news sentiment) and requesting
a structured trading decision with reasoning.

Agent specializations:
- Analyst: evaluates market fundamentals and probability estimation
- Contrarian: looks for overpriced/underpriced contracts based on sentiment
- Momentum: identifies trending markets with strong directional moves
- Consensus: aggregates all agent opinions into a final decision
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from typing import Optional

from app.config import get

logger = logging.getLogger("bot.kalshi.ai")

# ─── Agent Prompts ───────────────────────────────────────────────────────────

ANALYST_PROMPT = """You are an expert prediction market analyst. Analyze this Kalshi market and provide a trading recommendation.

Consider:
1. Is the current price (YES probability) fairly valued?
2. What is your estimated true probability of the event occurring?
3. Is there edge (difference between market price and true probability)?
4. What are the key risk factors?

Respond with ONLY a JSON object:
{
  "action": "BUY_YES" | "BUY_NO" | "HOLD",
  "confidence": 0.0-1.0,
  "estimated_probability": 0.0-1.0,
  "edge_cents": <integer, expected profit per contract>,
  "reasoning": "<2-3 sentence explanation>",
  "risk_level": "low" | "medium" | "high"
}"""

CONTRARIAN_PROMPT = """You are a contrarian prediction market trader. You profit by identifying when the crowd is wrong.

Analyze this market for:
1. Signs of herding or momentum-chasing (price moved too far too fast)
2. Overreaction to news events
3. Anchoring bias (price stuck near round numbers)
4. Recency bias (recent events overweighted)

Respond with ONLY a JSON object:
{
  "action": "BUY_YES" | "BUY_NO" | "HOLD",
  "confidence": 0.0-1.0,
  "crowd_sentiment": "too_bullish" | "too_bearish" | "fair",
  "edge_cents": <integer>,
  "reasoning": "<2-3 sentence explanation>",
  "contrarian_signal_strength": "strong" | "moderate" | "weak" | "none"
}"""

MOMENTUM_PROMPT = """You are a momentum trader on prediction markets. You identify markets with strong directional trends and ride them.

Analyze this market for:
1. Price trend direction and strength over recent candles
2. Volume confirmation of the trend
3. Orderbook imbalance (more buyers or sellers)
4. Whether the trend has room to continue or is exhausted

Respond with ONLY a JSON object:
{
  "action": "BUY_YES" | "BUY_NO" | "HOLD",
  "confidence": 0.0-1.0,
  "trend": "bullish" | "bearish" | "neutral",
  "trend_strength": 0.0-1.0,
  "edge_cents": <integer>,
  "reasoning": "<2-3 sentence explanation>"
}"""

RISK_AUDITOR_PROMPT = """You are a risk auditor for a prediction market trading system. Your ONLY job is to review proposed trades and approve or reject them based on risk.

You will receive:
- The market details (ticker, price, volume)
- Three other agents' votes and reasoning
- The proposed trade action and size

Apply these risk rules:
1. REJECT if all agents have low confidence (<0.4) even if they agree on direction
2. REJECT if the proposed entry price is in the 41-50 cent range (empirical dead zone, +0.16pp avg edge)
3. REJECT if market volume is very low (<5) — illiquid, hard to exit
4. REJECT if agent reasoning is contradictory or based on speculation without data
5. APPROVE if at least 2 agents agree with confidence >0.5 and price is outside 41-50 range

Be balanced. Allow trades with reasonable consensus. Only REJECT when there are clear risk flags.

Respond with ONLY a JSON object:
{
  "decision": "APPROVE" | "REJECT",
  "reasoning": "<1-2 sentence explanation>",
  "risk_flags": ["flag1", "flag2"],
  "risk_score": 1-10
}"""


# ─── Agent Decision ──────────────────────────────────────────────────────────

class AgentDecision:
    """A decision from one AI agent."""

    def __init__(self, agent_name: str, ticker: str, title: str, raw_response: dict):
        self.agent_name = agent_name
        self.ticker = ticker
        self.title = title
        self.action = raw_response.get("action", "HOLD")
        self.confidence = raw_response.get("confidence", 0)
        self.edge_cents = raw_response.get("edge_cents", 0)
        self.reasoning = raw_response.get("reasoning", "")
        self.raw = raw_response
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "agent": self.agent_name,
            "ticker": self.ticker,
            "title": self.title,
            "action": self.action,
            "confidence": self.confidence,
            "edge_cents": self.edge_cents,
            "reasoning": self.reasoning,
            "raw": self.raw,
            "timestamp": self.timestamp,
        }


class ConsensusDecision:
    """Aggregated decision from all agents."""

    def __init__(self, ticker: str, title: str, decisions: list[AgentDecision]):
        self.ticker = ticker
        self.title = title
        self.decisions = decisions
        self.timestamp = datetime.utcnow().isoformat()

        # Aggregate: majority vote weighted by confidence
        yes_score = 0
        no_score = 0
        hold_score = 0
        total_edge = 0

        for d in decisions:
            w = d.confidence
            if d.action == "BUY_YES":
                yes_score += w
            elif d.action == "BUY_NO":
                no_score += w
            else:
                hold_score += w
            total_edge += d.edge_cents * w

        total = yes_score + no_score + hold_score or 1

        if yes_score > no_score and yes_score > hold_score:
            self.action = "BUY_YES"
            self.consensus_strength = yes_score / total
        elif no_score > yes_score and no_score > hold_score:
            self.action = "BUY_NO"
            self.consensus_strength = no_score / total
        else:
            self.action = "HOLD"
            self.consensus_strength = hold_score / total

        self.avg_confidence = sum(d.confidence for d in decisions) / len(decisions) if decisions else 0
        self.avg_edge = total_edge / total if total > 0 else 0
        self.unanimous = len(set(d.action for d in decisions)) == 1

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "action": self.action,
            "consensus_strength": round(self.consensus_strength, 2),
            "avg_confidence": round(self.avg_confidence, 2),
            "avg_edge_cents": round(self.avg_edge, 1),
            "unanimous": self.unanimous,
            "agent_count": len(self.decisions),
            "agents": [d.to_dict() for d in self.decisions],
            "timestamp": self.timestamp,
        }


# ─── Main Bot ────────────────────────────────────────────────────────────────

class KalshiAIAgentBot:
    """Multi-agent AI trading bot for Kalshi prediction markets."""

    def __init__(self):
        cfg = get("kalshi") or {}
        ai_cfg = cfg.get("ai_agent", {})

        self.enabled = ai_cfg.get("enabled", False)
        self.scan_interval = ai_cfg.get("scan_interval_seconds", 600)
        self.agents = ai_cfg.get("agents", ["analyst", "contrarian", "momentum"])
        self.auto_trade = ai_cfg.get("auto_trade", False)
        self.min_consensus_strength = ai_cfg.get("min_consensus_strength", 0.7)
        self.require_unanimous = ai_cfg.get("require_unanimous", False)
        self.contracts_per_trade = ai_cfg.get("contracts_per_trade", 5)
        self.max_positions = ai_cfg.get("max_positions", 3)
        self.max_cost_per_trade_cents = ai_cfg.get("max_cost_per_trade_cents", 500)
        self.telegram_alerts = ai_cfg.get("telegram_alerts", True)
        self.target_tickers = ai_cfg.get("target_tickers", [])
        self.max_markets = ai_cfg.get("max_markets_to_analyze", 5)

        # Runtime
        self._consensus_history: list[ConsensusDecision] = []
        self._positions: list[dict] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._scan_count = 0
        self._last_scan: Optional[str] = None
        self._total_trades = 0

    # ── Lifecycle ──

    def _reload_positions_from_db(self):
        """Reload open positions from DB so dedup works across restarts."""
        from app.database import get_db
        try:
            conn = get_db()
            rows = conn.execute("""
                SELECT t.ticker, t.side, SUM(t.count) as total_count,
                       AVG(t.price_cents) as avg_price, MIN(t.timestamp) as first_buy,
                       t.title
                FROM kalshi_trades t
                WHERE t.status = 'executed' AND t.action = 'buy'
                AND NOT EXISTS (
                    SELECT 1 FROM kalshi_trades s
                    WHERE s.ticker = t.ticker AND s.side = t.side
                    AND s.action = 'sell' AND s.status = 'executed'
                )
                GROUP BY t.ticker, t.side
            """).fetchall()
            conn.close()

            self._positions.clear()
            for row in rows:
                ticker, side, count, avg_price, first_buy, title = row
                self._positions.append({
                    "ticker": ticker, "side": side,
                    "count": int(count), "entry_price": int(round(avg_price)),
                    "entry_time": first_buy, "title": title or ticker,
                })
            if self._positions:
                logger.info(
                    f"AI Agent reloaded {len(self._positions)} positions from DB"
                )
        except Exception as e:
            logger.warning(f"AI Agent failed to reload positions from DB: {e}")

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._reload_positions_from_db()
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(f"AI Agent bot started: agents={self.agents}, interval={self.scan_interval}s")
        return self._task

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("AI Agent bot stopped")

    async def _scan_loop(self):
        while self._running:
            try:
                await self.analyze_markets()
            except Exception as e:
                logger.error(f"AI Agent scan error: {e}")
            await asyncio.sleep(self.scan_interval)

    # ── Core Analysis ──

    async def analyze_markets(self) -> list[dict]:
        """Analyze target markets with all agents and generate consensus."""
        from app.services.kalshi_client import get_async_kalshi_client
        client = get_async_kalshi_client()
        if not client.enabled:
            return []

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        results = []

        markets = await self._select_markets(client)
        logger.info(f"AI scan #{self._scan_count}: analyzing {len(markets)} markets with {len(self.agents)} agents")

        # Subscribe to WS feed for liquidity-based sizing
        try:
            from app.services.kalshi_ws_feed import get_kalshi_ws_feed
            ws = get_kalshi_ws_feed()
            for m in markets:
                await ws.subscribe(m.get("ticker", ""))
        except Exception:
            pass

        for m in markets:
            ticker = m.get("ticker", "")
            title = m.get("title", ticker)

            try:
                context = await self._build_context(client, m)

                # Run all agents (sequentially to avoid overloading Claude CLI)
                decisions = []
                for agent_name in self.agents:
                    try:
                        decision = await self._run_agent(agent_name, ticker, title, context)
                        decisions.append(decision)
                    except Exception as e:
                        logger.warning(f"Agent '{agent_name}' failed on {ticker}: {e}")

                if not decisions:
                    continue

                consensus = ConsensusDecision(ticker, title, decisions)

                # Run Risk Auditor on non-HOLD consensus
                audit_result = None
                if consensus.action != "HOLD":
                    audit_result = await self._run_risk_auditor(
                        ticker, title, decisions, consensus, context
                    )
                    if audit_result.get("decision") == "REJECT":
                        logger.info(
                            f"Risk Auditor REJECTED {ticker}: {audit_result.get('reasoning')}"
                        )
                        consensus.action = "HOLD"
                        consensus._auditor_rejected = True

                self._consensus_history.insert(0, consensus)
                result_dict = consensus.to_dict()
                if audit_result:
                    result_dict["risk_auditor"] = audit_result
                results.append(result_dict)

                logger.info(
                    f"AI consensus on {ticker}: {consensus.action} "
                    f"(strength={consensus.consensus_strength:.0%}, "
                    f"unanimous={consensus.unanimous}"
                    f"{', auditor=REJECT' if audit_result and audit_result.get('decision') == 'REJECT' else ''})"
                )

            except Exception as e:
                logger.error(f"AI analysis failed for {ticker}: {e}")

        # Trim history
        if len(self._consensus_history) > 100:
            self._consensus_history = self._consensus_history[:100]

        # Alert and trade
        actionable = [r for r in results if r["action"] != "HOLD"]
        if actionable:
            if self.auto_trade:
                await self._execute_trades(actionable, client)

        return results

    # Finance/economics markets are nearly efficient — AI edge is minimal there
    # Focus on politics, sports, entertainment, world events where behavioral bias is high
    LOW_EDGE_KEYWORDS = ["finance", "fed ", "federal reserve", "interest rate",
                         "gdp", "cpi", "inflation", "treasury", "earnings"]
    LOW_EDGE_PREFIXES = ("KXFED", "KXCPI", "KXGDP", "KXINX", "KXINXD",
                         "KXNDX", "KXSPY", "KXDJI")
    # Maker-edge keywords ranked by Becker 2026-04-28 per-category gap.
    # Top tier (4-7pp): world events, media, entertainment, science/tech
    # Mid tier (2-3pp): crypto, weather, sports
    # Politics (1pp) intentionally NOT here — modest edge.
    HIGH_EDGE_KEYWORDS = ["world", "war", "conflict", "geopolit",
                          "media", "press", "news",
                          "entertainment", "celebrity", "movie", "tv ",
                          "award", "oscar", "grammy",
                          "science", "tech", "ai ", "space",
                          "weather", "hurricane", "tornado",
                          "sports", "super bowl", "world series",
                          "playoff", "championship",
                          "crypto", "bitcoin", "ethereum"]
    # Ticker-prefix matching for high-edge series (catches markets whose
    # titles wouldn't match the keyword list above — e.g., KXNOBELPEACE,
    # KXEPSTEIN). Aligned with Becker classifier.
    HIGH_EDGE_PREFIXES = (
        "KXNOBEL", "KXNEXTPOPE", "KXPOPE", "KXEPSTEIN", "KXOTEEPSTEIN",
        "KXZELENSKYYPUTINMEET", "KXBOLIVIAPRES", "KXSKPRES",
        "KXLAGODAYS", "KXARREST",
        "KXMENTION", "KXHEADLINE", "KXGOOGLESEARCH", "KX538APPROVE", "KXAPRPOTUS",
        "KXOSCAR", "KXGRAMMY", "KXEMMY", "KXBAFTA", "KXGAMEAWARDS",
        "KXSPOTIFY", "KXNETFLIX", "KXRT", "KXTOPSONG", "KXTOPALBUM", "KXTOPARTIST",
        "KXBILLBOARD",
        "KXLLM", "KXAI", "KXSPACEX", "KXALIENS", "KXAPPLE",
        "KXHIGH", "KXRAIN", "KXSNOW", "KXTORNADO", "KXHURCAT", "KXARCTICICE", "KXWEATHER",
        "KXBTCMAX", "KXBTCMIN", "KXETHMAX", "KXETHMIN", "KXBTCRESERVE",
    )

    async def _select_markets(self, client) -> list[dict]:
        """Select markets for AI analysis.

        Empirical filtering (Becker dataset 2026-04-28):
        - Skip finance (0.17pp gap, near-efficient)
        - Prefer high-bias categories: sports 2.23pp, world events 7.32pp,
          media 7.28pp, entertainment 4.79pp, science/tech 4.28pp
        - Skip true dead zone 41-50¢ (+0.16pp). 51-60¢ is +2.40pp — kept in.
        - Drop "always prefer 1-20¢ tails": 1-15¢ is weak (+0.06–0.48pp).
        """
        if self.target_tickers:
            markets = []
            for ticker in self.target_tickers:
                try:
                    markets.append(await client.get_market(ticker))
                except Exception:
                    pass
            return markets

        ai_cfg = (get("kalshi") or {}).get("ai_agent", {})
        max_days = ai_cfg.get("max_days_to_close", 0)
        all_markets = await client.discover_active_markets(min_volume=10, max_days_to_close=max_days)
        candidates = []
        for m in all_markets:
            vol = int(float(m.get("volume_fp", "0") or "0"))
            yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            if vol < 10 or yes_ask < 5 or yes_ask > 95:
                continue
            # Skip true dead zone (Becker: 41-50¢ avg +0.16pp).
            # Asymmetric — 51-60¢ is +2.40pp (best band) and stays IN.
            if 41 <= yes_ask <= 50:
                continue
            # Skip low-edge finance/economics markets (ticker prefix + title keyword)
            ticker = m.get("ticker", "")
            if ticker.startswith(self.LOW_EDGE_PREFIXES):
                continue
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            if any(kw in title for kw in self.LOW_EDGE_KEYWORDS):
                continue
            # Score: volume + category edge bonus (ticker-prefix match first,
            # title keyword fallback — catches series like KXNOBELPEACE/KXEPSTEIN
            # whose titles don't include "world"/"war"/etc.)
            score = vol
            if ticker.startswith(self.HIGH_EDGE_PREFIXES) or \
               any(kw in title for kw in self.HIGH_EDGE_KEYWORDS):
                score *= 1.5  # Boost high-bias categories
            m["_volume"] = vol
            m["_yes_ask"] = yes_ask
            m["_score"] = score
            candidates.append(m)
        candidates.sort(key=lambda m: m.get("_score", 0), reverse=True)
        return candidates[:self.max_markets]

    async def _build_context(self, client, market: dict) -> str:
        """Build a rich context string for Claude to analyze."""
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        # Get full market data via direct API
        try:
            full_market = await client.get_market_full(ticker)
        except Exception:
            full_market = market

        try:
            book = await client.get_orderbook(ticker)
        except Exception:
            book = {}

        try:
            trades = await client.get_market_trades(ticker, limit=10)
        except Exception:
            trades = []

        try:
            candles = await client.get_candlesticks(ticker, period_interval=60, limit=24)
        except Exception:
            candles = []

        yes_ask = int(round(float(full_market.get("yes_ask_dollars", "0") or "0") * 100))
        no_ask = int(round(float(full_market.get("no_ask_dollars", "0") or "0") * 100))
        yes_bid = int(round(float(full_market.get("yes_bid_dollars", "0") or "0") * 100))
        no_bid = int(round(float(full_market.get("no_bid_dollars", "0") or "0") * 100))
        volume = int(float(full_market.get("volume_fp", "0") or "0"))

        lines = [
            f"MARKET: {title}",
            f"Ticker: {ticker}",
            f"Current YES price: {yes_ask}c",
            f"Current NO price: {no_ask}c",
            f"YES bid/ask: {yes_bid}/{yes_ask}",
            f"NO bid/ask: {no_bid}/{no_ask}",
            f"Volume: {volume}",
            f"Close date: {full_market.get('close_time', 'unknown')}",
        ]

        if book:
            lines.append(f"\nORDERBOOK: {json.dumps(book)[:300]}")

        if trades:
            lines.append(f"\nRECENT TRADES ({len(trades)}):")
            for t in trades[:5]:
                price = t.get("yes_price") or t.get("no_price", "?")
                count = t.get("count", "?")
                lines.append(f"  {count}x @{price}c")

        if candles:
            prices = [c.get("close", 0) for c in candles if c.get("close")]
            if prices:
                lines.append(f"\n24H PRICE RANGE: {min(prices)}-{max(prices)}c")
                if len(prices) >= 2:
                    change = prices[-1] - prices[0]
                    lines.append(f"24H CHANGE: {change:+}c")

        # Whale flow data — large trades as smart money signal
        try:
            from app.services.kalshi_whale_tracker import get_whale_tracker
            whale = get_whale_tracker().get_whale_sentiment(ticker)
            if whale["whale_count"] > 0:
                lines.append(f"\nWHALE FLOW (last 60min):")
                lines.append(f"  Whale trades: {whale['whale_count']}")
                lines.append(f"  YES volume: {whale['yes_volume']} contracts")
                lines.append(f"  NO volume: {whale['no_volume']} contracts")
                lines.append(f"  Net sentiment: {whale['net_sentiment']:+.2f} ({whale['signal']})")
        except Exception:
            pass

        return "\n".join(lines)

    async def _run_agent(self, agent_name: str, ticker: str, title: str, context: str) -> AgentDecision:
        """Run a single AI agent on a market."""
        prompts = {
            "analyst": ANALYST_PROMPT,
            "contrarian": CONTRARIAN_PROMPT,
            "momentum": MOMENTUM_PROMPT,
        }

        system_prompt = prompts.get(agent_name, ANALYST_PROMPT)
        full_prompt = f"{system_prompt}\n\n---\n\nMARKET DATA:\n{context}"

        response_text = await self._call_claude(full_prompt)
        parsed = self._parse_response(response_text)
        return AgentDecision(agent_name, ticker, title, parsed)

    async def _run_risk_auditor(self, ticker: str, title: str,
                                decisions: list[AgentDecision],
                                consensus: 'ConsensusDecision',
                                context: str) -> dict:
        """Run the Risk Auditor agent to approve/reject a proposed trade.

        Returns {"decision": "APPROVE"|"REJECT", "reasoning": ..., ...}
        """
        # Build summary of other agents' votes
        agent_summary = []
        for d in decisions:
            agent_summary.append(
                f"  {d.agent_name}: {d.action} (confidence={d.confidence:.2f}, "
                f"edge={d.edge_cents}c) — {d.reasoning}"
            )

        audit_context = (
            f"MARKET: {title}\nTicker: {ticker}\n\n"
            f"AGENT VOTES:\n" + "\n".join(agent_summary) + "\n\n"
            f"PROPOSED TRADE: {consensus.action}\n"
            f"Consensus strength: {consensus.consensus_strength:.0%}\n"
            f"Unanimous: {consensus.unanimous}\n"
            f"Average edge: {consensus.avg_edge:.1f}c\n\n"
            f"MARKET DATA:\n{context}"
        )

        full_prompt = f"{RISK_AUDITOR_PROMPT}\n\n---\n\n{audit_context}"

        try:
            response_text = await self._call_claude(full_prompt)
            parsed = self._parse_response(response_text)
            logger.info(
                f"Risk Auditor on {ticker}: {parsed.get('decision', 'UNKNOWN')} "
                f"(risk_score={parsed.get('risk_score', '?')}) — {parsed.get('reasoning', '')}"
            )
            return parsed
        except Exception as e:
            logger.warning(f"Risk Auditor failed on {ticker}: {e} — defaulting to REJECT")
            return {"decision": "REJECT", "reasoning": f"Auditor error: {e}", "risk_flags": ["error"], "risk_score": 10}

    async def _call_claude(self, prompt: str) -> str:
        """Call Claude via CLI or API.

        Uses asyncio.create_subprocess_exec which passes arguments directly
        to the process (no shell interpolation), preventing command injection.
        """
        cfg = get("claude")
        mode = cfg.get("mode", "cli")

        if mode == "cli":
            cli_path = cfg.get("cli_path", "claude")
            timeout = cfg.get("timeout_seconds", 60)

            resolved = shutil.which(cli_path)
            if not resolved:
                raise FileNotFoundError(f"Claude CLI not found at '{cli_path}'")

            # create_subprocess_exec passes args as a list (safe, no shell)
            proc = await asyncio.create_subprocess_exec(
                resolved, "--print", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise TimeoutError(f"Claude CLI timed out after {timeout}s")

            if proc.returncode != 0:
                raise RuntimeError(f"Claude CLI error: {stderr.decode()[:200]}")

            return stdout.decode()
        else:
            import anthropic
            api_key = cfg.get("api_key", "")
            if not api_key:
                raise ValueError("Claude API key not configured")

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=cfg.get("model", "claude-sonnet-4-6"),
                max_tokens=cfg.get("max_tokens", 1024),
                temperature=cfg.get("temperature", 0.3),
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

    def _parse_response(self, text: str) -> dict:
        """Extract JSON from Claude's response."""
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        raise ValueError(f"No JSON found in response: {text[:200]}")

    # ── Execution ──

    async def _execute_trades(self, results: list[dict], client):
        """Execute trades on strong consensus decisions."""
        from app.database import insert_kalshi_trade

        for r in results:
            if r["action"] == "HOLD":
                continue
            if r["consensus_strength"] < self.min_consensus_strength:
                continue
            if self.require_unanimous and not r["unanimous"]:
                continue
            if len(self._positions) >= self.max_positions:
                break

            ticker = r["ticker"]
            side = "yes" if r["action"] == "BUY_YES" else "no"

            # DB deduplication: skip if we already have contracts on this ticker+side
            try:
                from app.database import get_db
                conn = get_db()
                buy_count = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM kalshi_trades "
                    "WHERE ticker=? AND side=? AND action='buy' AND status='executed'",
                    (ticker, side),
                ).fetchone()[0]
                sell_count = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM kalshi_trades "
                    "WHERE ticker=? AND side=? AND action='sell' AND status='executed'",
                    (ticker, side),
                ).fetchone()[0]
                conn.close()
                if buy_count - sell_count > 0:
                    logger.debug(f"AI Agent skipping {ticker} {side}: already {buy_count - sell_count} open contracts")
                    continue
            except Exception as e:
                logger.warning(f"AI Agent DB dedup check failed: {e}")

            try:
                market = await client.get_market_full(ticker)
                price = int(round(float(market.get(f"{side}_ask_dollars", "0") or "0") * 100)) or 50
                count = self.contracts_per_trade
                cost = price * count

                if cost > self.max_cost_per_trade_cents:
                    continue

                # Rule-based risk audit (fast, before trade)
                try:
                    from app.services.kalshi_risk_manager import get_risk_manager
                    rm = get_risk_manager()
                    if rm.enabled:
                        audit = rm.audit_trade(
                            ticker=ticker, side=side, price_cents=price,
                            count=count, confidence=r.get("consensus_strength", 0.5),
                            bot_name="ai", title=r.get("title", ""),
                        )
                        if not audit["approved"]:
                            logger.info(f"AI trade BLOCKED by auditor: {audit['reason']}")
                            continue
                        if audit.get("adjustments", {}).get("count"):
                            count = audit["adjustments"]["count"]
                except Exception as e:
                    logger.warning(f"Risk audit failed (allowing trade): {e}")

                if side == "yes":
                    result = await client.buy_yes(ticker, price, count)
                else:
                    result = await client.buy_no(ticker, price, count)

                order = result.get("order", {})
                self._total_trades += 1

                insert_kalshi_trade({
                    "order_id": order.get("order_id", ""),
                    "ticker": ticker,
                    "title": r.get("title", ""),
                    "side": side,
                    "action": "buy",
                    "count": count,
                    "price_cents": price,
                    "total_cost_cents": price * count,
                    "status": order.get("status", "placed"),
                    "notes": (
                        f"AI Agent: {r['action']} consensus={r['consensus_strength']:.0%} "
                        f"unanimous={r['unanimous']} agents={r['agent_count']}"
                    ),
                })

                self._positions.append({
                    "ticker": ticker, "side": side,
                    "count": count, "entry_price": price,
                    "entry_time": datetime.utcnow().isoformat(),
                })

                logger.info(
                    f"AI Agent TRADE: {side.upper()} {count}x "
                    f"@{price}c on {ticker} (consensus={r['consensus_strength']:.0%})"
                )

            except Exception as e:
                logger.error(f"AI Agent trade failed on {ticker}: {e}")

    # ── Alerts ──

    async def _send_alerts(self, results: list[dict]):
        from app.services.telegram_service import TelegramService
        tg = TelegramService()

        lines = ["<b>Kalshi AI Agent Analysis</b>\n"]
        for r in results[:3]:
            icon = "BUY YES" if r["action"] == "BUY_YES" else "BUY NO" if r["action"] == "BUY_NO" else "HOLD"
            unanimous_tag = " UNANIMOUS" if r["unanimous"] else ""
            lines.append(
                f"<b>{icon}</b> -- {r['title'][:40]}\n"
                f"   Consensus: {r['consensus_strength']:.0%}{unanimous_tag}\n"
                f"   Edge: {r['avg_edge_cents']:.0f}c | Agents: {r['agent_count']}\n"
            )

            for a in r.get("agents", [])[:3]:
                lines.append(f"   {a['agent']}: {a['action']} ({a['confidence']:.0%}) -- {a['reasoning'][:60]}\n")

        if self.auto_trade:
            lines.append(f"\n<i>Auto-trade: ON | Min consensus: {self.min_consensus_strength:.0%}</i>")

        await tg.send_message("\n".join(lines))

    # ── Status ──

    def get_decisions(self, limit: int = 20) -> list[dict]:
        return [c.to_dict() for c in self._consensus_history[:limit]]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "auto_trade": self.auto_trade,
            "agents": self.agents,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan,
            "total_decisions": len(self._consensus_history),
            "total_trades": self._total_trades,
            "active_positions": len(self._positions),
            "max_positions": self.max_positions,
            "min_consensus": self.min_consensus_strength,
            "require_unanimous": self.require_unanimous,
        }


_ai_bot: Optional[KalshiAIAgentBot] = None


def get_ai_agent_bot() -> KalshiAIAgentBot:
    global _ai_bot
    if _ai_bot is None:
        _ai_bot = KalshiAIAgentBot()
    return _ai_bot
