# Decision Log

Every autonomous decision I make on this bot, logged for review. The user reads
this and pushes back on anything they'd have done differently — that feedback is
the correction loop for the autonomy granted on 2026-08-10 (widened 2026-08-11 to
remove the ask-first line entirely).

**Format** — one `##` block per decision, newest first. The nightly Telegram
digest parses these headers, so keep the `## YYYY-MM-DD — Title` shape exact.

- **What** — the change, concretely.
- **Why** — the evidence. Prefer numbers over adjectives.
- **Risk** — what could go wrong, and what bounds it.
- **Reversible** — how to undo it, or why you can't.

---

## 2026-08-20 — Built the autonomous nightly agent (closes the report-vs-act gap)

**What:** `scripts/autonomous_nightly.sh` + launchd `com.tradingbot.autonomous-nightly`,
07:15 daily. Runs `health_report.py --quiet`; if findings exist AND the finding
set changed since last run, invokes `claude -p` headless with a scoped prompt to
investigate and fix, then Telegrams the summary. First job in this system that
actually invokes Claude — everything else only reports.

**Why:** The user asked whether they were still babysitting. They were. Full
autonomy was granted 2026-08-11, but nothing ever triggered me, so it was
theoretical: the roster gap sat 9 days in the digest unchanged, price-source-audit
failed 12+ days, the whale tracker burned CPU for weeks, a signal sat stranded
4.5 hours. Every one was found only when a session was opened. Reporting is not
fixing.

**Spend control — the binding constraint is the weekly allowance.** It fires only
on a *changed* finding set. Digits are normalised first (`3 signals` and
`4 signals` hash the same) so a ticking counter cannot re-fire it. The CPU line
is excluded from the hash entirely: `ps -o %cpu` is a lifetime average on macOS
and read 89% then 20.9% seconds apart on 2026-08-20, so it flickers in and out on
its own and would have re-fired the agent most days. Verified: run 1 acts, runs
2 and 3 skip silently.

**Scope handed to the unattended agent** — bug/test fixes, observability, config
that does not raise capital at risk, bot restarts, backtests, docs. Explicitly
OUT: raising sizing tiers / max positions / leverage, deleting or deactivating
alerts, moving funds, and raising `loss_budget.max_loss_usd`. It is told to log
those to DECISIONS.md as "deferred, needs a human" instead. That is narrower than
the standing grant on purpose — the grant assumes someone can react, and at 07:15
nobody can.

**Risk:** It can edit code and restart the bot with nobody watching. Bounded by:
the out-of-scope list, the code-enforced `$25/7d` loss budget it cannot raise,
`--allowed-tools` limited to Bash/Read/Edit/Write/Grep/Glob, and a Telegram
summary every time it acts. Untested against a real finding set — the plumbing
was verified with a stubbed binary, and headless `claude -p` was confirmed
working from this environment, but its first genuine run is tomorrow 07:15.

**Reversible:** Yes — `launchctl unload com.tradingbot.autonomous-nightly`.

## 2026-08-20 — Deployed the COMP alert (roster 23 -> 24)

**What:** Created alert **`5417513312`** — `BINANCE:COMPUSDT`, resolution 240
(4H), pineId `USER;452f801743764531b38407308ff41da6` (Stoch RSI v1.1). Verified
after activation: `active: true`, 64-char webhook secret intact, `in_10=false`
(bar close, no repaints), webhook URL correct. Tier C 9% — deployed as a watch,
matching the LINK precedent.

**Why:** Stoch RSI/COMP/4H passed walk-forward on 6 of the last 7 nightlies
(PF 1.48, OOS 1.76, 35 trades) and had no live alert — it was culled 2026-05-15
for stacking. It was the one genuine deployment candidate from the roster-gap
analysis. Liquidity was verified first: $7.27 USDC -> 0.396087 COMP at **0.94%**
implied cost on Arbitrum. That check is exactly what was skipped for INJ in May,
which shipped an alert for a token with no execution lane and had to be culled.

**Method:** deep-cloned the TIA/4H Stoch RSI alert `4816316288`, stripped the
server-generated fields (`alertId`, `createTime`, `lastFireTime`,
`lastFireBarTime`, `lastStopReason`, `lastError`, `complexity`), swapped
`symbol`/`proSymbol` to COMP, then `createAlert` + `restartAlerts`. Cloning
preserves `pineId`/`pineVersion` in `conditions[0].series[0]`, side-stepping the
wrapper's field-stripping problem entirely — no interceptor needed.

**Unblocking note:** the alerts API had blocked this since 2026-08-19. Root cause
was NOT an empty collection — `coll._alerts` is a populated Map. Two schema
changes broke every lookup: the id field is **`alertId`** (not `id`), and
**`symbol` is an object** (`a.symbol.symbol`), so string matching silently
matched nothing. Module also rotated to **951706**. All recorded in
[[reference_tv_alerts_rest_api]].

**Risk:** Low. Tier C is the smallest size band; the loss budget ($25/7d) still
bounds it. COMP routes to the EVM/Arbitrum lane, which holds ~$48 USDC.

**Reversible:** Yes — deactivate or delete alert `5417513312`. Nothing else
references it.

## 2026-08-19 — Recreated the Donchian + ADX bull-roster script

**What:** New TV slot `USER;19990b656a724d1da67d8ff124d70bf3` — "Donchian + ADX
v1.1 — Alerts", 0 errors / 0 warnings, 14 inputs, `in_12` (realtimeTrig) =
false so it fires on bar close. Source saved to
`staged/indicator_donchian_adx_v1.1.pine`. **No alerts created** — script only.

**Why:** The original v1.0 slot `bf538897…` still carries the name "Donchian +
ADX v1.0" but its title is "VWAP Deviation v1.1" — it was overwritten by the
slot-overwrite bug and the indicator no longer existed. It is 1 of the 7
bull-roster combos, so the roster would have deployed with a hole in it. BTC
moved $64,369 → $69,284 today and the regime went from 1/4 to **2/4** factors
(ADX slope flipped positive; the EMA200 gap closed from +11.7% to +3.8%), so
this stopped being hypothetical.

**Version bumped v1.0 → v1.1 deliberately.** The slot name, the `indicator()`
title and the `_strat` payload string now all read v1.1. Leaving it at v1.0
would have recreated the exact name/title mismatch that made the old slot so
confusing to diagnose. `_normalize_strategy_name` strips version suffixes, so
sizing overrides still resolve.

**Method:** `saveNew()` from webpack module **957019** — the old 752174 is gone.
Signature is `saveNew({scriptSource, scriptName, allowOverwrite, currentVersion})`
POSTing to `save/new`. `allowOverwrite` was omitted, so it defaults false and
**cannot clobber an existing slot** — the safeguard that was missing when v1.0
was destroyed. No editor binding involved, so CLAUDE.md rules 2/3 do not apply.

**Risk:** Low. Verified after the write that all three slots are intact and
distinct: `bf538897…` still holds VWAP Dev v1.1, `c0ffe8e0…` still holds EMA
Ribbon + ADX v1.0, and the new slot holds the recreated script.

**Reversible:** Yes — delete the new slot; nothing references it yet.

**Still outstanding:** no bull-roster alerts exist, by design — the regime is
NOT bull (needs ADX > 25, currently 15.0, and close above EMA200). Alert
creation is also blocked on the alerts-API rotation logged in review_list.

## 2026-08-19 — Disabled Kalshi whale tracker; added job/resource checks to the health report

**What:** `kalshi.whale_tracker.enabled: false`. Added a SCHEDULED JOBS section to
`health_report.py` covering launchd exit codes, output freshness, and bot CPU/RSS.

**Why (whale tracker):** Kalshi has not traded in 30+ days with $143.18 idle, yet
this scanned 30 markets every 60s — 874 log lines and ~1900 httpx calls per 10
minutes, with the bot averaging 76% CPU over a 7-day run. It feeds a dashboard
panel and a dry-run gate for a lane that is not running. After the change: 3
whale lines per restart window.

**Why (health checks):** the user asked why the nightly checks had not surfaced
this. They hadn't because the report only ever inspected trading DATA — P&L,
positions, signals — and never the machine or the jobs producing that data. Two
things were consequently invisible for weeks: this CPU burn, and
`com.tradingbot.price-source-audit` exiting 1 with 4-6 failing checks **every day
for 12+ days**. Nothing read launchd exit codes. The new section catches both,
and flags job output that goes stale.

**Risk:** Losing whale data breaks the whale-gate backtest idea in review_list,
which needs a rolling history. Accepted — that idea is gated behind a Kalshi
restart that has no date. Re-enable together with any Kalshi revival.

**Reversible:** Yes — `enabled: true`.

**Also established (no change made):** the "no live combo is a WF passer" finding
is a CONVERSION problem, not a deployment one. VWAP Dev/LDO/1D — the most stable
passer, 7 of 7 nightlies, PF 2.01, OOS 3.58 — is already deployed and firing on
alert `4665962153`. It has produced exactly 2 BUYs in 3 months and **both died to
infrastructure**: 2026-06-05 `rejected_min_size` ($2.37 < $3.00, lane starved)
and 2026-08-06 `failed_engine_error` (the blank-reason network timeout). Both
causes are now fixed — the lane holds $48.49 after the orphan sweep, sizing LDO
at $9.70 against a $5 floor, and retry_async landed 08-12. The remaining
constraint is signal rarity on a 1D timeframe, not plumbing. Stoch RSI/COMP/4H
(6 of 7) has no live alert — culled 2026-05-15 — and is the one real deployment
candidate.

## 2026-08-19 — Fixed a race that stranded signals in the queue

**What:** `SignalQueue._drain_after` now re-arms in a `finally` if anything
landed while it was processing. Added a regression test that fails on the old
code and passes on the new.

**Why:** A signal arriving *during* a drain was orphaned indefinitely.
`enqueue()` only schedules a drain when `self._drain_task.done()`, but the task
is not done until the whole batch is processed — ~45s+ per signal once a Claude
decision and a swap are involved. The drain had already snapshotted and cleared
the queue, so the new arrival had nobody to collect it. Caught live: a PNUT
CLOSE drained at 12:00:33, a RENDER BUY arrived at 12:00:40, and it was still
`disposition='received'` **4.5 hours later**. It would have sat there until some
unrelated webhook happened to arrive and sweep it up.

Silent, and it loses trades — the stranded signal never reaches the engine, so
none of the existing rejection/disposition paths ever report it.

**Risk:** Low. The re-arm is in a `finally`, so a crash mid-batch cannot strand
the remainder either. Worst case is one extra drain cycle on an empty queue,
which returns immediately.

**Reversible:** Yes — drop the `finally` block.

**Not carried over:** the stranded RENDER signal itself was 4.5h stale by the
time it was found; the restart's startup sweep marked it `dropped_restart`
(accurate). Executing a stale breakout signal at a moved price would have been
worse than dropping it.

## 2026-08-16 — Swept Binance + Solana orphans (+~$43.3 recovered)

**What:** Added `scripts/sweep_orphans.py` (dry-run default) and ran it. Binance:
sold 395.0 surplus ARB (order 125435582) and 46.29 OP (order 113506944).
USDT 163.13 → **195.88, +$32.75**. Solana: PNUT + BONK also sold, ~$10.59 (see correction below). **Total recovered ~$43.3.**

**Why:** Same class as the EVM orphans, but bigger. The wallet held 502.58 ARB
while open position #84 accounted for only 107.5 — so the surplus had no TP, no
SL and no exit path. BONK's 1,139,635 tokens match abandoned position #60's
recorded 1,139,641 almost exactly: marked abandoned, never sold.

**The important subtlety: orphans can be PARTIAL.** Treating "has an open
position" as "don't touch" (what `sell_evm_orphans.py` does) would have missed
the $29 of ARB entirely; selling the whole balance would have liquidated a live
position. The sweep computes surplus = on-chain − sum(open position qty) per
asset and sells only that. Verified after: ARB 502.58 → 107.58, position #84's
107.5 intact.

**Solana — CORRECTED 2026-08-16.** Originally logged here as failed with Jupiter
error `0x1788`. **That was wrong: both swaps succeeded.** PNUT
(`5G49tA9Zy4q5u14hB2UmFKZLXSwUru5Y7i924ioHShRgCeNfLqU8SVpZ8muRsbiShnaZJAGE`) and
BONK (`bqod9PZiBUWiRBYAitun37v7dQBhTn4tGeGoULsqGks2gv8ipysbTbZxLWJZ4PhMcX7p7AsA`)
both landed at 22:55:13 and are in `wallet_transactions` (ids 353/354). On-chain
balances for both mints are now 0. **~$10.59 recovered, not stranded** — total
across both lanes is ~$43.3, not $32.75.

Two errors compounded into the false report:
1. `sweep_orphans.py` probed `res.get("success")`, but `execute_swap` returns
   `{tx_signature, output_amount, ...}` and RAISES on failure. A completed swap
   printed `FAILED: None`. Fixed — it now reads `tx_signature`.
2. The follow-up balance check read before the RPC reflected the swap, so
   "balances unchanged" appeared to confirm the failure. **Verification of an
   on-chain write must be a fresh read after settlement, not an immediate one.**

The `0x1788` (6024) errors were self-inflicted: retrying a swap whose source
account was already emptied. That is the diagnosis — **empty/insufficient source
account**, consistent with failing after only 1324 of 1,399,700 compute units
(an account-validation bail, not a swap-execution failure). Slippage was never
involved, which is why 100/500/1500 bps all behaved identically. No Jupiter bug,
and nothing here threatens a live Solana close.

**Risk:** Low. Sold at market; realized proceeds were within 1.4% of the quoted
~$33.23, consistent with market-order slippage.

**Reversible:** No — trades are final. These were untracked strays.

**Left alone deliberately:** ATOM $0.01, FLOKI ~$0, TIA ~$0, FARTCOIN $0.11,
PENGU $0.08 — all under the $1 floor and under venue min-notional. Unsellable;
attempting only burns fees on rejected orders.

## 2026-08-12 — Retry transient network failures in the price path

**What:** Added `app/utils/retry.py::retry_async` (3 attempts, 0.4s exponential
backoff, ~1.2s worst case). Applied to `jupiter_client.get_sol_price` (both
Binance and CoinGecko sources) and `get_token_price` (Jupiter quote + CoinGecko
fallback). Retries transient transport faults and 5xx only — 4xx, ValueError
and KeyError raise immediately, verified by test.

**Why:** A single CoinGecko `ConnectTimeout` discarded a whole BUY signal at
04:01 on 08-12. The traceback (visible only because of the 08-11 `describe()` +
`exc_info` work) pinned it exactly: `trade_engine.py:713` →
`jupiter_client.py:241` → `httpx.ConnectTimeout`. `get_sol_price` already fell
back Binance → CoinGecko, but *neither source was retried*, so one blip on each
killed the signal. Two sources is not the same as resilience. This also
surfaced as the smoke test's `lane_sizing[SOLUSDT]` failure — same event — and
is the same failure class that killed the 08-06 LDO signal, our best
walk-forward combo. Kamino logged 92 timeouts in 24h, so the upstream flakiness
is real and ongoing.

**Risk:** Adds up to ~1.2s latency to a signal when the network is failing;
nil on the happy path (measured 0.24s for SOL). A retry cannot mask a bad
price — each attempt re-fetches and the final failure still propagates, so
existing error handling is unchanged.

**Deliberately NOT applied** to `price_router.get_monitor_price`, the TP/SL
price source (CLAUDE.md rule 8). It already has a WS → Binance-REST fallback
chain and polls every 30s, so a single miss is cheap, whereas a signal is
one-shot. Not changing the most safety-critical path without a demonstrated
failure there.

**Reversible:** Yes — the `retry_async` wrappers are a thin layer; unwrap to
restore the direct calls.

## 2026-08-11 — Sold $26.67 of orphaned EVM tokens on Arbitrum

**What:** Sold the entire untracked LDO (15.782116) and AAVE (0.250734) balances
to USDC. `0x78b2f2f5…` → 4.610295 USDC, `0x794262d3…` → 22.063702 USDC, both
status `0x1`. EVM USDC $21.82 → $48.49. Added `scripts/sell_evm_orphans.py`
(dry-run default, refuses any symbol with an open position).

**Why:** Neither had an open `positions` row, so `position_monitor` could not see
them — no TP, no SL, no exit path. Both were left by the 2026-05-30 DB-corruption
recovery, which marked positions #4/#8 `abandoned` without ever selling the
tokens. AAVE at $88.34 was already **above** the $87.26 take-profit that position
#8 was created to enforce, so being untracked had cost a completed exit.

**Risk:** Low. Sold at market with 3% slippage tolerance; realized ~$26.67 into
working capital. Both quotes matched expectation within a cent.

**Reversible:** No — on-chain swaps are final. The positions were untracked
strays, not part of any strategy, so nothing downstream depends on holding them.

## 2026-08-11 — Fixed float→wei precision bug in the EVM position-close path

**What:** `position_monitor._close_position_evm` computed `sell_wei = int(float *
10**decimals)`. Now reads the exact integer via a new
`EVMWalletService.get_erc20_balance_wei()` and does the min() in wei.

**Why:** float64 can't hold 18 significant digits. Measured against the real
2026-08-11 balances, the old path asked for **+948 wei** more LDO and **+25 wei**
more AAVE than the wallet held → `transferFrom` reverts "transfer amount exceeds
balance" → **the position cannot exit at all**. Same defect as the 2026-05-30
full-balance sell bug; that fix landed in `evm_swap_executor` but never here.

**Risk:** Reduces risk. Failure mode was a stuck, unexitable EVM position.

**Reversible:** Yes — small, contained diff.

## 2026-08-11 — Exits no longer blocked by a Claude outage

**What:** In `claude_decision.py`, CLOSE/SELL now bypass the confidence gate when
Claude is *unavailable* (timeout / non-zero exit). An explicit REJECT Claude
actually rendered is still honoured, and entries keep their confidence bar.

**Why:** Pine stamps every CLOSE at confidence 50 (all 192 on record) while BUYs
carry 65-70. The 08-10 threshold of 60 sat exactly between them, so the fix
restored entries during an outage but left exits rejected — the bot could open
positions it could not close. 35 CLOSEs blocked all-time; 2 real exits (BTC, ETH)
lost on 08-11 before this was caught.

**Risk:** An exit executes during an outage without a risk opinion. Acceptable:
exits are risk-reducing by construction, and the alternative is stranding
positions during exactly the conditions where exiting matters.

**Reversible:** Yes — delete the `is_exit` branch.

## 2026-08-11 — Exceptions with empty messages now render their type

**What:** Added `app/utils/errors.py::describe()`; applied at the engine-error
site (plus `exc_info=True`) and both Kamino sites.

**Why:** The whole timeout family (`asyncio.TimeoutError`, `TimeoutError`,
`httpx.ReadTimeout`/`ConnectTimeout`) has an empty `str()`, so every
`f"...: {e}"` rendered a timeout as nothing. 64 blank-error lines in one bot.log.
It destroyed the diagnosis of the 08-06 LDO `failed_engine_error` — our
best-validated strategy failed and left a blank reason, then the logs rotated.

**Risk:** None — logging only.

**Reversible:** Yes.

## 2026-08-10 — Rolling loss budget: $25 / 7 days, all lanes

**What:** New `app/services/loss_budget.py`, gating entries in `trade_engine`.
Blocks new BUYs when rolling 7d realized P&L across all lanes hits -$25. Exits
are never blocked. Stateless — recomputed from the DB per call.

**Why:** This is the bound on autonomous sizing authority. It has to be in code:
an agent that only exists when invoked cannot itself be the limit. Existing
breakers were daily-only, Solana-only, and in-memory (so any restart cleared
them — and I restart routinely).

**Risk:** $25 is **my** calibration, not the user's. Worst week on record is
-$9.62, so it's ~2.6x that and ~5.5% of ~$450 tradeable. Too loose and it never
binds; too tight and it becomes a permanent brake. Untested under real
conditions — it has never actually tripped in production.

**Reversible:** Yes — `loss_budget.enabled: false`.

## 2026-08-10 — Claude gate stops failing closed on infra errors

**What:** Bypass threshold moved from a hardcoded 80 to config-driven
`unavailable_bypass_confidence: 60`, and outages are now distinguished from
genuine rejects and from misconfiguration.

**Why:** Pine never emits above 70, so the 80 bypass could never fire — every CLI
hiccup silently killed a trade. 31 such kills Jun–Jul, at which point infra
failures outnumbered genuine rejects 31:15.

**Risk:** Trades execute during an outage without a risk opinion. Bounded by the
confidence floor and the loss budget.

**Reversible:** Yes — raise the config value above 70 to restore fail-closed.

## 2026-08-10 — Daily Intelligence Telegram disabled; smoke test kept and repaired

**What:** `scout.enabled: false`. Separately, rewrote the smoke test's tier
assertions to resolve from `nightly.SIZING_TIERS` instead of a hardcoded copy.

**Why:** The scout sent daily whether or not it found anything. The smoke test
was *not* noise — it had failed nightly since 07-22 because the 07-21 tier bump
left a stale duplicate of the numbers. 20 days of false FAILs had trained
everyone to ignore a real alert channel. Now 23/23 and silent on pass.

**Risk:** Low.

**Reversible:** Yes — `scout.enabled: true`.
