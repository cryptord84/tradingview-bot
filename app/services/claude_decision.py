"""Claude AI Decision Layer - reviews every trade signal before execution.

Supports two modes:
  - "cli": Shells out to the `claude` CLI (Claude Code). No API key needed.
  - "api": Calls the Anthropic API directly. Requires api_key in config.

Failure handling distinguishes "Claude said no" from "Claude didn't answer":

  - said no        -> a parsed REJECT, or an unparseable response that might
                      have contained one. Always rejects.
  - misconfigured  -> ClaudeConfigError. Always rejects, loudly; an operator
                      has to fix it and the gate must not vanish quietly.
  - didn't answer  -> timeout / non-zero exit / API overload. Falls through to
                      the confidence bypass (claude.unavailable_bypass_confidence),
                      because an outage is not a risk assessment.
"""

import asyncio
import json
import logging
import shutil
from typing import Optional

from app.config import get
from app.models import ClaudeResponse, ClaudeDecision, WebhookSignal

logger = logging.getLogger("bot.claude")


class ClaudeConfigError(Exception):
    """Claude can never answer until an operator fixes something (CLI not
    installed, api_key missing in api mode).

    Kept distinct from transient failures on purpose: a transient outage is
    allowed to fall through to the confidence bypass below, but a config error
    is not. Bypassing a permanent misconfiguration would silently disable the
    risk gate for every future trade with nothing in the logs to say the gate
    was gone — the failure has to stay loud.
    """

SYSTEM_PROMPT = """You are a professional crypto trading risk manager and decision engine for a Solana (SOL) trading bot. Your job is to review every incoming trade signal and make a final decision: EXECUTE, REJECT, or MODIFY.

## Your Decision Framework

### Inputs You Receive:
1. **TradingView Signal**: Multi-indicator confluence signal with confidence score
2. **Wallet Balance**: Current SOL and USD balance
3. **Market Data**: Latest SOL price, 24h change, volume
4. **News & Geopolitical Context**: Recent headlines that could impact crypto markets
5. **On-Chain Metrics**: If available (TVL, volume, whale activity)
6. **Risk Parameters**: Max position size, leverage caps, daily loss limits

### Decision Rules:

**EXECUTE** when:
- Confidence score >= 65
- No major negative geopolitical events
- Wallet balance is healthy (above low-balance threshold)
- Daily loss limit not exceeded
- Signal aligns with broader market trend

**REJECT** when:
- Confidence score < 50
- Major negative geopolitical event detected (war escalation, severe crypto regulation, exchange collapse)
- Wallet balance near shutdown threshold
- Daily loss limit already hit
- Conflicting signals or extreme volatility without clear direction
- News indicates imminent black swan risk

**MODIFY** when:
- Signal is valid but risk is elevated
- Geopolitical tension is moderate -> reduce size by 30-80%
- Confidence is borderline (50-65) -> reduce size, lower leverage
- High volatility -> switch from market to limit order
- Approaching daily loss limit -> reduce size significantly

### Geopolitical Risk Assessment:
- **LOW**: Normal market conditions, no major events -> trade normally
- **MODERATE**: Political tension, regulatory uncertainty -> reduce size 30-50%
- **HIGH**: Active conflict escalation, major regulatory crackdown -> reduce size 50-80%
- **EXTREME**: Black swan event, exchange failure, systemic crisis -> REJECT all trades, recommend going flat

### Response Format (strict JSON, no markdown, no explanation outside the JSON):
{"decision": "EXECUTE|REJECT|MODIFY", "reasoning": "Clear 2-3 sentence explanation", "modified_size_percent": null, "modified_leverage": null, "modified_order_type": null, "limit_price": null, "risk_score": 5, "geo_risk_note": null}

Be concise. Be decisive. Protect capital above all else. When in doubt, reduce size rather than reject entirely."""


def _build_context(
    signal: WebhookSignal,
    wallet_balance_sol: float,
    wallet_balance_usd: float,
    sol_price: float,
    market_data: Optional[dict] = None,
    news_headlines: Optional[list[str]] = None,
    onchain_data: Optional[dict] = None,
    risk_params: Optional[dict] = None,
) -> str:
    """Build the context message sent to Claude."""
    context = f"""## Trade Signal from TradingView
- Signal Type: {signal.signal_type.value}
- Symbol: {signal.symbol}
- Entry Price Estimate: ${signal.entry_price_estimate:.4f}
- Confidence Score: {signal.confidence_score}/100
- Suggested Leverage: {signal.suggested_leverage}x
- Suggested Position Size: {signal.suggested_position_size_percent}%
- Bull Score: {signal.bull_score}/7 | Bear Score: {signal.bear_score}/7
- RSI: {signal.rsi} | ATR: {signal.atr}
- Timeframe: {signal.timeframe}

## Wallet Status
- SOL Balance: {wallet_balance_sol:.4f} SOL (${wallet_balance_sol * sol_price:.2f})
- Total Purchasing Power: ${wallet_balance_usd:.2f} (USDC wallet + Kamino deposits + SOL value)
- Current SOL Price: ${sol_price:.2f}
"""

    if market_data:
        context += f"""
## Market Data
- 24h Change: {market_data.get('price_change_24h', 'N/A')}%
- 24h Volume: ${market_data.get('volume_24h', 'N/A')}
- Market Cap Rank: {market_data.get('market_cap_rank', 'N/A')}
"""

    if news_headlines:
        context += "\n## Recent News Headlines\n"
        for i, headline in enumerate(news_headlines[:10], 1):
            context += f"{i}. {headline}\n"

    if onchain_data:
        context += f"""
## On-Chain Metrics
- Solana TVL: ${onchain_data.get('tvl', 'N/A')}
- 24h DEX Volume: ${onchain_data.get('dex_volume', 'N/A')}
- Active Addresses: {onchain_data.get('active_addresses', 'N/A')}
"""

    if risk_params:
        context += f"""
## Risk Parameters
- Max Purchase: ${risk_params.get('max_purchase_usd', 'N/A')} USD
- Max Leverage: {risk_params.get('max_leverage', 'N/A')}x
- Daily Loss Limit: {risk_params.get('daily_loss_limit_percent', 'N/A')}%
- Low Balance Shutdown: ${risk_params.get('low_balance_shutdown_usd', 'N/A')} USD
- Today's P&L: ${risk_params.get('today_pnl_usd', 0):.2f}
- Geo Risk Weight: {risk_params.get('geo_risk_weight', 0.7)}

## Balance Breakdown
- USDC in Wallet: ${risk_params.get('usdc_wallet', 0):.2f}
- USDC in Kamino: ${risk_params.get('usdc_kamino', 0):.2f}
- Tradeable USDC (wallet + Kamino): ${risk_params.get('tradeable_usd', 0):.2f}
- SOL value: ${wallet_balance_sol * sol_price:.2f}
- Total portfolio: ${risk_params.get('total_usd', 0):.2f}
"""

    context += "\n## Your Decision\nRespond with ONLY the JSON object. No markdown fences, no extra text."
    return context


def _parse_response(response_text: str) -> ClaudeResponse:
    """Parse Claude's response text into a ClaudeResponse."""
    text = response_text.strip()

    # Strip markdown code fences if present
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # Find the JSON object in the response
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    data = json.loads(text)
    return ClaudeResponse(**data)


async def _call_cli(context: str) -> str:
    """Call the Claude Code CLI in non-interactive print mode.

    Uses asyncio.create_subprocess_exec which passes arguments directly
    to the process (no shell interpolation), avoiding command injection.
    The prompt is passed as a positional argument to `claude --print`.
    """
    cfg = get("claude")
    cli_path = cfg.get("cli_path", "claude")
    timeout = cfg.get("timeout_seconds", 60)

    # Verify claude CLI is installed
    resolved = shutil.which(cli_path)
    if not resolved:
        raise ClaudeConfigError(
            f"Claude CLI not found at '{cli_path}'. "
            "Install Claude Code (npm install -g @anthropic-ai/claude-code) "
            "or set claude.cli_path in config.yaml."
        )

    # Build the full prompt including system instructions
    full_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{context}"

    # claude --print (-p) runs non-interactively: reads prompt, prints response, exits.
    # create_subprocess_exec passes args as a list (safe, no shell expansion).
    proc = await asyncio.create_subprocess_exec(
        resolved, "--print", full_prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Claude CLI timed out after {timeout}s")

    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "unknown error"
        raise RuntimeError(f"Claude CLI exited with code {proc.returncode}: {err}")

    return stdout.decode()


async def _call_api(context: str) -> str:
    """Call the Anthropic API directly."""
    import anthropic

    cfg = get("claude")
    api_key = cfg.get("api_key", "")
    if not api_key:
        raise ClaudeConfigError(
            "claude.api_key not set in config.yaml. "
            "Either set it or switch to mode: 'cli' to use Claude Code instead."
        )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=cfg.get("model", "sonnet"),
        max_tokens=cfg.get("max_tokens", 1024),
        temperature=cfg.get("temperature", 0.3),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    return message.content[0].text


async def get_claude_decision(
    signal: WebhookSignal,
    wallet_balance_sol: float,
    wallet_balance_usd: float,
    sol_price: float,
    market_data: Optional[dict] = None,
    news_headlines: Optional[list[str]] = None,
    onchain_data: Optional[dict] = None,
    risk_params: Optional[dict] = None,
) -> ClaudeResponse:
    """Send signal + context to Claude for decision. Uses CLI or API based on config."""

    cfg = get("claude")
    mode = cfg.get("mode", "cli")

    context = _build_context(
        signal, wallet_balance_sol, wallet_balance_usd, sol_price,
        market_data, news_headlines, onchain_data, risk_params,
    )

    try:
        if mode == "cli":
            logger.info("Requesting Claude decision via CLI (claude --print)")
            response_text = await _call_cli(context)
        else:
            logger.info("Requesting Claude decision via Anthropic API")
            response_text = await _call_api(context)

        logger.debug(f"Claude raw response: {response_text[:500]}")
        return _parse_response(response_text)

    except json.JSONDecodeError as e:
        # Claude answered, we just couldn't read it. The unreadable text may
        # well have been a REJECT, so this must not bypass — see the module
        # note on "said no" vs "didn't answer".
        logger.error(f"Failed to parse Claude response: {e}")
        return ClaudeResponse(
            decision=ClaudeDecision.REJECT,
            reasoning="Failed to parse Claude response. Rejecting for safety.",
            risk_score=8,
        )
    except ClaudeConfigError as e:
        logger.error(
            f"Claude misconfigured ({mode} mode) — risk gate cannot run, rejecting "
            f"{signal.signal_type.value if signal else '?'} "
            f"{signal.symbol if signal else '?'}: {e}"
        )
        return ClaudeResponse(
            decision=ClaudeDecision.REJECT,
            reasoning=(
                f"Claude {mode} misconfigured: {str(e)}. Rejecting until fixed — "
                f"a config error must not silently disable the risk gate."
            ),
            risk_score=10,
        )
    except Exception as e:
        # Transient failure: timeout, non-zero CLI exit, API overload. Claude
        # never rendered a decision, so treating this as a REJECT is not "risk
        # management" — it is an infrastructure outage masquerading as one.
        #
        # 2026-08-10: the old threshold was a hardcoded 80, but Pine emits
        # confidence 65 (111 signals) and 70 (4 signals) and has never once
        # produced an 80. The bypass could therefore never fire, so every CLI
        # hiccup became a silently-killed trade — 31 of them Jun–Jul, at which
        # point CLI errors outnumbered genuine rejects 31:15. Threshold is now
        # config-driven and defaults to 60 so real signals can actually clear it.
        conf = signal.confidence_score if signal else 0
        threshold = int(cfg.get("unavailable_bypass_confidence", 60))

        # EXITS ARE NEVER GATED BY AN OUTAGE. Pine stamps every CLOSE with
        # confidence 50 (all 192 on record) while BUYs carry 65-70, so a
        # threshold of 60 sits exactly between them: the 08-10 fix restored
        # entries during an outage but left exits rejected, which is backwards
        # — the bot could open positions it could not close on signal. Two real
        # exits (BTC, ETH) were lost to this on 08-11 before it was caught; 35
        # CLOSEs have been blocked by this gate all-time.
        #
        # An exit is risk-reducing by construction, so an unavailable risk
        # manager is never a reason to block one. Same rule the loss budget
        # uses. Note this only covers the UNAVAILABLE path — an explicit REJECT
        # that Claude actually rendered on a CLOSE is still honoured above.
        is_exit = signal and str(signal.signal_type.value).upper() in ("CLOSE", "SELL")
        if is_exit:
            logger.warning(
                f"Claude unavailable ({e}) — allowing exit {signal.signal_type.value} "
                f"{signal.symbol} regardless of confidence ({conf}): exits are never "
                f"blocked by an outage"
            )
            return ClaudeResponse(
                decision=ClaudeDecision.EXECUTE,
                reasoning=(
                    f"Claude unavailable ({str(e)}). Exit signal allowed through: "
                    f"an outage must not strand an open position."
                ),
                risk_score=3,
            )

        if conf >= threshold:
            logger.warning(
                f"Claude unavailable ({e}) — auto-executing {signal.signal_type.value} "
                f"{signal.symbol} (confidence={conf} >= {threshold}, bypassing Claude)"
            )
            return ClaudeResponse(
                decision=ClaudeDecision.EXECUTE,
                reasoning=(
                    f"Claude unavailable ({str(e)}). "
                    f"Auto-executing: confidence {conf}/100 meets ≥{threshold} bypass threshold."
                ),
                risk_score=5,
            )
        logger.warning(
            f"Claude unavailable ({e}) — skipping {signal.signal_type.value} "
            f"{signal.symbol} (confidence={conf} < {threshold}, no bypass)"
        )
        return ClaudeResponse(
            decision=ClaudeDecision.REJECT,
            reasoning=(
                f"Claude {mode} error: {str(e)}. "
                f"Confidence {conf}/100 below {threshold} bypass threshold — rejecting for safety."
            ),
            risk_score=10,
        )
