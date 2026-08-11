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
