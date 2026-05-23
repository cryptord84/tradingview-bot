# Indicator & Alert Deployment Status

**Last verified:** 2026-05-19 ~10:00 EDT (deployed VWAP Dev/ARB/1D + EMA Ribbon/BTC/1D; full reconciliation against alert_list)
**Source of truth:** TradingView (`alert_list` MCP / webpack 560065 `getAlertsCollection()`). This doc is a snapshot — always re-pull live state before acting.

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
| 1 | FVG v1.1 | `USER;3156f00306a244688b2d8de21cd03dbe` | 2.0 | `staged/indicator_fvg_v1.1.pine` |
| 3 | EMA Ribbon v1.0 | `USER;f060080f798d46efa6ee90ea4356190a` | 4.0 | `staged/indicator_ema_ribbon_v1.0.pine` |
| 0 | Liquidity Sweep v1.0 (retired) | `USER;12e465c59f0941d2a4fef70e58003c45` | 4.0 | `staged/indicator_liq_sweep_v1.0.pine` |
| 4 | Stochastic RSI v1.0 | `USER;fea633ae4e5a488c8ccea5efd448b93a` | 4.0 | `staged/indicator_stoch_rsi_v1.0.pine` |
| 6 | VWAP Deviation v1.0 | `USER;53163d00de3843f1a78c67bfc88dbf6d` | 11.0 | `staged/indicator_vwap_dev_v1.0.pine` |
| 0 | FVG v1.0 (retired) | `USER;4852215f50f54cbdad7d6ae82fb4ff07` | 5.0 | `staged/indicator_fvg_v1.0.pine` |
| 4 | Donchian Breakout v1.0 | `USER;6a0a490366d34845bed8071a79198cde` | 6.0 | `staged/indicator_donchian_v1.0.pine` |
| 0 | Donchian + ADX v1.0 (retired) | `USER;bf538897546a48519a83e588ff562e72` | 2.0 | `staged/indicator_donchian_adx_v1.0.pine` |
| 0 | EMA Ribbon + ADX v1.0 (retired) | `USER;c0ffe8e0dd034504a05de359eb6d41bd` | 2.0 | `staged/indicator_ema_ribbon_adx_v1.0.pine` |

**Totals:** 16 alerts (16 active, 0 inactive), 4 indicators in production (VWAP Dev, Stoch RSI, EMA Ribbon, Donchian). 5 indicators retired (FVG v1.0, FVG v1.1, Liq Sweep, Donch+ADX, EMA+ADX).

**Timeframe split:** 10 alerts on 1D (WF-validated), 6 alerts on 4H (WF-passing or live-profitable).

**WF alignment:** 13/16 WF-validated passers. 3 live-performance keeps (RENDER/Donchian 4H 89% WR, PNUT/VWAP Dev 4H 100% WR, BONK/EMA Ribbon 4H +$1.24).

**Note on FARTCOIN/MOODENG perp symbols:** These tokens have no Binance Spot listing — alerts use `BINANCE:<TOKEN>USDT.P` (perpetual). The trade engine's symbol normalization was patched 2026-05-02 to strip the `.P` suffix so webhook payloads route correctly.

---

## FVG v1.1 — Fair Value Gap (CLOSE-spam fixed)

**Logic:** edge-triggered FVG fill detection. v1.1 splits exit *state* from exit *signal* — eliminates ~270/day CLOSE webhook spam from v1.0.
**Slot:** `USER;3156f00306a244688b2d8de21cd03dbe` · script v1.0 · `staged/indicator_fvg_v1.1.pine`
**Deployed:** 2026-04-28

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | NEAR | 1D | 4768403384 | **WF passer (PF 1.57, OOS 1.84)** — added 2026-05-23, Binance.US lane |
| ✓ | PENGU | 4H | 4478628322 | 2026-04-28 |
| — | _culled 2026-05-03:_ BONK (`4454018061`) PF 0.64, JUP (`4478601735`) PF 0.66, RENDER (`4454018043`) PF 0.41 | | | |

---

## EMA Ribbon v1.0

**Logic:** 3/8/21/55 EMA ribbon expansion + RSI confirmation. Long-only since Apr 17 refactor; same-bar BUY→CLOSE bug fixed Apr 19 (v1.0 → v3.0).
**Slot:** `USER;f060080f798d46efa6ee90ea4356190a` · script v4.0 · `staged/indicator_ema_ribbon_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | BONK | 4H | 4454015047 | live-performance keeper (+$1.24, 3/6) |
| ✓ | SOL | 1D | 4665962741 | **WF passer** — added 2026-05-10 |
| ✓ | BTC | 1D | 4736445587 | **WF passer (PF 1.54, OOS 1.32)** — added 2026-05-19, Binance.US lane |
| — | _culled 2026-05-03:_ RENDER 1H (`4576191015`) PF 0.50, WIF 4H (`4454015089`) PF 0.67, PENGU 1H (`4493207481`) | | | |
| — | _culled 2026-05-02:_ ETH 4H, RENDER 4H, SOL 1H, SOL 4H, PENGU 1H (original) | | | |

---

## Liquidity Sweep v1.0

**Logic:** wick-rejection detection at swing highs/lows; edge-triggered sweep + reclaim. Same-bar bugfix Apr 19 (v1.0 → v3.0).
**Slot:** `USER;12e465c59f0941d2a4fef70e58003c45` · script v4.0 · `staged/indicator_liq_sweep_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| — | _culled 2026-05-15:_ SOL 4H (`4608026983`), UNI 4H (`4659629664`) — profitability overhaul |
| — | _culled 2026-05-08:_ INJ.P (`4606986738`) — Phase 4 audit found Arbitrum INJ address invalid + zero Arbitrum liquidity for the canonical Injective token |
| — | _culled 2026-05-02:_ ETH 1H (`4454017961`) PF 0.40, ETH 4H (`4454017945`) PF 0.93 |

---

## Stochastic RSI v1.0

**Logic:** K/D crossover in oversold zone + RSI<50 trend filter. Was the dominant source of fee-only churn pre-Apr 19 (22/25 BUY→CLOSE loops). v1.0 → v3.0 fix removed `short_exit`.
**Slot:** `USER;fea633ae4e5a488c8ccea5efd448b93a` · script v4.0 · `staged/indicator_stoch_rsi_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | FARTCOIN.P | 4H | 4606125639 | **WF passer (PF 4.49, Tier A+ 18%)** |
| ✓ | OP | 1D | 4665962133 | **WF passer** — added 2026-05-10 |
| ✓ | ARB | 1D | 4665962784 | **WF passer** — added 2026-05-10 |
| ✓ | ETH | 1D | 4765875052 | **WF passer (PF 1.72, OOS 1.66)** — added 2026-05-22, Solana/Jupiter |
| — | _culled 2026-05-15:_ PENGU 4H (`4479801456`) — profitability overhaul |
| — | _culled 2026-05-08:_ ETH 4H (`4454015121`) PF 0.65, SOL 4H (`4454015105`) PF 0.85, RENDER 1H (`4454015587`) PF 0.86 | | | |
| — | _culled 2026-05-02:_ BONK 1H (`4576190853`), PENGU 1H (`4558016704`) | | | catastrophic 1H |

---

## VWAP Deviation v1.0

**Logic:** anchored VWAP ± deviation bands; mean-reversion entry on band touch + momentum confirmation. v6.0/v10.0 saves were part of Apr 19 same-bar bugfix series.
**Slot:** `USER;53163d00de3843f1a78c67bfc88dbf6d` · script v11.0 · `staged/indicator_vwap_dev_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | FARTCOIN.P | 4H | 4606125661 | **WF passer (PF 3.79, Tier A+ 22%)** |
| ✓ | MOODENG.P | 4H | 4606125675 | **WF passer (PF 2.13, Tier A 18%)** |
| ✓ | PNUT | 4H | 4606392921 | **WF passer (PF 1.52, Tier B 13%) — added 2026-05-02** |
| ✓ | AAVE | 1D | 4665962766 | **WF passer** — added 2026-05-10, EVM via Arbitrum |
| ✓ | LDO | 1D | 4665962153 | **WF passer** — added 2026-05-10, EVM via Arbitrum |
| ✓ | ARB | 1D | 4736423474 | **WF passer (PF 2.40, OOS 2.27)** — added 2026-05-19, EVM via Arbitrum |
| — | _culled 2026-05-15:_ JUP 4H (`4606092343`) -$1.00, LDO 4H (`4659627111`) dup of 1D, COMP 4H (`4659627481`) stacking |
| — | _culled 2026-05-08:_ BONK 4H (`4524592285`) PF 0.95, PENGU 4H (`4478619043`) PF 0.97 | | | |
| — | _culled 2026-05-03:_ ETH 4H (`4524592433`) PF 0.47 | | | |
| — | _culled 2026-05-02:_ SOL 1H (`4576190178`) | | | catastrophic 1H (PF 0.24) |

---

## Donchian Breakout v1.0

**Logic:** 20-bar Donchian channel breakout with volume surge confirmation. Bar-close trigger (trend-following — no intra-bar repaint). Long entry on close above prior channel high + volume > 1.5× MA.
**Slot:** `USER;6a0a490366d34845bed8071a79198cde` · script v6.0 · `staged/indicator_donchian_v1.0.pine`
**Deployed:** 2026-05-06

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| ✓ | RENDER | 4H | 4640525994 | live-performance keeper (8/9, +$1.78) |
| ✓ | BTC | 1D | 4665961105 | **WF passer (PF 2.38, OOS 2.05)** — added 2026-05-10, Binance.US lane |
| ✓ | ETH | 1D | 4665962725 | **WF passer** — added 2026-05-10 |
| ✓ | DOGE | 1D | 4665962753 | **WF passer** — added 2026-05-10 |

---

## Retired / staged-but-unused

### FVG v1.0 (retired 2026-04-28)
- Slot `USER;4852215f50f54cbdad7d6ae82fb4ff07` (script v5.0)
- Replaced by FVG v1.1; all 9 alerts repointed via `modifyRestartAlert`
- Source kept at `staged/indicator_fvg_v1.0.pine` for reference

## Donchian + ADX v1.0 (bull-roster — deployed 2026-05-08 evening)

**Logic:** Donchian Breakout v1.0 with regime gate: `ADX(14) > 25 AND not in BB squeeze`. Designed to suppress entries in chop and only fire during sustained trend regimes.
**Slot:** `USER;bf538897546a48519a83e588ff562e72` · script v1.0 · `staged/indicator_donchian_adx_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| — | _culled 2026-05-15:_ SOL 4H (`4659548364`) -$1.27 — profitability overhaul |

**Bull-window evidence:** FLOKI 4H PF 2.31 (deferred — no Arbitrum liquidity), SOL 4H PF 1.36, MATIC 4H PF 1.16.

---

## EMA Ribbon + ADX v1.0 (bull-roster — deployed 2026-05-08 evening)

**Logic:** EMA Ribbon v1.0 with same regime gate: `ADX(14) > 25 AND not in BB squeeze`. Highest WIN-RATE family in bull-window backtest (18% of combos passed PF≥1.4).
**Slot:** `USER;c0ffe8e0dd034504a05de359eb6d41bd` · script v1.0 · `staged/indicator_ema_ribbon_adx_v1.0.pine`

| status | symbol | TF | alert_id | notes |
|---|---|---|---|---|
| — | _culled 2026-05-15:_ SOL 4H (`4659632593`), UNI 4H (`4659632668`), ARB 4H (`4659634147`) — profitability overhaul |

**Note on bull-roster auto-size-up:** `regime_detector.py` writes `source: bull_regime` entries to `config_sizing_overrides.yaml` when `BULL_CONFIRMED` transition detected, removing them on `BULL_LOST`. See `feedback_position_sizing_levers.md` and the project memory `project_bull_cycle_indicators.md` for details.

---

## Retired / staged-but-unused

### FVG v1.0 (retired 2026-04-28)
- Slot `USER;4852215f50f54cbdad7d6ae82fb4ff07` (script v5.0)
- Replaced by FVG v1.1; all 9 alerts repointed via `modifyRestartAlert`
- Source kept at `staged/indicator_fvg_v1.0.pine` for reference

### Deferred bull-roster combos (no current execution lane)
- **Donch+ADX/FLOKI/4H** (PF 2.31 in 2023-Q4) and **Liq Sweep/FLOKI/4H** (PF 1.45 in 2024-Q4) — FLOKI has zero Arbitrum liquidity. Recovery requires BSC chain extension. See `config/bull_roster.yaml` `deferred_combos`.

---

## Token coverage matrix (active alerts)

|  | EMA Ribbon | Stoch RSI | VWAP Dev | Donchian |
|---|---|---|---|---|
| **AAVE** | — | — | **1D ✦** | — |
| **ARB** | — | **1D ✦** | **1D ✦** | — |
| **BONK** | 4H ▲ | — | — | — |
| **BTC** | **1D ✦** | — | — | **1D ✦** |
| **DOGE** | — | — | — | **1D ✦** |
| **ETH** | — | — | — | **1D ✦** |
| **FARTCOIN.P** | — | **4H ✦** | **4H ✦** | — |
| **LDO** | — | — | **1D ✦** | — |
| **MOODENG.P** | — | — | **4H ✦** | — |
| **OP** | — | **1D ✦** | — | — |
| **PNUT** | — | — | **4H ▲** | — |
| **RENDER** | — | — | — | **4H ▲** |
| **SOL** | **1D ✦** | — | — | — |

✦ = WF-validated passer (nightly backtest walk-forward confirmed).
▲ = live-performance keeper (strong real-trade results justify retention despite no WF pass).

---

## Open issues / cleanup candidates

- **BONK/EMA Ribbon 4H**: live-profitable (+$1.24, 3/6) but no WF pass. Monitor through next week; cull if it stops performing.
- **RENDER/Donchian 4H**: exceptional live results (8/9, +$1.78) but nightly WF doesn't pass it. Keep as long as live performance holds.
- **PNUT/VWAP Dev 4H**: perfect live record (4/4, +$2.76). Watch for regression as sample grows.

---

## Changelog

| Date | Event |
|---|---|
| 2026-05-19 | **Deployed VWAP Dev/ARB/1D + EMA Ribbon/BTC/1D**: Gap analysis against `nightly_20260518_0403` identified 2 WF-passing combos not yet deployed. Created alerts via TV UI automation; pine_versions corrected via `modifyRestartAlert` (ARB v10→v11, BTC v3→v4). ARB routes EVM/Arbitrum, BTC routes Binance.US. **Total alerts 14→16** (13 tokens, 10 1D + 6 4H). Full section reconciliation: added missing May 10 1D deploys to section tables, marked May 15 culls in Liq Sweep/Donch+ADX/EMA+ADX/Stoch RSI/VWAP Dev sections. |
| 2026-05-15 | **Profitability overhaul — 13 underperforming 4H alerts culled, SL widened**: Data-driven cleanup based on May 1-15 P&L analysis. Deleted 13 alerts that failed WF AND underperformed live: PENGU/Stoch RSI, PENGU/FVG (biggest loser -$2.61), JUP/VWAP Dev (-$1.00), SOL/Donch+ADX (-$1.27), COMP/VWAP Dev (stacking positions), LDO/VWAP Dev 4H (dup of 1D, stacking), FLOKI/Donch+ADX, FLOKI/Liq Sweep, ARB/EMA+ADX, UNI/EMA+ADX, SOL/EMA+ADX, UNI/Liq Sweep, SOL/Liq Sweep. **SL multiplier widened**: global 1.5→2.0 ATR, memecoins 2.0→2.5 ATR (15 SL exits avg -4.2% vs 11 TP exits avg +8% — SL was triggering on normal volatility). Removed PENGU/JUP from token_overrides. Roster: 27→14 alerts (8 1D + 6 4H), 4 active indicators (down from 8). All remaining alerts refreshed via modifyRestartAlert. |
| 2026-05-13 | **CLOSE/SELL signals removed from all 9 Pine indicators**: all indicators now BUY-only — `long_exit` variables, `if long_exit` alert blocks, CLOSE plotshapes, and CLOSE alertconditions stripped from all 9 staged Pine files (165 lines total). 8 production scripts recompiled+saved to TV slots (FVG v1.0 retired, skipped). All 27 alerts verified active post-save. Bot-side `ignore_close_signals` flag (deployed 2026-05-10) is now redundant but harmless. Webhook volume expected to drop ~75%. Commit `5182b0b`. |
| 2026-05-13 | **Dashboard: Opened/Current columns + date/strategy alignment**: added `Closed At` timestamp and `Current` price columns to closed positions table; aligned column order (Symbol, date, Strategy) between open and closed positions tables. Commit `4717bd3`. |
| 2026-05-09 | **Backtest universe expansion (8 new tokens + MATIC→POL fix)**: probed all 199 USDT pairs on Binance.US, found 56 not yet in our backtest. Added BTC, XRP, DOGE, SUI, ADA, HYPE, ONDO, ASTER (Tier 1 + Tier 2) to `BINANCE_TOKENS` in `backtesting/data.py`. Removed MATIC (silently delisted on Binance.US — fetches were returning empty, masked nightly was wasting compute on a dead token), replaced with POL (rebranded MATIC). Total backtest universe now 40 Binance + 6 other = 46 tokens × 16 strategies × 3 timeframes = ~2200 nightly tests (~5–10 min added). Tonight's 04:03 UTC run picks these up. Any new WF passers will be deployable via Binance.US lane. |
| 2026-05-09 | **FLOKI bull-roster combos deployed (manual TV UI)**: 2 alerts added on FLOKI/4H — Donch+ADX (PF 2.31, highest in bull-window family) and Liq Sweep (PF 1.45). Both route through new Binance.US lane validated by canary trade earlier today. These were "deferred" yesterday because we thought BSC chain extension was needed — turned out FLOKI is on Binance.US with $5171 max market order. Cleanest unlock of the session. |
| 2026-05-09 | **Binance.US Phase 2 canary validated**: $5 FLOKI round-trip via market BUY + market SELL. Round-trip cost $0.02 (0.41% — better than projected 0.6%). Two production gotchas caught + fixed during canary: (1) INJ not actually listed on Binance.US (US regulatory) — removed from BINANCE_TOKENS; (2) MARKET_LOT_SIZE filter is stricter than LOT_SIZE — caps single market order $; (3) buy fees taken in the asset purchased (not USDT), so post-buy free balance is qty − fees, not qty. Position monitor's `_close_position_binance` correctly refetches free balance before sell so production code is unaffected. KAVA also dropped (max market $2.56, below useful trade size). |
| 2026-05-09 | **Binance.US Phase 2 — third execution lane shipped**: full trading integration on top of yesterday's read-only Phase 1 client. New `BINANCE_TOKENS` registry in trade_engine (INJ, KAVA initially), `_is_binance_symbol()` routing, market BUY (quoteOrderQty) + market SELL (qty quantized to LOT_SIZE), pre-flight min-notional check, Jupiter-shaped result for downstream code reuse. Position monitor extended with `_close_position_binance()` for TP/SL via market sell. `tradeable_usd` now includes Binance USDT but only when symbol routes to Binance (avoids over-sizing Solana/EVM trades). Dashboard `/binance_us/balance` + `/binance_us/trades` endpoints + `binance_us` section in `/portfolio` + 5th wallet card in frontend with allocation bar. **Account state**: $97.28 USDT funded, ready to deploy. **Next step**: re-create the Liq Sweep / INJ / 4H alert in TV — first BUY signal will route to Binance.US end-to-end. Bot restart required to pick up changes. |
| 2026-05-09 | **Binance.US Phase 1 — read-only API client**: `app/services/binance_us_client.py` with HMAC-SHA256 signing, no external library. Health-check cron `com.tradingbot.binance-us-health` (every 30 min) Telegrams on auth failure (catches ISP IP-whitelist breaks). Smoke test passed: ping + clock skew (9ms) + auth + balance enumeration. |
| 2026-05-11 | **crypto_strikes re-enabled with calibrator + nightly rebuild cron**: empirical PAV-isotonic calibration table built (`backtesting/build_btcd_calibrator.py` → `data/btcd_calibration.json`, 655K post-hotfix samples, 51 breakpoints). Wired through `score_market(use_calibrator=True)` + `app/services/btcd_calibrator.py` singleton. **OOS validation** (`validate_btcd_calibrator_oos.py`, split 2026-05-04): TEST set max bucket gap 2.7pt — passes ≤5pt re-enable gate, bias correction generalizes. **Selection-bias reconciliation** (`btcd_selection_bias_audit.py`): MAJOR REVERSAL — crypto_strikes was NOT the Kalshi bleeder; 72 settled trades netted +$28.44, the capital drop came from spread_bot + arb. Selection bias inside 50-80% bucket is real (-19pt) but absorbed by underlying edge. Calibrator counterfactual: +$31.58 with 29 fewer trades, winrate 61% → 74%. **Flipped `crypto_strikes.enabled: true`** with `use_calibrator: true`, `min_fair_prob: 0.60` (on calibrated prob), `max_cost_per_trade: $1`, `max_open_positions: 3`. First post-restart trade fired immediately: KXBTCD-26MAY1119-T81799.99 @ 68¢, calibrated fair=0.773, edge +9.3¢. **Nightly calibrator rebuild cron** (`backtesting/btcd_calibrator_nightly.sh` + `com.tradingbot.btcd-calibrator.plist` @ 01:40 ET — between Kalshi nightly 01:30 and BTCD audit 01:45): backs up prev table, rebuilds, restores on failure, Telegrams if any bucket drifts past ±5pt. Wrapper smoke-tested end-to-end. Memos: `project_btcd_audit_20260511.md`, `project_btcd_selection_bias_20260511.md`. |
| 2026-05-11 | **Kalshi fix pass — spread_bot & shared infra cleared, BTCD HOLD**: worked through `project_kalshi_reenable_tracker.md`. (1) **Asymmetric-fill skip rule** verified live at `kalshi_spread_bot.py:707-718` with 60¢/60¢ defaults. (2) **Capital floor gate** added: `min_capital_cents: 10000` in config + `_run_loop` precheck — spread_bot refuses to start if Kalshi balance < $100. (3) **`sync_kalshi_positions` patched**: open rows refreshed each cycle, closed rows preserved with realized P&L from API (was DELETE-all every 120s). Sub-breaker `_get_category_pnl()` query now functional. Verified idempotent via synthetic 2-call test. (4) **SELL trade logging added** to `_exit_position`, `flatten_market` (YES+NO), and `_emergency_flatten` — future flatten events have audit trail in `kalshi_trades`. (5) **BTCD audit verdict (`backtesting/results/btcd_audit_20260511_0145.txt`)**: post-hotfix 20-50% bucket overpredicts YES by 7.9pts (gate: ≤5pts). Bidirectional overdispersion across all mid buckets. crypto_strikes stays disabled. Recommended fix: empirical calibration table from accumulated `kalshi_strikes_calibration.jsonl` (~12k+ samples/bucket). See `project_btcd_audit_20260511.md`. Tracker `project_kalshi_reenable_tracker.md` updated with what's resolved. |
| 2026-05-11 | **Kalshi quiet mode** (autonomous action, user away). Overnight loss $28.61 tripped the new $20 daily cap (cap was 1000→2000c per 2026-05-10 tightening; functioning as designed). Circuit breaker emergency-flattened 22 positions + 35 orders at 01:53 UTC. Sequential nightly bleed (-$45 → -$28.61) confirms 2026-04-27 audit memo's "OTM YES overpredicts 20pts in 20-50% bucket" remains active despite 2026-05-10 gate tightening. **Action**: disabled 3 Kalshi bots in `config.yaml` — `crypto_strikes.enabled: false`, `spread_bot.enabled: false`, `arbitrage.enabled: false`. Whale tracker (read-only) remains. Kalshi balance preserved at $41.53 cash, 0 open positions, no exposure. Per-category sub-breaker (added 2026-05-10) didn't fire because `kalshi_positions` DB table is empty — bots write in-memory P&L only, sub-breaker structurally broken until that gap is closed. Re-enable criteria documented in `project_kalshi_quiet_mode_20260511.md`. Restart needed to apply config. |
| 2026-05-10 | **Trade engine UnboundLocalError fix + chain-aware price routing + webhook validator**: caught the bug that lost the 16:00 EDT FLOKI BUY (`token_symbol` referenced before assignment in the Binance.US lane block). Lost FLOKI + SOL BUYs recovered via manual replay at $23.66 + $11.60 Tier B sizing (Position #174, #175). Shipped `app/services/price_router.py` (chain-aware: Solana→Jupiter, EVM→OpenOcean, Binance.US→WS) — eliminates 2026-05-06 zombie-listing fake-SL class permanently. Per-token Feed dot in dashboard token list (live/stale/on-demand). `webhook.ignore_close_signals: true` filter shipped (audit showed indicator CLOSEs closed positions at +1.4% vs TP at +8% — leaving money on the table). 75% warning + per-category sub-breaker added to Kalshi risk manager (sub-breaker latent until kalshi_positions DB writes wired). Webhook pre-flight validator (`backtesting/webhook_preflight.py`) — synthetic dry-run BUYs catch UnboundLocalError-class bugs across all 3 lanes; verified 5/5 pass post-fix. Nightly price-source audit cron (03:30 UTC). REST seed for price_feed at WS connect — eliminates zombie-listing first-tick gap. |
| 2026-05-10 | **8 new 1D alerts deployed (2-sample WF confirmed)**: tonight's `nightly_20260510_0403` produced an identical 16-passer set as yesterday's kitchen-sink — clean 2-sample stability proof. Deployed all 8 fresh combos at Tier C 9% pos_size (conservative first-deploy posture; bot's nightly will auto-promote based on accumulated PF history). Alerts created via `getAlertsCollection().createAlert()` programmatic path with the fetch-interceptor that re-injects `pine_id` + `pine_version` (same pattern as the modifyRestartAlert fix from 2026-05-08 — first validated use of `createAlert` via this path; previously only modifyRestartAlert was confirmed). **New alerts**: Donchian/BTC/1D `4665961105`, Stoch RSI/OP/1D `4665962133`, VWAP Dev/LDO/1D `4665962153`, Donchian/ETH/1D `4665962725`, EMA Ribbon/SOL/1D `4665962741`, Donchian/DOGE/1D `4665962753`, VWAP Dev/AAVE/1D `4665962766`, Stoch RSI/ARB/1D `4665962784`. **Total alerts 19 → 27** (13 → 18 alerted tokens). VWAP Dev/LDO/1D fired immediately on creation (alert evaluated at create-time and found trigger condition already true). BTC/ETH/DOGE/OP/AAVE got their first-ever alerts; LDO/SOL/ARB got second-strategy adds on top of existing 4H alerts. Donchian/BTC/1D was the priority deploy — first BTC signal we've ever had (PF 2.38, OOS 2.05, 38 trades, classic Turtle-style daily breakout). ETH routes via Solana/Jupiter (existing wrapped-ETH path). AAVE routes via EVM lane — first AAVE alert exercises the just-shipped Aave V3 JIT auto-withdraw if the wallet is short. `data/active_alerts.json` snapshot refreshed — also corrected pine_id↔strategy mapping (USER;53163d00... = VWAP Dev v10.0, was mistakenly tagged as Mean Rev v1.3.3 in earlier snapshot). |
| 2026-05-09 | **Aave V3 USDC supply integration (full automation)**: shipped `app/services/aave_v3_client.py` (read-only + write methods + auto-helpers), `/api/aave/{status,deposit,withdraw}` endpoints, dashboard EVM sub-card mini-controls mirroring Kamino UX. Pool `0x794a61358D6845594F94dc1DB02A252b5b4814aD`, USDC `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`, aUSDC `0x724dc807b04555b71ed48a6896b6F41593b8C637` on Arbitrum. Config: `aave_v3.reserve_usdc=25, auto_deposit=true, approve_mode=infinite, max_gas_gwei=0.5`. **Auto-deposit hooks** fire after every successful EVM trade in `trade_engine.process_signal` AND after every EVM TP/SL close in `position_monitor._close_position_evm`. **Auto-withdraw hook** fires before EVM BUY when `wallet_usdc < trade_usd + reserve` — JIT pulls the gap from Aave with Telegram notification. Manual canary: $10 + 80% deposits broadcast successfully; first deposit triggered one-time infinite approve (~$0.005 gas) and supply (~$0.01 gas). Live state: $71 USDC supplied at 3.24% APY, $25 wallet floor. EVM total in dashboard now correctly includes aUSDC (cash + Aave = full EVM exposure). Solana side untouched. |
| 2026-05-09 | **Dashboard wallet redesign + Aave allocation segment**: replaced single-Solana-centric "Wallet & Portfolio" card with 3 side-by-side wallet sub-cards (Solana cyan, EVM purple, Binance.US yellow) each with chain-specific yield section (Kamino mini / Aave V3 mini / "no idle yield" note). Top portfolio header allocation bar split into 6 segments: SOL/KAM/EVM/AAVE/BUS/KAL (Aave purple `#7c3aed` distinct from EVM `#a855f7`). `/api/portfolio.totals` exposes `evm_wallet_usd` (cash) + `aave_usd` (yield) separately for the bar; `evm_usd` keeps full exposure for the sub-card. Removed Kalshi Spread Bot UI (shelved per asymmetric-fill incident); backend service kept for direct-API use if needed. Removed stale `rebalancer.targets` block from `config.yaml` (was disabled for months, drifted from current alert lineup). Token list adds `is_backtested` flag + ● alert pill + chain badges (ARB/BUS/BT). |
| 2026-05-09 | **Backtest matrix prune**: kitchen-sink 16-strategy run produced same 16 WF passers as 11-strategy CORE — confirming 5 ADX regime variants + Ichimoku + RSI Div + MACD Vol + Supertrend earn nothing on this universe. Cut 8 strategies (kept VWAP Dev, Donchian, Stoch RSI, EMA Ribbon, Liq Sweep, FVG, Mean Rev, EMA+ADX) and 2 timeframes (kept 4H + 1D, dropped 15m+1H — 15m averaged PF 0.06 across 186 30-trade combos). Dropped 5 zero-history tokens (DBR, HNT, KMNO, MEW, ORCA — never crossed 30-trade threshold). Matrix: 3216 → 768 tests/nightly (76% reduction), nightly runtime ~12min → ~3min. All 16 WF passers preserved + all close near-misses. Soft-drop list (POL/PEPE/ACT/BONK/GOAT/ZEUS) kept for one more nightly. **1D timeframe added — unlocked BTC + DOGE first-ever WF passes via Donchian.** Analysis written to `backtesting/results/kitchen_sink_analysis_20260509.md`. |
| 2026-05-09 | **FLOKI bull-roster deploy + sizing fix**: created Donch+ADX/FLOKI/4H + Liq Sweep/FLOKI/4H alerts on the new Binance.US execution lane (FLOKI added to `TradeEngine.BINANCE_TOKENS`). Both alerts initially had `pos_size=15` (Tier A); patched in-place to `pos_size=9` (Tier C) via `modifyRestartAlert` with the fetch interceptor that re-injects pine_id/pine_version stripped by the wrapper. Verified server-side via fresh `alert_list`. Total alerts unchanged at 19 (FLOKI joined as the 13th alerted token). Bull-roster combos that were "deferred to BSC" on 2026-05-08 are now executable since FLOKI is on Binance.US — no BSC chain build needed. |
| 2026-05-09 | **Binance.US Phase 2 trading lane shipped + canary validated**: `BinanceUSClient` extended with `place_market_buy_quote`, `place_market_sell_base`, `quantize_qty` for MARKET_LOT_SIZE filter compliance. `trade_engine` and `position_monitor` route `BINANCE_TOKENS` symbols through the CEX path. Canary: $5 FLOKI round-trip cost $0.02 net (0.41% spread+fees) — lane validated. INJ confirmed unavailable on Binance.US (US regulatory). KAVA listed but `MARKET_LOT_SIZE.maxQty` caps single market order at $2.56 — too small. Backtest universe expanded with 8 tradeable additions (BTC, XRP, DOGE, SUI, ADA, HYPE, ONDO, ASTER) + POL replacing dead MATIC = 40 Binance backtest tokens, 46 total. |
| 2026-05-08 | **Kalshi caps raised to match balance growth**: `max_active_exposure_cents` $80→$120, `crypto_strikes_cents` $35→$50, `crypto_cents` $0→$15. Driver: live balance has grown to $70.09 with bot quoting orders that were being rejected by the $0 KXETH/KXBTC non-strike cap. Effective deployment ceiling now $110 (sum of category limits is the binding constraint), giving $40 headroom over current balance. Next bot restart picks up changes. |
| 2026-05-08 | **Bull-roster + LDO/COMP deployed (manual TV UI), 7 new alerts**: 5 bull-roster combos (Donch+ADX/SOL, EMA+ADX SOL/UNI/ARB, Liq Sweep/UNI) at Tier C 9% in chop — auto-sized to B/A on BULL_CONFIRMED via regime_detector. 2 near-passer combos (LDO/COMP/4H VWAP Dev) at 9% — stable PF 1.46-1.51 with strong OOS, fails IS_PF gate only. **5 alerts now use EVM/Arbitrum lane** (LDO, COMP, ARB EMA+ADX, UNI EMA+ADX, UNI Liq Sweep) — first live EVM alerts since INJ.P cull. Total alerts 10 → 17. |
| 2026-05-08 | **Trailing stop offset widened 2.0 → 3.0 ATR**: TP/SL audit (`backtesting/tp_sl_audit.py`) of last 14d / 54 closed positions found trail-stop closures had **-1% efficiency** (avg MFE +3.66% vs realized +0.13%) — i.e., positions peaked +3-4% above entry then trail clipped them at near-breakeven. Worst pattern: EMA Ribbon trail closures (7 positions, $2.08 left on table). Root cause: with offset=2.0 ATR and memecoin ATR ~5%, trail SL was just above entry on activation, exiting on normal pullbacks. Widening to 3.0 ATR adds 50% more cushion. Bot restart required to apply. |
| 2026-05-08 | **TV API modify workaround — fetch interceptor unblocks `modifyRestartAlert`/`createAlert`**: The wrapper at webpack 560065 silently strips `pine_id` and `pine_version` from outgoing request bodies, causing `invalid_request` server responses. Direct REST POST hits CORS. **Workaround**: monkey-patch `window.fetch` to re-inject those two fields into the JSON body before the request goes out. Verified live by changing SOL Donch+ADX `in_11` 15→9 — server returned 200, alert updated, other inputs preserved. Pattern saved in `feedback_tv_alert_api_pine_id_strip.md`. Future bulk-modifies (cull rebinds, version bumps, input changes) are unblocked. |
| 2026-05-08 | **Regime detector — auto-size-up wiring**: rather than triggering alert deploy on `BULL_CONFIRMED`, the detector now manipulates `config_sizing_overrides.yaml` directly. New helpers `apply_bull_overrides()` / `revert_bull_overrides()` write/remove `source: bull_regime` entries for the 5 bull-roster combos (Donch+ADX/SOL C 9%, EMA+ADX/SOL B 13%, EMA+ADX/UNI B 13%, EMA+ADX/ARB B 13%, Liq Sweep/UNI A 18%). On `BULL_LOST` the entries are removed and alerts revert to signal-default (9% Tier C). Idempotent — re-running adds nothing. Added `--apply-bull` / `--apply-bear` flags for manual control. Aliases for "donchian + adx" → `Donch+ADX` and "ema ribbon + adx" → `EMA+ADX` added to `_STRATEGY_NAME_ALIASES`. **Assumption: bull-roster alerts deployed manually before BULL_CONFIRMED fires.** Tested apply→revert→re-apply cycle. |
| 2026-05-08 | **Webhook reconciliation cron**: `backtesting/webhook_reconciliation.py` + launchd `com.tradingbot.webhook-reconciliation` (every 30 min). Compares ngrok inspector (TV→ngrok delivery layer) to `signals_log` (bot processing layer). Flags any: (a) ngrok status != 200 — bot rejected webhook; (b) ngrok 200 but no matching `signals_log` entry — silent loss path (e.g., bot paused). Telegram on first occurrence per request_id (deduplicated via state file). Smoke-tested over 24h window: 15 ngrok ↔ 15 signals_log matched cleanly. Limitation: only sees what reached ngrok. TV-outbound failures need `/list_fires` reconciliation which requires authenticated browser session (not cron). |
| 2026-05-08 | **Phase 6 — sizing rescaling**: 7-day audit found bot opened 26 positions, settled 23 at PF 5.15 / 52% WR, but net P&L only $+3.61 because avg trade size was just $10–15. Diagnosis: tier percentages had been scaled to 62% of original on 2026-05-02 (A=12/B=9/C=6), AND `kamino.reserve_usdc=$150` was acting as a tradeable floor. Pre-empted scheduled 2026-05-11 re-eval given clean data. **Changes**: (1) `nightly.py:SIZING_TIERS` A=12→18, B=9→13, C=6→9; (2) `config.yaml:kamino.reserve_usdc` 150→50 (semantics: floor that subtracts from tradeable); (3) `config.yaml:risk.max_purchase_usd` 220→500 (was binding the new tier sizes); (4) `config_sizing_overrides.yaml` manual A+ entries 18→22 (FARTCOIN×2). Projected per-trade size at fully-unwound state: A+ $77, A $63, B $45, C $31 (3–4× current). Built-in self-throttle: as positions stack tradeable shrinks → new sizes auto-shrink. No change to max_open_positions (10) or correlation caps (memes 3 / majors 2 / sol_defi 2). Bot restart required to pick up `config.yaml` changes. |
| 2026-05-08 | **Failed-swap visibility fix**: previously, when a Jupiter or EVM swap was rejected by the DEX (e.g., Raydium AMM custom error 0x1d "InvalidInput" on stale pool state), the bot silently logged the giant RPC error blob and discarded the signal — no DB record, only a generic Telegram dump. From the user's view this looked identical to a missed webhook. **Fix**: `trade_engine.py` exception handler now detects swap-execution failures via error markers, calls new `_summarize_swap_error()` helper to extract a clean reason (decodes Solana custom error codes via `_SOLANA_CUSTOM_ERRORS`), inserts a `BUY_FAILED` / `SELL_FAILED` record into the `trades` DB with `reason='swap_failed'`, and emits a focused `notify_swap_failed()` Telegram instead of the noisy `notify_error()`. Verified parser against yesterday's actual PNUT failure: `Transaction failed: ... Custom: 29` → `"DEX rejected swap: InvalidInput / pool state stale (Raydium AMM rejected — likely slippage exceeded mid-block)"`. Bot restart required to pick up changes. |
| 2026-05-08 | **INJ.P alert culled**: deleted `4606986738` (Liq Sweep / INJ.P / 4H) via `getAlertsCollection().deleteAlerts()`. Phase 4 audit confirmed zero Arbitrum liquidity for canonical Injective token (only Ethereum mainnet has it, ~$350k pools). The bot's registry address `0x97ad75…` was bogus and any BUY signal would have failed silently — luckily the alert only fired CLOSE signals since 2026-05-02 deploy, so no real money lost. INJ removed from `EVM_TOKENS` registry. Side-effect: a stray inactive INJ.P alert (`4657904580`) created during today's createAlert API probing was also deleted. **Total alerts 11 → 10. No EVM alerts active.** EVM wallet still funded and ready for bull-roster UNI/ARB combos when regime confirms. |
| 2026-05-08 | **Phase 4 EVM lane validation + INJ latent bug**: ran `backtesting/phase4_evm_validate.py` (dry-run swap quotes via `EVMSwapExecutor`). UNI and ARB both succeeded — Arbitrum lane verified end-to-end through bot's actual code path. **INJ failed**: `0x97ad75064b20fb2B2447feD4fa953bF7F007a706` is NOT a valid Arbitrum ERC20 contract (`BadFunctionCallOutput` from web3 + `swap_quote 500` from OpenOcean). Latent bug since 2026-05-02 INJ deploy — undetected because the alert has only fired CLOSE signals (no BUYs), so the broken address never tripped a real trade. Marked `INJ` as `# BROKEN` in `trade_engine.py:EVM_TOKENS`. **FLOKI verdict**: no Arbitrum liquidity at all (OpenOcean tokenList + quote both empty). FLOKI exists on BSC ($15 → 405k FLOKI quote works) and Ethereum mainnet, but bot has neither chain extension yet. Bull roster updated: 7 combos → 5 executable + 2 deferred (FLOKI both). Phase 5 (BSC chain extension) adds ~2-4hrs work to recover FLOKI combos. |
| 2026-05-08 | **Bull-roster deploy recipe + manifest**: `backtesting/regime_deploy.py` + `config/bull_roster.yaml` (7 combos with full inputs/slot IDs/bull PFs). On BULL_CONFIRMED, regime detector Telegram instructs running `venv/bin/python -m backtesting.regime_deploy bull` in Claude. Claude reads the recipe and uses MCP `createAlert`+`restartAlerts` to deploy. Saves IDs to `config/bull_roster_deployed.json` for later undeploy. **Why semi-auto, not full cron:** createAlert via webpack 560065 strips pine_id/pine_version from payload when called outside an active editor context — proven 2026-05-06 Donchian/RENDER deploy pattern only works from a Claude/MCP session with TV loaded. Acceptable trade — user has 1-command confirm step before live alerts go up. |
| 2026-05-08 | **Bull-regime auto-detector deployed**: `backtesting/regime_detector.py` + launchd `com.tradingbot.regime-detector` (daily 01:30 UTC). Checks BTC 1D for: close > EMA(200), ADX(14)>25, ADX rising, not in BB squeeze. State persisted to `regime_state.json`; on bear→bull or bull→bear transition emits Telegram + recommended deploy/pause actions. Baseline at 2026-05-08: NOT BULL (3 of 4 factors true; BTC close $80,017 vs EMA200 $82,040 — single missing factor is close > EMA200). |
| 2026-05-08 | **Bull-cycle indicator pre-build**: created two new Pine indicators in TradingView using `saveNew()` (webpack module 752174) — `Donchian + ADX v1.0` (`USER;bf538897546a48519a83e588ff562e72`) and `EMA Ribbon + ADX v1.0` (`USER;c0ffe8e0dd034504a05de359eb6d41bd`). Both clone existing originals (Donchian v1.0, EMA Ribbon v1.0) with an added regime gate (`ADX(14) > 25 AND not in BB squeeze`) mirroring `backtesting/strategies.py:_apply_regime_filter(regime="trend")`. Both compile clean. **No alerts deployed yet** — these are dormant until bull regime confirms. Driven by `backtesting/bull_period.py` analysis (`results/bull_period_20260508_1529.txt`) showing trend-following families (Liq Sweep, Donch+ADX, EMA+ADX) dominated past bull windows while VWAP Dev / Stoch RSI / FVG underperformed. Originals all intact (Donchian Breakout v5.0, EMA Ribbon v3.0). |
| 2026-05-08 | **5-alert cull (sub-1.0 PF losers)**: deleted Stoch RSI/ETH/4H (`4454015121`) PF 0.65 (regime-B override expired), Stoch RSI/SOL/4H (`4454015105`) PF 0.85, Stoch RSI/RENDER/1H (`4454015587`) PF 0.86, VWAP Dev/BONK/4H (`4524592285`) PF 0.95, VWAP Dev/PENGU/4H (`4478619043`) PF 0.97. Used `getAlertsCollection().deleteAlerts([ids])` on webpack module 560065. Total alerts 16→11. Also corrected stale DEPLOYMENT.md table rows from the 2026-05-03 cull (FVG BONK/JUP/RENDER and EMA Ribbon WIF/RENDER-1H/PENGU-1H were deleted then but never removed from tables). |
| 2026-05-08 | **Daily volume cap raised $200→$500** (`max_daily_volume_cents: 20000→50000` in config.yaml). Sports scanner MLB orders had consumed the full $200 budget by 10:20 AM, blocking all BTCD crypto strikes. Per-component exposure caps ($50 spread, $35 strikes) and asymmetric-fill protection now provide safety. Bot restarted to apply change. |
| 2026-05-06 | **Donchian/RENDER/4H deployed**: First-ever WF pass for Donchian Breakout v1.0 (PF 1.81, OOS 3.16, 30 trades). Created alert `4640525994` on `BINANCE:RENDERUSDT` 4H via `createAlert` + `restartAlerts` REST API. Bar-close trigger (`in_7: false`). Tier C 6% sizing already written by nightly. Total alerts 15→16. |
| 2026-05-06 | **Major fix wave — oracle, no-ATR fail-open, spread bot asymmetric risk, ghost cleanup**: (1) `position_monitor.py:99,230` now strip `.P` suffix in price + wallet lookups (FARTCOIN.P bug). (2) `position_monitor.py:241` special-cases SOL native balance instead of SPL lookup (40 false-ghost SOL positions deleted). (3) `trade_engine.py:1047` now creates position with fallback ATR (2.5% of price) when Pine signal lacks atr field — root cause of 6 orphan tokens (~$50) accumulated across multiple sessions because old Pine indicator versions didn't emit `atr`. (4) `jupiter_client.py:get_token_price` switched from Binance.US REST to Jupiter aggregator quote — Binance.US zombie listings (JUPUSDT $0.10 vs real $0.19) had fake-fired SLs on 4 positions for $4.59 phantom loss. (5) `position_monitor.py:320` now records exit_price from actual swap fill, not monitor trigger price. (6) `kalshi_risk_manager.py` added `max_active_exposure_cents` ($80) gate on real Kalshi cash; daily-volume cap relegated to anti-churn role. (7) `kalshi_spread_bot.py:_requote` skips quoting YES if mid > 60¢ and NO if (100-mid) > 60¢ — avoids one-sided fills on lopsided binary strikes (caused $88 BTCD bleed in 10min). (8) `telegram_commands.py /wallet` now shows ALL Solana SPL + Arbitrum holdings. (9) 7 orphan tokens recovered as `manual_recovery` positions; 5 closed positions retroactively corrected with actual swap fills. New CLAUDE.md hard rules #8 (oracle must match executor) and #9 (BUY position record always required); new memory `feedback_no_atr_fail_closed.md` and `feedback_spread_bot_asymmetric_risk.md`. |
| 2026-05-04 | **realtimeTrig flipped to bar-close on trend-following alerts**: `EMA Ribbon / BONK / 4H` (`4454015047`, `in_8: true→false`) and `FVG v1.0 / PENGU / 4H` (`4478628322`, `in_6: true→false`) now fire only at the 4H bar close, not intra-bar. Eliminates repaint risk and resolves the BONK 13:51 "no triangle visible" mystery (intra-bar trigger that didn't materialize at close). Mean-reversion alerts (Stoch RSI, VWAP Dev) intentionally left on intra-bar — early entries are needed for that family. Modify path: `getAlertsCollection().modifyRestartAlert(alertId, alert)` from webpack module 560065 (the lower-level 359399 wrapper expects a transformed payload that broke direct fetch with `invalid_request`). |
| 2026-05-03 | **JUP & PNUT promoted C→B**: Combined PFs (1.95 / 1.52) just under Tier B threshold, but OOS PFs (2.89 / 2.21) with strong retention (190% / 156%) justify B. JUP and PNUT both fired today; promoting their per-trade size 6%→9% (~$9→$13). |
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
