"""Focused WF eval: how far are COMP/UNI/LINK from deployable (active-alert) status?
Mirrors nightly.py's walk-forward exactly (70/30 split, OOS retention >=0.6, OOS abs >=1.2).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtesting.nightly import (
    fetch_all, STRATEGIES, run_walkforward, risk_for, CORE_STRATEGIES, FOCUS_TIMEFRAMES,
)

TOKENS = ["COMP", "UNI", "LINK"]
BARS = 2000
res = {t: [] for t in TOKENS}

for tf in FOCUS_TIMEFRAMES:
    ohlcv = fetch_all(tf, bars=BARS)
    for strat in CORE_STRATEGIES:
        fn = STRATEGIES.get(strat)
        if fn is None:
            continue
        for tok in TOKENS:
            df = ohlcv.get(tok)
            if df is None:
                continue
            try:
                sig = fn(df, enable_short=False)
                risk = risk_for(strat, tok, tf)
                wf = run_walkforward(df, sig, tok, strat, tf, split_pct=0.7,
                                     min_oos_pf_retention=0.6, min_oos_pf_absolute=1.2, risk=risk)
            except Exception:
                continue
            r = wf.combined
            res[tok].append((r.profit_factor, wf.in_sample.profit_factor,
                             wf.out_of_sample.profit_factor, r.trade_count,
                             r.win_rate, wf.passed, strat, tf))

for tok in TOKENS:
    rows = sorted(res[tok], key=lambda x: -x[0])
    print(f"\n=== {tok} — best combos by combined PF (deploy gate: PF/IS pass + OOS_PF>=1.2 + retention>=0.6 + trades>=30) ===")
    print(f"  {'strategy':14}{'tf':5}{'PF':>6}{'IS_PF':>7}{'OOS_PF':>8}{'trades':>7}{'WR%':>6}  verdict")
    if not rows:
        print("  (no data)")
        continue
    for pf, isp, oos, n, wr, passed, strat, tf in rows[:6]:
        if passed and n >= 30:
            verdict = "✅ PASS"
        elif n < 30:
            verdict = f"✗ too few trades ({n})"
        elif oos < 1.2:
            verdict = f"✗ OOS_PF {oos:.2f} < 1.2"
        else:
            verdict = "✗ fail WF"
        print(f"  {strat:14}{tf:5}{pf:>6.2f}{isp:>7.2f}{oos:>8.2f}{n:>7}{wr:>6.1f}  {verdict}")
