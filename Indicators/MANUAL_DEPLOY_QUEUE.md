# Manual TV UI Deploy Queue (2026-05-08)

Two batches to add via TradingView right-click → Add Alert → Pine indicator:

## Batch 1 — LDO/COMP near-passers (Tier C, fires now)

| Symbol | Indicator | Slot | Sizing | Evidence |
|---|---|---|---|---|
| `BINANCE:LDOUSDT` 4H | VWAP Deviation v1.0 | `USER;53163d00de3843f1a78c67bfc88dbf6d` | `pos_size: 9.0` | PF 1.51 stable across 5 nightlies, OOS 2.23, fails IS_PF gate only |
| `BINANCE:COMPUSDT` 4H | VWAP Deviation v1.0 | (same VWAP slot) | `pos_size: 9.0` | PF 1.46 stable, OOS 1.83 |

EVM lane validated dry-run via `phase4_evm_validate.py` 2026-05-08 — OpenOcean Arbitrum routes for both UNI/ARB/LDO/COMP/AAVE/LINK confirmed.

## Batch 2 — Bull-roster alerts (Tier C now, auto-bumped to B/A on BULL_CONFIRMED)

| Symbol | Indicator | Slot | Sizing | Bull-window evidence |
|---|---|---|---|---|
| `BINANCE:SOLUSDT` 4H | Donchian + ADX v1.0 | `USER;bf538897546a48519a83e588ff562e72` | `pos_size: 9.0` | 2023-Q4 PF 1.36 |
| `BINANCE:SOLUSDT` 4H | EMA Ribbon + ADX v1.0 | `USER;c0ffe8e0dd034504a05de359eb6d41bd` | `pos_size: 9.0` | 2023-Q4 PF 1.88 |
| `BINANCE:UNIUSDT` 4H | EMA Ribbon + ADX v1.0 | (same) | `pos_size: 9.0` | 2024-Q4 PF 1.88 (Δ +1.48) |
| `BINANCE:ARBUSDT` 4H | EMA Ribbon + ADX v1.0 | (same) | `pos_size: 9.0` | 2023-Q4 PF 1.71 |
| `BINANCE:UNIUSDT` 4H | Liquidity Sweep v1.0 | `USER;12e465c59f0941d2a4fef70e58003c45` | `pos_size: 9.0` | 2024-Q4 PF 2.74 |

Skipped: FLOKI combos (no Arbitrum liquidity, deferred to Phase 5 BSC support).

## TV UI steps (per alert)

1. Switch chart to the symbol + 4H timeframe
2. Right-click chart → "Add alert"
3. Condition: select the saved Pine indicator from "My Scripts"
4. **Important**: in the alert settings, set `Position Size %` to **9.0** (overrides Pine default of 15.0)
5. Set webhook URL: `https://jarred-damoda-nonperiodically.ngrok-free.dev/webhook`
6. Set message: `{{strategy.order.alert_message}}` (or leave blank — Pine emits via `alert()`)
7. Frequency: "Once per bar close"
8. Save

## What happens after deploy

- Each alert sits live with 9% sizing (Tier C ~$22-30/trade with current Phase 6 wallet state)
- ADX gate (built into the +ADX indicators) naturally suppresses entries during chop
- When `BULL_CONFIRMED` fires (regime detector cron, 01:30 UTC daily):
  - SOL Donch+ADX stays at 9% (PF 1.36 only justifies C)
  - SOL/UNI/ARB EMA+ADX → 13% (B)
  - UNI Liq Sweep → 18% (A)
  - Telegram confirms which entries were added
- When `BULL_LOST` fires: all bull_regime entries reverted, alerts back to 9%

## Verification after deploy

```bash
# Confirm 17-19 active alerts (was 10 + 7-9 new)
mcp__tradingview__alert_list  # via Claude session

# Verify regime detector wiring
venv/bin/python -m backtesting.regime_detector --no-save
```
