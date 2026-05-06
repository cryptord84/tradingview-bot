# CLAUDE.md

Project-specific guidance for Claude sessions in this repo. For setup/install, see `SETUP.md`.

## What this is
Multi-token crypto trading bot: TradingView Pine alerts → FastAPI webhook → Claude decision layer → Jupiter DEX (Solana) and Kalshi (prediction markets). Live bot entry: `main.py` (uvicorn). Backtesting engine: `backtesting/`.

## Run commands
```bash
# Live bot (foreground, active terminal)
source venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Single backtest
venv/bin/python backtesting/run.py

# Nightly matrix (inserts to dashboard DB)
venv/bin/python backtesting/nightly.py [--bars 3000] [--dry-run] [--htf]

# Deploy shortlist (candidates for alert rollout)
venv/bin/python backtesting/deploy_shortlist.py
```
Dashboard: http://localhost:8000  · API docs: `/docs`  · Webhook: `POST /webhook`

## Directory map
- `main.py` — FastAPI app entrypoint
- `app/routers/` — `webhook.py` (TV signals in), `dashboard.py` (UI API)
- `app/services/` — `trade_engine.py`, `claude_decision.py`, `jupiter_client.py`, `kalshi_*.py`, `telegram_*.py`, `price_feed.py`, `position_monitor.py`
- `backtesting/` — `engine.py` (walk-forward), `strategies.py`, `indicators.py`, `nightly.py`
- `backtesting/results/` — CSV outputs, nightly summaries
- `Indicators/staged/` — canonical Pine source for deployed indicators (FVG, Liq Sweep, VWAP Dev, EMA Ribbon, Donchian, Stoch RSI)
- `config.yaml` — all runtime config (gitignored; `.example` is committed)
- `logs/` — rotated bot logs
- `data/trades.db`, `app/trades.db` — SQLite state

## Hard rules (do NOT violate)

1. **Never restart the bot in the background.** Always run uvicorn in an active terminal the user can watch. Backgrounded restarts have hidden failures the user can't see.

2. **Never `pine_save` or `pine_smart_compile` without first verifying the editor's active slot.** TV Pine editor has a known bug where `pine_open` by name loads source into Monaco but does not always update the binding — saves then overwrite the *wrong* script slot. Flow: open via nameButton → "Open script…" dialog → verify `nameButton h2` matches target → only then `pine_set_source` + compile. See `.claude/memory/feedback_pine_slot_overwrite.md`.

3. **Never create a new Pine script with `pine_set_source` on a blank/untitled editor.** Always `pine_new` first (creates a named slot) OR open an existing target slot. Skipping this overwrites the currently-focused script.

4. **To refresh alert `pine_version`, use the internal REST API, not UI automation.** Webpack module `359399` → `getAlertsRestApi().modifyRestartAlert(payload)` preserves `alert_id`, webhook URL, message, and timeline. Strip server-generated fields before sending. See `.claude/memory/reference_tv_alerts_rest_api.md`.

5. **Secrets never go in source.** `config.yaml`, `.env`, and `keys/` are gitignored. Webhook secret, Anthropic key, wallet password, Solana private key — all via `config.yaml` + `.env`. Don't echo them in logs or commit messages.

6. **Don't delete alerts in bulk without confirmation.** Live alerts drive live trades. Use `modifyRestartAlert` to update in place. Deletion requires explicit user approval even if the alert looks stale.

7. **Don't `pine_save` during a backtest.** Use the compile-only pipeline (`backtesting/run.py`). Saving mid-backtest can corrupt live alerts bound to that slot.

8. **Monitor price source must match swap execution source.** TP/SL decisions in `position_monitor.py` use `JupiterClient.get_token_price()` — which MUST resolve via the same DEX route the actual swap will use (Jupiter aggregator). Using a different oracle (Binance.US REST, CoinGecko polled prices, etc.) risks fake-fired triggers on prices the bot can't actually achieve. Lesson 2026-05-06: Binance.US zombie listing returned `JUPUSDT $0.10` while Solana spot was $0.19 → fake-SL'd 4 positions for $4.59 phantom loss. The real swap proceeds were correct; only the monitor's trigger price was wrong.

9. **Position records must always have a TP/SL — never let a BUY swap execute without one.** `trade_engine.py:1047` falls back to a percentage-of-price ATR (`position_monitor.fallback_atr_pct`, default 2.5%) when the Pine alert payload lacks an `atr` field. The bot logs a warning so you can investigate which indicator regressed. **Don't change this to fail-closed (reject the BUY) without thinking through it** — the tokens were already swapped for USDC; rejecting after the fact strands them as orphans (the original 2026-04-19 → 2026-05-06 bug class). See `.claude/memory/feedback_no_atr_fail_closed.md`.

## Finding live state
- **Indicator/alert deployment** → `Indicators/DEPLOYMENT.md` (which scripts, which slots, which alerts on which tokens). Update this file after any rebind / create / delete / version bump — it is the project's source of truth for "what is deployed right now."
- **Strategy metrics, token lists, alert rollout status** → live in `.claude/memory/` (project memories), not here. Those change frequently; this file does not.
- **Recent nightly results** → `backtesting/results/nightly_*.txt`
- **Open positions / recent fills** → dashboard at `/` or `app/trades.db`

## Conventions
- Pine files in `Indicators/staged/` are the source of truth; the TV slot is a deployment target, not a master copy.
- Alert names follow: `<Indicator> v<ver> — Alerts: Any alert() function call` (created by TV UI on "Any alert() function call" condition).
- Webhook payloads are JSON emitted by Pine `alert()` calls; schema lives in each indicator's `alert()` block.
