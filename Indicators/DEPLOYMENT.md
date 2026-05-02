# Indicator & Alert Deployment Status

**Last verified:** 2026-04-28 11:32 AM EDT (post Apr 20 FVG-dup cleanup)
**Source of truth:** TradingView (`alert_list` MCP / webpack 359399 `listAlerts()`). This doc is a snapshot — always re-pull live state before acting.

## How to update this file

After ANY change to alerts or indicator scripts (rebind, create, delete, redeploy, version bump), re-run this audit and overwrite the tables below. The audit script lives in conversation history; quick recipe:

1. `mcp__tradingview__alert_list` → save full JSON
2. `mcp__tradingview__pine_list_scripts` → confirm slot versions
3. Group alerts by `pine_id`, sort by symbol/TF, flag any `pine_version` ≠ current
4. Update the **Last verified** date and the affected rows

Update the **Changelog** at the bottom for any deployment event (script save, alert rebind/create/delete, indicator retired).

---

## Summary

| # alerts | indicator | script slot | script ver | source file |
|---|---|---|---|---|
| 4 | FVG v1.1 | `USER;3156f00306a244688b2d8de21cd03dbe` | 1.0 | `staged/indicator_fvg_v1.1.pine` |
| 7 | EMA Ribbon v1.0 | `USER;f060080f798d46efa6ee90ea4356190a` | 3.0 | `staged/indicator_ema_ribbon_v1.0.pine` |
| 2 | Liquidity Sweep v1.0 | `USER;12e465c59f0941d2a4fef70e58003c45` | 3.0 | `staged/indicator_liq_sweep_v1.0.pine` |
| 6 | Stochastic RSI v1.0 | `USER;fea633ae4e5a488c8ccea5efd448b93a` | 3.0 | `staged/indicator_stoch_rsi_v1.0.pine` |
| 5 | VWAP Deviation v1.0 | `USER;53163d00de3843f1a78c67bfc88dbf6d` | 10.0 | `staged/indicator_vwap_dev_v1.0.pine` |
| 0 | FVG v1.0 (retired) | `USER;4852215f50f54cbdad7d6ae82fb4ff07` | 5.0 | `staged/indicator_fvg_v1.0.pine` |
| 0 | Donchian Breakout v1.0 (not deployed) | `USER;6a0a490366d34845bed8071a79198cde` | 5.0 | `staged/indicator_donchian_v1.0.pine` |

**Totals:** 24 alerts (24 active, 0 inactive), 5 indicators in production, 2 staged-but-unused.

**WF alignment:** every active alert is on a strategy that passed walk-forward validation in ≥3 of the last 4 nightly runs (or just deployed today on a 3-4/4 stable validator).

---

## FVG v1.1 — Fair Value Gap (CLOSE-spam fixed)

**Logic:** edge-triggered FVG fill detection. v1.1 splits exit *state* from exit *signal* — eliminates ~270/day CLOSE webhook spam from v1.0.
**Slot:** `USER;3156f00306a244688b2d8de21cd03dbe` · script v1.0 · `staged/indicator_fvg_v1.1.pine`
**Deployed:** 2026-04-28

| status | symbol | TF | alert_id | last_fired |
|---|---|---|---|---|
| ✓ | BONK | 4H | 4454018061 | 2026-04-28 |
| ✓ | JUP | 4H | 4478601735 | 2026-04-28 |
| ✓ | PENGU | 4H | 4478628322 | 2026-04-28 |
| ✓ | RENDER | 4H | 4454018043 | 2026-04-28 |

---

## EMA Ribbon v1.0

**Logic:** 3/8/21/55 EMA ribbon expansion + RSI confirmation. Long-only since Apr 17 refactor; same-bar BUY→CLOSE bug fixed Apr 19 (v1.0 → v3.0).
**Slot:** `USER;f060080f798d46efa6ee90ea4356190a` · script v3.0 · `staged/indicator_ema_ribbon_v1.0.pine`

| status | symbol | TF | alert_id | last_fired |
|---|---|---|---|---|
| ✓ | BONK | 4H | 4454015047 | 2026-04-28 |
| ✓ | ETH | 4H | 4454015010 | 2026-04-28 |
| ✓ | PENGU | 1H | 4493207481 | 2026-04-28 |
| ✓ | RENDER | 1H | 4576191015 | never (deployed today) |
| ✓ | RENDER | 4H | 4454015019 | 2026-04-27 |
| ✓ | SOL | 4H | 4454013710 | 2026-04-27 |
| ✓ | WIF | 4H | 4454015089 | 2026-04-28 |

---

## Liquidity Sweep v1.0

**Logic:** wick-rejection detection at swing highs/lows; edge-triggered sweep + reclaim. Same-bar bugfix Apr 19 (v1.0 → v3.0).
**Slot:** `USER;12e465c59f0941d2a4fef70e58003c45` · script v3.0 · `staged/indicator_liq_sweep_v1.0.pine`

| status | symbol | TF | alert_id | last_fired |
|---|---|---|---|---|
| ✓ | ETH | 1H | 4454017961 | 2026-04-28 |
| ✓ | ETH | 4H | 4454017945 | 2026-04-28 |

⚠ **Coverage note:** Liq Sweep only validates on ETH in current backtests. All non-ETH alerts culled 2026-04-28.

---

## Stochastic RSI v1.0

**Logic:** K/D crossover in oversold zone + RSI<50 trend filter. Was the dominant source of fee-only churn pre-Apr 19 (22/25 BUY→CLOSE loops). v1.0 → v3.0 fix removed `short_exit`.
**Slot:** `USER;fea633ae4e5a488c8ccea5efd448b93a` · script v3.0 · `staged/indicator_stoch_rsi_v1.0.pine`

| status | symbol | TF | alert_id | last_fired |
|---|---|---|---|---|
| ✓ | BONK | 1H | 4576190853 | never (deployed today) |
| ✓ | ETH | 4H | 4454015121 | 2026-04-27 |
| ✓ | PENGU | 1H | 4558016704 | 2026-04-28 |
| ✓ | PENGU | 4H | 4479801456 | 2026-04-25 |
| ✓ | RENDER | 1H | 4454015587 | 2026-04-28 |
| ✓ | SOL | 4H | 4454015105 | 2026-04-28 |

---

## VWAP Deviation v1.0

**Logic:** anchored VWAP ± deviation bands; mean-reversion entry on band touch + momentum confirmation. v6.0/v10.0 saves were part of Apr 19 same-bar bugfix series.
**Slot:** `USER;53163d00de3843f1a78c67bfc88dbf6d` · script v10.0 · `staged/indicator_vwap_dev_v1.0.pine`

| status | symbol | TF | alert_id | last_fired |
|---|---|---|---|---|
| ✓ | BONK | 4H | 4524592285 | 2026-04-27 |
| ✓ | ETH | 4H | 4524592433 | 2026-04-27 |
| ✓ | PENGU | 4H | 4478619043 | 2026-04-25 |
| ✓ | SOL | 1H | 4576190178 | never (deployed today) |

---

## Retired / staged-but-unused

### FVG v1.0 (retired 2026-04-28)
- Slot `USER;4852215f50f54cbdad7d6ae82fb4ff07` (script v5.0)
- Replaced by FVG v1.1; all 9 alerts repointed via `modifyRestartAlert`
- Source kept at `staged/indicator_fvg_v1.0.pine` for reference

### Donchian Breakout v1.0 (never deployed)
- Slot `USER;6a0a490366d34845bed8071a79198cde` (script v5.0)
- Source: `staged/indicator_donchian_v1.0.pine`
- Failed walk-forward validation on every token/TF in nightly backtests (Apr 21 cull review)
- Source retained but no alerts active

---

## Token coverage matrix (active alerts)

|  | FVG | EMA Ribbon | Liq Sweep | Stoch RSI | VWAP Dev |
|---|---|---|---|---|---|
| **BONK** | 4H | 4H | — | 1H | 4H |
| **ETH** | — | 4H | 1H, 4H | 4H | 4H |
| **JUP** | 4H | — | — | — | — |
| **PENGU** | 4H | 1H | — | 1H, 4H | 4H |
| **RENDER** | 4H | 1H, 4H | — | 1H | — |
| **SOL** | — | 4H | — | 4H | 1H |
| **WIF** | — | 4H | — | — | — |

---

## Open issues / cleanup candidates

- Donchian source still staged but never validated; consider archiving
- Flickers held but not expanded: EMA Ribbon SOL 1H (1/4 nights), EMA Ribbon SOL 4H (1/4 nights kept under SOL coverage). Re-evaluate after one more week.

---

## Changelog

| Date | Event |
|---|---|
| 2026-04-28 | **Apr 20 FVG-dup cleanup**: deleted 3 disabled FVG alerts (`4513570647` BONK 4H, `4513571230` RENDER 4H, `4513571327` PENGU 4H). All Apr 20 batch with 8 inputs (missing `in_8`) — duplicates of the Apr 13/16 originals on the same tokens/TFs. Total 27 → 24 (100% active). |
| 2026-04-28 | **WF-alignment cull + deploy**: deleted 16 alerts that never validated in last 4 nightly walk-forward runs (5 Liq Sweep non-ETH, 4 FVG non-validated, 3 EMA Ribbon, 1 Stoch RSI JUP 4H, 3 VWAP Dev non-validated). Created 3 new alerts on stable 3-4/4 validators: VWAP Dev SOL 1H (`4576190178`), Stoch RSI BONK 1H (`4576190853`), EMA Ribbon RENDER 1H (`4576191015`). Total 40 → 27. WF alignment 49% → 100%. |
| 2026-04-28 | **Duplicate cleanup**: deleted 3 alerts via webpack 359399 `deleteAlerts`. Removed: Liq Sweep ETH 4H `4513574533` (dup of `4454017945`), Stoch RSI PENGU 4H `4524595484` (malformed `in_0` carried "secret: " prefix), VWAP Dev PENGU 4H `4524593076` (dup of `4478619043`, missing `in_8`). Total 43 → 40. |
| 2026-04-28 | **Stale pine_version rebind**: 19 alerts brought to current versions via `modifyRestartAlert` (10 EMA Ribbon v1.0→v3.0, 5 Stoch RSI v1.0→v3.0, 4 VWAP Dev v6.0→v10.0). Webhooks + fire history preserved. |
| 2026-04-28 | **FVG v1.1 deployed**: new slot, all 9 v1.0 alerts repointed; ETH/SOL alerts created; 3 Apr 20 dups disabled. CLOSE-spam fix splits exit state from exit signal. |
| 2026-04-21 | **Cull**: 49 alerts removed (legacy indicators, low-cap memes failing WF, all Donchian). Down to 47 active. |
| 2026-04-19 | **Same-bar BUY→CLOSE bugfix**: removed `short_exit` from all 6 indicators. Stoch RSI was responsible for 22/25 fee-only loops. Bulk-rebind 36 alerts. |
| 2026-04-17 | **Long-only refactor**: removed SELL signals from all 6 indicators (strategy-isolation: SELL on one indicator was liquidating positions opened by another). |
