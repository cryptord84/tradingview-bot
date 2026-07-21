"""Bull-regime detector.

Pulls BTC/USDT 1D, computes a 4-factor bull-regime signal, persists state,
emits Telegram on transition, and AUTO-MANAGES sizing overrides for the
bull-roster combos.

Bull regime confirmed when ALL four are true on the latest closed 1D bar:
  1. close > EMA(200)               — long-term trend up
  2. ADX(14) > 25                   — trend strength above threshold
  3. ADX rising vs 3-bar slope      — strengthening, not fading
  4. Not in Bollinger squeeze       — volatility actually expanded

State transitions trigger sizing changes (bull-roster alerts assumed
already deployed in TV at Tier C / signal-default):

  bear → bull   = "BULL CONFIRMED" → write `source: bull_regime` entries to
                  config_sizing_overrides.yaml that size up the 5 bull combos
                  to B (13%) or A (18%) per bull-window evidence.

  bull → bear   = "BULL LOST"      → remove the `source: bull_regime` entries.
                  Alerts revert to signal-default Tier C (9%).

Use:
  venv/bin/python -m backtesting.regime_detector              # current check
  venv/bin/python -m backtesting.regime_detector --force-notify   # send TG even if no transition
  venv/bin/python -m backtesting.regime_detector --apply-bull     # force-apply bull overrides
  venv/bin/python -m backtesting.regime_detector --apply-bear     # force-revert overrides

State persisted to backtesting/results/regime_state.json so transitions are
only emitted ONCE per change. Designed to run alongside nightly.py via cron
(roughly daily, after ~01:30 UTC when the 1D bar has confirmed).
"""
from __future__ import annotations

import sys, os, json, asyncio, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from dataclasses import dataclass, asdict

import pandas as pd
import yaml

from backtesting.data import fetch_binance
from backtesting.indicators import ema, adx, sma, bollinger_squeeze


STATE_FILE = "backtesting/results/regime_state.json"
SIZING_OVERRIDE_FILE = "config_sizing_overrides.yaml"
LOG_DIR = "backtesting/results"

# Bull-roster size-up entries — applied on BULL_CONFIRMED, removed on BULL_LOST.
# Strategy keys match _STRATEGY_NAME_ALIASES output in trade_engine.py.
# PFs are from backtesting/results/bull_period_20260508_1529.txt.
BULL_ROSTER_OVERRIDES = [
    {"strategy": "Donch+ADX", "token": "SOL", "tf": "4H",
     "pf": 1.36, "size_pct": 15.0, "tier": "C",
     "note": "Bull-window 2023-Q4 PF 1.36 (multi-strat SOL strength). Auto-applied on BULL_CONFIRMED."},
    {"strategy": "EMA+ADX",   "token": "SOL", "tf": "4H",
     "pf": 1.88, "size_pct": 20.0, "tier": "B",
     "note": "Bull-window 2023-Q4 PF 1.88 (Δ +0.94). Auto-applied on BULL_CONFIRMED."},
    {"strategy": "EMA+ADX",   "token": "UNI", "tf": "4H",
     "pf": 1.88, "size_pct": 20.0, "tier": "B",
     "note": "Bull-window 2024-Q4 PF 1.88 (Δ +1.48 — biggest in family). Auto-applied on BULL_CONFIRMED."},
    {"strategy": "EMA+ADX",   "token": "ARB", "tf": "4H",
     "pf": 1.71, "size_pct": 20.0, "tier": "B",
     "note": "Bull-window 2023-Q4 PF 1.71 (Δ +0.77). Auto-applied on BULL_CONFIRMED."},
    {"strategy": "Liq Sweep", "token": "UNI", "tf": "4H",
     "pf": 2.74, "size_pct": 28.0, "tier": "A",
     "note": "Bull-window 2024-Q4 PF 2.74 — highest absolute in family. Auto-applied on BULL_CONFIRMED."},
]

# Default thresholds — match _apply_regime_filter(regime="trend") in strategies.py
ADX_THRESHOLD = 25.0
ADX_SLOPE_BARS = 3
EMA_PERIOD = 200
ADX_PERIOD = 14
BB_PERIOD = 20
KC_MULT = 1.5


@dataclass
class RegimeReading:
    timestamp_utc: str            # latest 1D bar close (ISO)
    bar_date: str                 # YYYY-MM-DD of latest bar
    btc_close: float
    ema200: float
    adx: float
    adx_3bar_slope: float         # adx[0] - adx[ADX_SLOPE_BARS]
    in_squeeze: bool

    # Per-factor booleans (so we can see which factor is the blocker)
    f1_above_ema200: bool
    f2_adx_above_thresh: bool
    f3_adx_rising: bool
    f4_not_in_squeeze: bool

    bull: bool                    # all four AND'd

    def summary(self) -> str:
        marks = lambda b: "✓" if b else "✗"
        return (
            f"BTC 1D close: ${self.btc_close:,.0f}   bar={self.bar_date}\n"
            f"  {marks(self.f1_above_ema200)}  close > EMA(200)        "
            f"  close={self.btc_close:,.0f}  ema200={self.ema200:,.0f}\n"
            f"  {marks(self.f2_adx_above_thresh)}  ADX(14) > {ADX_THRESHOLD:.0f}            "
            f"  adx={self.adx:.1f}\n"
            f"  {marks(self.f3_adx_rising)}  ADX rising ({ADX_SLOPE_BARS}-bar slope > 0)"
            f"  slope={self.adx_3bar_slope:+.2f}\n"
            f"  {marks(self.f4_not_in_squeeze)}  Not in BB squeeze       "
            f"  in_squeeze={self.in_squeeze}\n"
            f"  → REGIME: {'BULL' if self.bull else 'NOT BULL'}"
        )


def compute_regime(df: pd.DataFrame) -> RegimeReading:
    """Compute regime indicators from a BTC 1D OHLCV DataFrame.

    df is expected to have columns: open, high, low, close, volume, with a
    DatetimeIndex sorted ascending. Drops the most recent bar if it's still
    open (live bar) — only uses confirmed closes.
    """
    if len(df) < EMA_PERIOD + ADX_SLOPE_BARS + 1:
        raise ValueError(
            f"Need at least {EMA_PERIOD + ADX_SLOPE_BARS + 1} bars; got {len(df)}"
        )

    # Compute indicators on full series
    ema200 = ema(df["close"], EMA_PERIOD)
    adx_series = adx(df["high"], df["low"], df["close"], ADX_PERIOD)
    squeeze = bollinger_squeeze(df["close"], BB_PERIOD, BB_PERIOD, KC_MULT)

    # Use the most recent CLOSED bar — i.e. last row of fetched data
    last = df.index[-1]
    bar_date = last.strftime("%Y-%m-%d")

    btc_close = float(df["close"].iloc[-1])
    ema200_v = float(ema200.iloc[-1])
    adx_v = float(adx_series.iloc[-1])
    adx_prior = float(adx_series.iloc[-1 - ADX_SLOPE_BARS])
    adx_slope = adx_v - adx_prior
    in_sq = bool(squeeze.iloc[-1])

    f1 = btc_close > ema200_v
    f2 = adx_v > ADX_THRESHOLD
    f3 = adx_slope > 0
    f4 = not in_sq

    return RegimeReading(
        timestamp_utc=last.isoformat(),
        bar_date=bar_date,
        btc_close=btc_close,
        ema200=ema200_v,
        adx=adx_v,
        adx_3bar_slope=adx_slope,
        in_squeeze=in_sq,
        f1_above_ema200=f1,
        f2_adx_above_thresh=f2,
        f3_adx_rising=f3,
        f4_not_in_squeeze=f4,
        bull=f1 and f2 and f3 and f4,
    )


def load_prior_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(reading: RegimeReading, transition: str | None) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    payload = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_transition": transition,
        "last_transition_at_bar": reading.bar_date if transition else None,
        "reading": asdict(reading),
    }
    # Preserve historical transition if no transition this run
    prior = load_prior_state()
    if not transition and prior:
        payload["last_transition"] = prior.get("last_transition")
        payload["last_transition_at_bar"] = prior.get("last_transition_at_bar")
    with open(STATE_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def detect_transition(prior: dict | None, current: RegimeReading) -> str | None:
    """Return 'BULL_CONFIRMED', 'BULL_LOST', or None."""
    if prior is None:
        return None  # first run — establish baseline silently
    prior_bull = prior.get("reading", {}).get("bull", False)
    if prior_bull == current.bull:
        return None
    return "BULL_CONFIRMED" if current.bull else "BULL_LOST"


def apply_bull_overrides() -> tuple[int, int]:
    """Write bull-roster size-up entries to config_sizing_overrides.yaml.

    Returns (added, already_present). Idempotent — re-running doesn't duplicate.
    """
    if not os.path.exists(SIZING_OVERRIDE_FILE):
        return (0, 0)
    with open(SIZING_OVERRIDE_FILE) as f:
        data = yaml.safe_load(f) or {}
    sizes = data.setdefault("strategy_token_tf_sizes", {})

    added = 0
    already = 0
    for ov in BULL_ROSTER_OVERRIDES:
        strat = sizes.setdefault(ov["strategy"], {})
        token = strat.setdefault(ov["token"], {})
        existing = token.get(ov["tf"])
        if existing and existing.get("source") == "bull_regime":
            already += 1
            continue
        # Don't clobber other-source entries with higher PF
        if existing and existing.get("pf", 0) > ov["pf"]:
            already += 1
            continue
        token[ov["tf"]] = {
            "pf": ov["pf"],
            "size_pct": ov["size_pct"],
            "source": "bull_regime",
            "tier": ov["tier"],
            "note": ov["note"],
        }
        added += 1

    with open(SIZING_OVERRIDE_FILE, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
    return (added, already)


def revert_bull_overrides() -> int:
    """Remove all `source: bull_regime` entries. Returns count removed."""
    if not os.path.exists(SIZING_OVERRIDE_FILE):
        return 0
    with open(SIZING_OVERRIDE_FILE) as f:
        data = yaml.safe_load(f) or {}
    sizes = data.get("strategy_token_tf_sizes") or {}
    removed = 0
    for strat in list(sizes.keys()):
        for token in list(sizes[strat].keys()):
            for tf in list(sizes[strat][token].keys()):
                entry = sizes[strat][token][tf]
                if isinstance(entry, dict) and entry.get("source") == "bull_regime":
                    del sizes[strat][token][tf]
                    removed += 1
            if not sizes[strat][token]:
                del sizes[strat][token]
        if not sizes[strat]:
            del sizes[strat]
    with open(SIZING_OVERRIDE_FILE, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
    return removed


def deploy_recommendation(transition: str, action_taken: str = "") -> str:
    """Tell the user what just happened + what's pending."""
    if transition == "BULL_CONFIRMED":
        msg = "BULL regime CONFIRMED."
        if action_taken:
            msg += f"\n{action_taken}"
        msg += (
            "\n\nThe 5 bull-roster alerts (assumed already deployed in TV at signal-default 9%) "
            "are now sized up to B/A tier per bull-window evidence:\n"
            "  • Donch+ADX/SOL/4H   → C tier 9%  (PF 1.36)\n"
            "  • EMA+ADX/SOL/4H     → B tier 13% (PF 1.88)\n"
            "  • EMA+ADX/UNI/4H     → B tier 13% (PF 1.88, EVM)\n"
            "  • EMA+ADX/ARB/4H     → B tier 13% (PF 1.71, EVM)\n"
            "  • Liq Sweep/UNI/4H   → A tier 18% (PF 2.74, EVM)\n\n"
            "If alerts are NOT yet deployed in TV, deploy them manually now."
        )
        return msg
    if transition == "BULL_LOST":
        msg = "BULL regime LOST."
        if action_taken:
            msg += f"\n{action_taken}"
        msg += (
            "\nBull-roster sizing reverted to signal-default Tier C (9%). "
            "Alerts remain active but ADX gate will naturally suppress entries in chop."
        )
        return msg
    return ""


async def send_telegram(msg: str) -> None:
    """Best-effort Telegram notification. Silently swallow errors so cron survives."""
    try:
        from app.services.telegram_service import TelegramService
        ts = TelegramService()
        await ts.send_message(msg)
    except Exception as e:
        print(f"  (telegram notify skipped: {e})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-notify", action="store_true",
                        help="Send Telegram even if no transition")
    parser.add_argument("--no-save", action="store_true",
                        help="Don't persist state (dry run)")
    parser.add_argument("--apply-bull", action="store_true",
                        help="Force-apply bull overrides (skip regime check)")
    parser.add_argument("--apply-bear", action="store_true",
                        help="Force-revert bull overrides (skip regime check)")
    args = parser.parse_args()

    # Manual override paths — for testing / out-of-band fixes
    if args.apply_bull:
        added, already = apply_bull_overrides()
        print(f"  Manual --apply-bull: added {added}, already present {already}")
        return
    if args.apply_bear:
        removed = revert_bull_overrides()
        print(f"  Manual --apply-bear: removed {removed} bull_regime entries")
        return

    print(f"\n{'='*72}")
    print(f"  REGIME DETECTOR  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
    print('='*72)

    # Need ~250 bars for warm EMA(200) + ADX history
    df = fetch_binance("BTCUSDT", "1d", bars=400)
    if df is None or len(df) < 250:
        print(f"  ERROR: insufficient BTC 1D data ({0 if df is None else len(df)} bars)")
        sys.exit(1)
    print(f"  Loaded BTC 1D: {len(df)} bars  ({df.index[0].date()} → {df.index[-1].date()})")
    print()

    reading = compute_regime(df)
    print(reading.summary())
    print()

    prior = load_prior_state()
    transition = detect_transition(prior, reading)

    action_taken = ""
    if transition == "BULL_CONFIRMED":
        added, already = apply_bull_overrides()
        action_taken = f"AUTO-APPLIED bull overrides: {added} new, {already} already in place."
        print(f"  ⚡ TRANSITION: {transition}")
        print(f"  {action_taken}")
    elif transition == "BULL_LOST":
        removed = revert_bull_overrides()
        action_taken = f"AUTO-REVERTED bull overrides: removed {removed} entries."
        print(f"  ⚡ TRANSITION: {transition}")
        print(f"  {action_taken}")
    else:
        if prior is None:
            print("  (first run — baseline established, no transition)")
        else:
            print(f"  No transition (regime stable: {'BULL' if reading.bull else 'NOT BULL'})")
    if transition:
        print()
        print(deploy_recommendation(transition, action_taken))

    # Send Telegram on transition or --force-notify
    if transition or args.force_notify:
        msg = (
            f"<b>Regime detector — {transition or 'manual check'}</b>\n"
            f"BTC 1D close: ${reading.btc_close:,.0f} (bar {reading.bar_date})\n"
            f"close>EMA200: {reading.f1_above_ema200} | "
            f"ADX>{ADX_THRESHOLD:.0f}: {reading.f2_adx_above_thresh} ({reading.adx:.1f}) | "
            f"ADX rising: {reading.f3_adx_rising} ({reading.adx_3bar_slope:+.2f}) | "
            f"not squeeze: {reading.f4_not_in_squeeze}\n"
            f"<b>Regime: {'BULL' if reading.bull else 'NOT BULL'}</b>"
        )
        if transition:
            msg += "\n\n" + deploy_recommendation(transition, action_taken)
        asyncio.run(send_telegram(msg))

    # Persist
    if not args.no_save:
        save_state(reading, transition)
        print(f"\n  state saved → {STATE_FILE}")

    # Append a one-line history record
    if not args.no_save:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = f"{LOG_DIR}/regime_history.txt"
        line = (
            f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}  "
            f"bar={reading.bar_date}  "
            f"close={reading.btc_close:,.0f}  "
            f"ema200={reading.ema200:,.0f}  "
            f"adx={reading.adx:.1f}  "
            f"slope={reading.adx_3bar_slope:+.2f}  "
            f"squeeze={reading.in_squeeze}  "
            f"bull={reading.bull}  "
            f"transition={transition or '-'}\n"
        )
        with open(log_file, "a") as f:
            f.write(line)


if __name__ == "__main__":
    main()
