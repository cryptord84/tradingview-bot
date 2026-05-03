# Indicator & Alert Deployment Status

**Last verified:** 2026-05-02 ~6:00 PM EDT (post EVM Phase 3 + INJ alert deploy on Arbitrum)
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
| 4 | EMA Ribbon v1.0 | `USER;f060080f798d46efa6ee90ea4356190a` | 3.0 | `staged/indicator_ema_ribbon_v1.0.pine` |
| 2 | Liquidity Sweep v1.0 | `USER;12e465c59f0941d2a4fef70e58003c45` | 3.0 | `staged/indicator_liq_sweep_v1.0.pine` |
| 4 | Stochastic RSI v1.0 | `USER;fea633ae4e5a488c8ccea5efd448b93a` | 3.0 | `staged/indicator_stoch_rsi_v1.0.pine` |
| 7 | VWAP Deviation v1.0 | `USER;53163d00de3843f1a78c67bfc88dbf6d` | 10.0 | `staged/indicator_vwap_dev_v1.0.pine` |
| 0 | FVG v1.0 (retired) | `USER;4852215f50f54cbdad7d6ae82fb4ff07` | 5.0 | `staged/indicator_fvg_v1.0.pine` |
| 0 | Donchian Breakout v1.0 (not deployed) | `USER;6a0a490366d34845bed8071a79198cde` | 5.0 | `staged/indicator_donchian_v1.0.pine` |

**Totals:** 15 alerts (15 active, 0 inactive), 5 indicators in production, 2 staged-but-unused.

**EVM execution lane (Phase 3 deployed 2026-05-02):** the Liq Sweep / INJ.P / 4H alert routes through the EVM trade engine (OpenOcean on Arbitrum) instead of Jupiter. EVM wallet `0x74F29429...` funded with $100 USDC + ~$15 ETH for gas.

**WF alignment:** Mixed. The **4 WF-passers from `nightly_20260502_0403`** are now deployed: Stoch RSI/FARTCOIN.P/4H (`4606125639`), VWAP Dev/FARTCOIN.P/4H (`4606125661`), VWAP Dev/MOODENG.P/4H (`4606125675`), VWAP Dev/JUP/4H (`4606092343`). The remaining **14 alerts have PF in the 0.5–1.4 range** (heavy-loss to marginal under fixed WF gate); kept after conservative cull pending re-evaluation as more nightly data accumulates. Catastrophic combos (PF<0.5) and triple-flagged stale combos already culled 2026-05-02.

**Note on FARTCOIN/MOODENG perp symbols:** These tokens have no Binance Spot listing — alerts use `BINANCE:<TOKEN>USDT.P` (perpetual). The trade engine's symbol normalization was patched 2026-05-02 to strip the `.P` suffix so webhook payloads route correctly.

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
| ✓ | BONK | 4H | 4454015047 | 2026-05-02 |
| ✓ | PENGU | 1H | 4493207481 | 2026-05-02 |
| ✓ | RENDER | 1H | 4576191015 | 2026-05-02 |
| ✓ | WIF | 4H | 4454015089 | 2026-05-02 |
| — | _culled 2026-05-02:_ ETH 4H, RENDER 4H, SOL 1H, SOL 4H, PENGU 1H | | | |

---

## Liquidity Sweep v1.0

**Logic:** wick-rejection detection at swing highs/lows; edge-triggered sweep + reclaim. Same-bar bugfix Apr 19 (v1.0 → v3.0).
**Slot:** `USER;12e465c59f0941d2a4fef70e58003c45` · script v3.0 · `staged/indicator_liq_sweep_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | INJ.P | 4H | 4606986738 | **NEW (2026-05-02) — WF passer (PF 1.55, Tier C 6%) — EVM lane via OpenOcean on Arbitrum** |
| — | _culled 2026-05-02:_ ETH 1H (`4454017961`) PF 0.40, ETH 4H (`4454017945`) PF 0.93 |

---

## Stochastic RSI v1.0

**Logic:** K/D crossover in oversold zone + RSI<50 trend filter. Was the dominant source of fee-only churn pre-Apr 19 (22/25 BUY→CLOSE loops). v1.0 → v3.0 fix removed `short_exit`.
**Slot:** `USER;fea633ae4e5a488c8ccea5efd448b93a` · script v3.0 · `staged/indicator_stoch_rsi_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | ETH | 4H | 4454015121 | retained (PF 0.65, marginal — pending re-eval) |
| ✓ | PENGU | 4H | 4479801456 | retained (PF 1.25, marginal — pending re-eval) |
| ✓ | RENDER | 1H | 4454015587 | retained (PF 0.86, marginal — pending re-eval) |
| ✓ | SOL | 4H | 4454015105 | retained (PF 0.85, marginal — pending re-eval) |
| ✓ | FARTCOIN.P | 4H | 4606125639 | **NEW — WF passer (PF 4.49, Tier A 12%)** |
| — | _culled 2026-05-02:_ BONK 1H (`4576190853`), PENGU 1H (`4558016704`) | | | catastrophic 1H |

---

## VWAP Deviation v1.0

**Logic:** anchored VWAP ± deviation bands; mean-reversion entry on band touch + momentum confirmation. v6.0/v10.0 saves were part of Apr 19 same-bar bugfix series.
**Slot:** `USER;53163d00de3843f1a78c67bfc88dbf6d` · script v10.0 · `staged/indicator_vwap_dev_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | BONK | 4H | 4524592285 | retained (PF 0.95, marginal — pending re-eval) |
| ✓ | ETH | 4H | 4524592433 | retained (PF 0.47, catastrophic but kept — used as clone source) |
| ✓ | PENGU | 4H | 4478619043 | retained (PF 0.97, marginal — pending re-eval) |
| ✓ | FARTCOIN.P | 4H | 4606125661 | **WF passer (PF 3.88, Tier A 12%)** |
| ✓ | MOODENG.P | 4H | 4606125675 | **WF passer (PF 2.13, Tier B 9%)** |
| ✓ | JUP | 4H | 4606092343 | **WF passer (PF 1.89, Tier C 6%)** |
| ✓ | PNUT | 4H | 4606392921 | **WF passer (PF 1.52, Tier C 6%) — added 2026-05-02** |
| — | _culled 2026-05-02:_ SOL 1H (`4576190178`) | | | catastrophic 1H (PF 0.24) |

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
| **BONK** | 4H | 4H | — | — | 4H |
| **ETH** | — | — | — | 4H | 4H |
| **FARTCOIN.P** | — | — | — | **4H ✦** | **4H ✦** |
| **JUP** | 4H | — | — | — | **4H ✦** |
| **MOODENG.P** | — | — | — | — | **4H ✦** |
| **PENGU** | 4H | 1H | — | 4H | 4H |
| **PNUT** | — | — | — | — | **4H ✦** |
| **INJ.P** | — | — | **4H ✦ EVM** | — | — |
| **RENDER** | 4H | 1H | — | 1H | — |
| **WIF** | — | 4H | — | — | — |

✦ = WF-validated passer deployed 2026-05-02, sized at A=12%/B=9%/C=6% per `config_sizing_overrides.yaml`.

---

## Open issues / cleanup candidates

- Donchian source still staged but never validated; consider archiving
- Flickers held but not expanded: EMA Ribbon SOL 1H (1/4 nights), EMA Ribbon SOL 4H (1/4 nights kept under SOL coverage). Re-evaluate after one more week.

---

## Changelog

| Date | Event |
|---|---|
| 2026-05-03 | **Tier promotion on top-PF combos**: 5 nights of post-fix WF data confirmed FARTCOIN slots are stable at PF 4+. Manually promoted in `config_sizing_overrides.yaml` (source=manual_promotion). Stoch RSI/FARTCOIN/4H 12%→18% (Tier A+), VWAP Dev/FARTCOIN/4H 12%→18% (Tier A+), VWAP Dev/MOODENG/4H 9%→12% (Tier A). Other slots (JUP, PNUT, INJ, ETH, SOL) unchanged — lower PF, conservative posture. Per-trade size on FARTCOIN signals goes from ~$18 to ~$27 — 50% bigger contract on the highest-conviction combos. PF written matches promotion (4.49/3.88/2.13) so future nightly merge logic preserves promotions. |
| 2026-05-03 | **Aggressive cull of confirmed losers**: deleted 6 alerts that re-validated at PF<0.7 with N≥20 trades on `nightly_20260503_0321`: FVG/RENDER/4H (PF 0.41), VWAP Dev/ETH/4H (PF 0.47), EMA Ribbon/RENDER/1H (PF 0.50), FVG/BONK/4H (PF 0.64), FVG/JUP/4H (PF 0.66), EMA Ribbon/WIF/4H (PF 0.67). Also bumped `risk.max_open_positions` 7→10 in config.yaml so the 8 sized combos + remaining alerts have headroom. Total alerts 21 → 15. |
| 2026-05-03 | **Liq Sweep / SOL / 4H regime-bet deploy**: Created new alert (id `4608026983`, BINANCE:SOLUSDT 4H) on the dormant Liq Sweep slot. Combo showed up in 2 of 3 analog windows (post-FTX PF 1.29, mid-2023 PF 1.02). SOL is 40% similar to current regime (partial analog) — sized at Tier C 6% for lower-conviction regime bet. Total alerts 20 → 21. |
| 2026-05-03 | **Regime-bet sizing for Stoch RSI / ETH / 4H**: regime_check.py ran 2026-05-03 02:44 UTC, ETH scored **80% similarity to the post-FTX (Nov 2022 - Mar 2023) analog window** where Stoch RSI / ETH / 4H ran PF 2.03 (vs 0.61 baseline). Existing alert (id `4454015121`, BINANCE:ETHUSDT 4H) is active and just fired 00:08 UTC. Added manual entry to `config_sizing_overrides.yaml` at Tier B (9% sizing, source=regime_analog) so the bot sizes it like a validated mid-PF combo. Persists across nightly runs (merge logic keeps higher-PF entry). Re-eval if regime drifts. |
| 2026-05-02 | **EVM Phase 3 + INJ alert deploy**: trade_engine.py wired to route EVM symbols (INJ + 6 near-misses pre-mapped) through new EVMSwapExecutor → OpenOcean → Arbitrum. Encrypted EVM wallet at `0x74F29429...` funded with $100 USDC + ~$15 ETH. Created `Liq Sweep / INJ.P / 4H` alert (id `4606986738`) on `BINANCE:INJUSDT.P` symbol — first EVM-routed alert. Total alerts 19 → 20. Earlier $2 USDC → 16.31 ARB canary swap on Arbitrum proved the integration end-to-end. |
| 2026-05-02 | **FOCUS_TOKENS expansion + PNUT deploy**: added 6 Jupiter-tradeable Solana tokens to backtest universe via 3 new exchange data fetchers (Coinbase: KMNO, DBR; OKX: ACT, GOAT, ZEUS; Binance.US: ME). 2 candidates dropped: WBTC (Coinbase data ended Dec 2024 — delisted), GRASS (only 49 OKX bars). New nightly tested 690 combos vs 460 prior. **0 of the 6 new tokens passed WF** (KMNO/DBR have only 30 days history; ACT/GOAT/ZEUS have history but no edge above PF 1.4; ME max PF 1.04 with ≥30 trades). However, PNUT (existing token) crossed WF gate this run at PF 1.52 — deployed `VWAP Dev / PNUT / 4H` (alert_id `4606392921`, Tier C 6%). 18 → 19 alerts. |
| 2026-05-02 | **Conservative cull + WF passer deploy**: deleted 10 alerts (4 triple-flagged stale 4H + 6 catastrophic 1H, all PF<0.5 or stale+failing) and created 4 alerts on WF-validated passers from `nightly_20260502_0403`: Stoch RSI/FARTCOIN.P/4H, VWAP Dev/FARTCOIN.P/4H, VWAP Dev/MOODENG.P/4H, VWAP Dev/JUP/4H. FARTCOIN/MOODENG use `BINANCE:<TOKEN>USDT.P` perp symbols (no spot listing on Binance). Trade engine symbol normalization patched to strip `.P` suffix. Net 24 → 14 → 18 alerts. Liq Sweep indicator went from 2 → 0 active alerts. |
| 2026-05-02 | **Live re-pull during audit**: confirmed 24 active alerts. Added missing `EMA Ribbon SOL 1H` (alert_id `4454014990`). WF-alignment claim invalidated by post-fix-gate re-validation: 0/24 of original deployment passed. Stale 4H slots flagged. See `backtesting/results/audit_deployment_vs_wf_20260502.pdf`. |
| 2026-04-28 | **Apr 20 FVG-dup cleanup**: deleted 3 disabled FVG alerts (`4513570647` BONK 4H, `4513571230` RENDER 4H, `4513571327` PENGU 4H). All Apr 20 batch with 8 inputs (missing `in_8`) — duplicates of the Apr 13/16 originals on the same tokens/TFs. Total 27 → 24 (100% active). |
| 2026-04-28 | **WF-alignment cull + deploy**: deleted 16 alerts that never validated in last 4 nightly walk-forward runs (5 Liq Sweep non-ETH, 4 FVG non-validated, 3 EMA Ribbon, 1 Stoch RSI JUP 4H, 3 VWAP Dev non-validated). Created 3 new alerts on stable 3-4/4 validators: VWAP Dev SOL 1H (`4576190178`), Stoch RSI BONK 1H (`4576190853`), EMA Ribbon RENDER 1H (`4576191015`). Total 40 → 27. WF alignment 49% → 100%. |
| 2026-04-28 | **Duplicate cleanup**: deleted 3 alerts via webpack 359399 `deleteAlerts`. Removed: Liq Sweep ETH 4H `4513574533` (dup of `4454017945`), Stoch RSI PENGU 4H `4524595484` (malformed `in_0` carried "secret: " prefix), VWAP Dev PENGU 4H `4524593076` (dup of `4478619043`, missing `in_8`). Total 43 → 40. |
| 2026-04-28 | **Stale pine_version rebind**: 19 alerts brought to current versions via `modifyRestartAlert` (10 EMA Ribbon v1.0→v3.0, 5 Stoch RSI v1.0→v3.0, 4 VWAP Dev v6.0→v10.0). Webhooks + fire history preserved. |
| 2026-04-28 | **FVG v1.1 deployed**: new slot, all 9 v1.0 alerts repointed; ETH/SOL alerts created; 3 Apr 20 dups disabled. CLOSE-spam fix splits exit state from exit signal. |
| 2026-04-21 | **Cull**: 49 alerts removed (legacy indicators, low-cap memes failing WF, all Donchian). Down to 47 active. |
| 2026-04-19 | **Same-bar BUY→CLOSE bugfix**: removed `short_exit` from all 6 indicators. Stoch RSI was responsible for 22/25 fee-only loops. Bulk-rebind 36 alerts. |
| 2026-04-17 | **Long-only refactor**: removed SELL signals from all 6 indicators (strategy-isolation: SELL on one indicator was liquidating positions opened by another). |
