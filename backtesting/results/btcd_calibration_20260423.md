## BTCD Paper-Trade Calibration Report — 48h Burn-in

**Window:** 2026-04-22 01:19 UTC → 2026-04-23 23:42 UTC (~46h)
**Source:** `data/kalshi_strikes_calibration.jsonl`
**Settlements:** fetched via `/trade-api/v2/markets/{ticker}` for each logged ticker

### 1. Volume
- 106,610 prediction rows
- 7,170 unique tickers (all KXBTCD series)
- Of those, 7,120 past close_time → 6,932 finalized (YES/NO), 188 still pending

### 2. Brier Scores
| Scope | N | Brier |
|---|---|---|
| First prediction per ticker (decision-time proxy, all 6932) | 6,932 | **0.0117** |
| Final prediction per ticker (last pre-close) | 6,932 | 0.0022 |
| Signal-time fair_prob on WOULD-BUY only | 423 | **0.1049** |
| Baseline (predict p_yes = 0.533 on all) | 6,932 | 0.2489 |

All three well below the 0.20 threshold. **But Brier is dominated by the tails** (94% of predictions sit in 0–10% or 90–100% buckets where outcome is near-deterministic by close).

### 3. Calibration Bins — FIRST prediction per ticker (all tickers)
No bin with N≥20 off by >15pp. Mild overconfidence (5–13pp) in 50–90% range, directionally consistent.

### 4. Calibration Bins — SIGNAL-TIME (only when bot fired WOULD-BUY)
| Bin | N | MeanPred | ActualYES | Diff | Flag |
|---|---|---|---|---|---|
| 0–10% | 5 | 9.5% | 0.0% | −9.5pp | |
| 10–20% | 93 | 15.5% | 4.3% | −11.2pp | |
| **20–30%** | **85** | **24.5%** | **4.7%** | **−19.7pp** | ⚠ |
| 30–40% | 50 | 34.6% | 36.0% | +1.4pp | |
| **40–50%** | **22** | **44.1%** | **22.7%** | **−21.4pp** | ⚠ |
| 50–60% | 12 | 56.2% | 58.3% | +2.2pp | |
| 60–70% | 19 | 64.3% | 63.2% | −1.1pp | |
| 70–80% | 27 | 75.3% | 77.8% | +2.5pp | |
| 80–90% | 38 | 85.2% | 89.5% | +4.2pp | |
| 90–100% | 72 | 95.3% | 100.0% | +4.7pp | |

### 5. Simulated P&L by Bin (1 contract per signal @ ask)
| Bin | N | Win% | Staked (¢) | P&L (¢) | ROI |
|---|---|---|---|---|---|
| 0–10% | 5 | 0% | 19 | −19 | −100% |
| 10–20% | 93 | 4.3% | 747 | **−347** | **−46.5%** |
| 20–30% | 85 | 4.7% | 1272 | **−872** | **−68.6%** |
| 30–40% | 50 | 36.0% | 1257 | +543 | +43.2% |
| 40–50% | 22 | 22.7% | 714 | **−214** | **−30.0%** |
| 50–60% | 12 | 58.3% | 552 | +148 | +26.8% |
| 60–70% | 19 | 63.2% | 1009 | +191 | +18.9% |
| 70–80% | 27 | 77.8% | 1775 | +325 | +18.3% |
| 80–90% | 38 | 89.5% | 2880 | +520 | +18.1% |
| 90–100% | 72 | 100% | 6303 | +897 | +14.2% |
| **TOTAL** | **423** | **41.8%** | **16,528** | **+1,172** | **+7.1%** |

### 6. Dry-Run Signals Fired
**447** `[DRY-RUN] WOULD BUY` log lines across bot.log + bot.log.1.
(423 had resolved outcomes, 24 still pending.)

### Diagnosis

Positive aggregate ROI (+7.1%) **masks a systematic overprediction bias on OTM YES strikes**. The 10–50% predicted-prob region — 61% of all WOULD-BUY signals — loses −$14.52 net. The model pays the 80–100% zone's winnings back out as OTM losses.

Root causes to investigate:
1. Annual vol input may be too high for short-dated (<24h) strikes → inflates OTM tail
2. Fair-prob calculation likely doesn't account for volatility skew (OTM calls trade richer in real option markets than Black-Scholes symmetric fair-prob suggests)
3. Edge threshold gating is firing at the wrong tail: when ask is 10–20c, even a small model over-estimate looks like huge % edge

### Recommendation: **REVISE model**

Reason: systematic bias detected (2 bins >15pp with N≥85+22 = 107 flagged signals), and those biased zones carry real money loss (−$10.86 combined).

Options:
- **(a) Revise**: refit vol or add skew correction, then re-paper-trade 48h
- **(b) Hotfix**: gate WOULD-BUY to fair_prob ≥ 0.50 only (keeps the profitable tail, drops the losing OTM zone). Reruns of this 48h sample with that gate → est. +$20.81 on 168 bets, 78% hit rate, ROI ~+15%.

Do **not** go live until either (a) or (b) is applied. N=423 is sufficient to reject the current configuration — not to promote it.
