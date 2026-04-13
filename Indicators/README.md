# Indicators — Development & Deployment Workflow

## Folder Structure

```
Indicators/
  dev/        Work-in-progress: strategy scripts being written and backtested
  staged/     Passed all thresholds: indicator (alert) version ready to deploy
  deployed/   Currently live as TradingView alerts
  archived/   Tested but didn't pass thresholds — kept for reference
```

## Naming Convention

```
{type}_{timeframe}_{strategy}_{version}.pine
```

- **type**: `strategy` (backtest version) or `indicator` (alert/live version)
- **timeframe**: `1h`, `4h`, `1d`
- **strategy**: short slug — `supertrend`, `donchian`, `ema_ribbon`, `vwap_dev`, `stochrsi`, `hull_ma`
- **version**: `v1.0`, `v1.1`, etc.

Examples:
- `strategy_1h_supertrend_v1.0.pine`  ← backtesting script
- `indicator_1h_supertrend_v1.0.pine` ← alert version deployed to TradingView

## Protected Scripts (NEVER Overwrite)

These live TradingView scripts must never be touched:

| TV Script Name | TV Script ID | Local Backup |
|---|---|---|
| Indicator - BB Squeeze v1.0 — 4H Alerts | USER;bd1213dcce4843a684c44fae44c1cadd | `Backup/indicator_4h_bbsqueeze_v1.pine` |
| Indicator - BB Squeeze v2.0 — 4H Alerts | USER;0b206b17914b478d92af7892da49dff8 | `Backup/indicator_4h_bbsqueeze_v2.pine` |
| Indicator - Confluence Pro v3.5.3 — 1H Alerts | USER;43e3ac211be241e9958451d26a80512d | `Backup/indicator_1h_v3.5.1.pine` |
| Indicator - Mean Reversion v1.3.3 — 1H Alerts | USER;182f490a5e1c445b8c26eb9d65d8d0a6 | `Backup/indicator_4h_meanrev_v1.3.3_LIVE.pine` |
| Indicator - RSI Divergence v1.1 — 1H Alerts | USER;12775068023e47aeb862df68f5f005db | `Backup/indicator_4h_rsi_divergence_v1.1_LIVE.pine` |
| Indicator - Volume Momentum v1.1 — 4H Alerts | USER;1a737b3b352c49b288983648a5650dd6 | `Backup/indicator_1h_obfvg_v1.pine` |
| OB+FVG Alert v1.0 | USER;a23503083e694b919d2229ecf75ad735 | `Backup/indicator_1h_obfvg_v1.pine` |

## Backtesting Thresholds (must pass ALL to stage)

| Metric | Minimum |
|---|---|
| Profit Factor | > 1.40 |
| Win Rate | > 30% |
| Trade Count | ≥ 30 trades |
| Max Drawdown | < 35% |
| Net Profit | > 0% |

## Safe TradingView Pipeline (MCP)

### Backtesting (compile-only, NEVER save)
1. `pine_new(type="strategy")` → fresh scratch script
2. `pine_set_source(source=<code>)` → inject strategy code
3. `pine_smart_compile()` → compile to chart
4. `data_get_strategy_results()` / `data_get_trades()` / `data_get_equity()` → scrape results
5. `batch_run()` → repeat across symbols/timeframes

### Deploying (saving to TradingView)
1. Save indicator code to `Indicators/staged/` first (local copy = source of truth)
2. `pine_new(type="indicator")` → fresh scratch script
3. `pine_set_source(source=<code>)` → inject indicator code
4. `pine_smart_compile()` → compile
5. Save via pine-facade API (see below) — do NOT use pine_save or pine_open
6. Set up alert via `alert_create()`
7. Move file from `staged/` to `deployed/`

### Save via Pine Facade (safe save method)
```js
// Save as new script (returns 409 if name already exists — safe guard)
POST https://pine-facade.tradingview.com/pine-facade/save/new/?name={name}
Body: source={urlencoded_source}
Content-Type: application/x-www-form-urlencoded
```

## Tokens & Timeframes

| Token | Category | Primary TF | Secondary TF |
|---|---|---|---|
| SOL | L1 | 1H | 4H |
| ETH | L1 | 1H | 4H |
| JTO | Liquid Staking | 4H | 1H |
| WIF | Meme | 1H | 4H |
| BONK | Meme | 1H | 4H |
| PYTH | Oracle | 4H | 1H |
| RAY | DEX | 4H | 1H |
| ORCA | DEX | 4H | 1H |
| RENDER | AI/GPU | 1H | 4H |
| W | Bridge | 4H | 1H |
| DOG | Meme/BTC | 1H | 4H |

## Strategies Queued for Testing

| Strategy | Type | Hypothesis |
|---|---|---|
| Supertrend | Trend following | ATR bands catch breakouts on high-vol memes |
| Donchian Breakout | Momentum | 20-bar channel break with volume confirm |
| EMA Ribbon | Trend continuation | 3/8/21/55 stack alignment = sustained moves |
| VWAP Deviation | Mean reversion | Deviation bands complement existing mean-rev |
| Stoch RSI Cross | Momentum | K/D crossover + RSI zone filter |
| Hull MA Reversal | Trend / reversal | Smoother MA, fewer whipsaws than EMA |
