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

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._running = True
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
        from app.services.kalshi_client import get_kalshi_client
        client = get_kalshi_client()
        if not client.enabled:
            return []

        self._scan_count += 1
        self._last_scan = datetime.utcnow().isoformat()
        results = []

        markets = await self._select_markets(client)
        logger.info(f"AI scan #{self._scan_count}: analyzing {len(markets)} markets with {len(self.agents)} agents")

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
                self._consensus_history.insert(0, consensus)
                results.append(consensus.to_dict())

                logger.info(
                    f"AI consensus on {ticker}: {consensus.action} "
                    f"(strength={consensus.consensus_strength:.0%}, "
                    f"unanimous={consensus.unanimous})"
                )

            except Exception as e:
                logger.error(f"AI analysis failed for {ticker}: {e}")

        # Trim history
        if len(self._consensus_history) > 100:
            self._consensus_history = self._consensus_history[:100]

        # Alert and trade
        actionable = [r for r in results if r["action"] != "HOLD"]
        if actionable:
            if self.telegram_alerts:
                await self._send_alerts(actionable)
            if self.auto_trade:
                await self._execute_trades(actionable, client)

        return results

    async def _select_markets(self, client) -> list[dict]:
        """Select markets for AI analysis."""
        if self.target_tickers:
            markets = []
            for ticker in self.target_tickers:
                try:
                    markets.append(client.get_market(ticker))
                except Exception:
                    pass
            return markets

        all_markets = client.get_markets(status="open", limit=50)
        candidates = [
            m for m in all_markets
            if (m.get("volume", 0) or 0) >= 200
            and 20 <= (m.get("yes_ask", 0) or 0) <= 80
        ]
        candidates.sort(key=lambda m: m.get("volume", 0) or 0, reverse=True)
        return candidates[:self.max_markets]

    async def _build_context(self, client, market: dict) -> str:
        """Build a rich context string for Claude to analyze."""
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        try:
            book = client.get_orderbook(ticker)
        except Exception:
            book = {}

        try:
            trades = client.get_market_trades(ticker, limit=10)
        except Exception:
            trades = []

        try:
            candles = client.get_candlesticks(ticker, period_interval=60, limit=24)
        except Exception:
            candles = []

        lines = [
            f"MARKET: {title}",
            f"Ticker: {ticker}",
            f"Current YES price: {market.get('yes_ask', '?')}c",
            f"Current NO price: {market.get('no_ask', '?')}c",
            f"YES bid/ask: {market.get('yes_bid', '?')}/{market.get('yes_ask', '?')}",
            f"NO bid/ask: {market.get('no_bid', '?')}/{market.get('no_ask', '?')}",
            f"Volume: {market.get('volume', '?')}",
            f"Close date: {market.get('close_time', 'unknown')}",
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

            try:
                market = client.get_market(ticker)
                price = market.get(f"{side}_ask", 50)
                cost = price * self.contracts_per_trade

                if cost > self.max_cost_per_trade_cents:
                    continue

                if side == "yes":
                    result = client.buy_yes(ticker, price, self.contracts_per_trade)
                else:
                    result = client.buy_no(ticker, price, self.contracts_per_trade)

                order = result.get("order", {})
                self._total_trades += 1

                insert_kalshi_trade({
                    "order_id": order.get("order_id", ""),
                    "ticker": ticker,
                    "title": r.get("title", ""),
                    "side": side,
                    "action": "buy",
                    "count": self.contracts_per_trade,
                    "price_cents": price,
                    "total_cost_cents": cost,
                    "status": order.get("status", "placed"),
                    "notes": (
                        f"AI Agent: {r['action']} consensus={r['consensus_strength']:.0%} "
                        f"unanimous={r['unanimous']} agents={r['agent_count']}"
                    ),
                })

                self._positions.append({
                    "ticker": ticker, "side": side,
                    "count": self.contracts_per_trade, "entry_price": price,
                    "entry_time": datetime.utcnow().isoformat(),
                })

                logger.info(
                    f"AI Agent TRADE: {side.upper()} {self.contracts_per_trade}x "
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
