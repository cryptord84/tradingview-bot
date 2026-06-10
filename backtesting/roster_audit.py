"""Promote/cull audit of the 20 live alerts. Fresh WF + cross-ref vs the last
two nightly passer sets (so culls need >1 bad sample). 2026-05-30."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtesting.nightly import fetch_all, STRATEGIES, run_walkforward, risk_for

# (strategy, token, tf) — .P symbols map to base token in backtest
DEPLOYED = [
    ("FVG","NEAR","1D"),
    ("Stoch RSI","ETH","1D"), ("Stoch RSI","ARB","1D"), ("Stoch RSI","OP","1D"),
    ("Stoch RSI","FARTCOIN","4H"), ("Stoch RSI","TIA","4H"), ("Stoch RSI","LINK","1D"),
    ("EMA Ribbon","BTC","1D"), ("EMA Ribbon","SOL","1D"), ("EMA Ribbon","BONK","4H"),
    ("VWAP Dev","ARB","1D"), ("VWAP Dev","AAVE","1D"), ("VWAP Dev","LDO","1D"),
    ("VWAP Dev","PNUT","4H"), ("VWAP Dev","MOODENG","4H"), ("VWAP Dev","FARTCOIN","4H"),
    ("Donchian","DOGE","1D"), ("Donchian","ETH","1D"), ("Donchian","BTC","1D"),
    ("Donchian","RENDER","4H"),
]
N29 = {("Stoch RSI","FARTCOIN","4H"),("VWAP Dev","ARB","1D"),("VWAP Dev","MOODENG","4H"),
       ("VWAP Dev","LDO","1D"),("Donchian","BTC","1D"),("Donchian","DOGE","1D"),
       ("Stoch RSI","OP","1D"),("EMA Ribbon","SOL","1D"),("Stoch RSI","TIA","4H"),
       ("Stoch RSI","ARB","1D"),("FVG","NEAR","1D")}
N28 = N29 - {("Stoch RSI","ARB","1D")}
LIVE = {("Donchian","RENDER","4H"),("VWAP Dev","PNUT","4H"),("EMA Ribbon","BONK","4H")}

data = {tf: fetch_all(tf, bars=2000) for tf in ("4H","1D")}
rows = []
for strat, tok, tf in DEPLOYED:
    df = data[tf].get(tok)
    fresh_pass = False; pf=isp=oos=0.0; n=0
    if df is not None and strat in STRATEGIES:
        try:
            sig = STRATEGIES[strat](df, enable_short=False)
            wf = run_walkforward(df, sig, tok, strat, tf, split_pct=0.7,
                                 min_oos_pf_retention=0.6, min_oos_pf_absolute=1.2,
                                 risk=risk_for(strat, tok, tf))
            r = wf.combined
            fresh_pass, pf, isp, oos, n = wf.passed, r.profit_factor, wf.in_sample.profit_factor, wf.out_of_sample.profit_factor, r.trade_count
        except Exception as e:
            pf = -1
    nightly = (("•" if (strat,tok,tf) in N28 else " ") + ("•" if (strat,tok,tf) in N29 else " "))
    nc = nightly.count("•")
    if (strat,tok,tf) in LIVE: verdict = "WATCH-live"
    elif (strat,tok,tf)==("Stoch RSI","LINK","1D"): verdict = "WATCH-new"
    elif fresh_pass and n>=30: verdict = "KEEP"
    elif nc>=1: verdict = "KEEP(nightly)"
    else: verdict = "❌ CULL"
    rows.append((verdict, strat, tok, tf, pf, isp, oos, n, nightly))

order = {"❌ CULL":0,"WATCH-live":1,"WATCH-new":2,"KEEP(nightly)":3,"KEEP":4}
rows.sort(key=lambda x: (order.get(x[0],9), -x[4]))
print(f"  {'verdict':14}{'strategy':12}{'token':9}{'tf':4}{'PF':>6}{'IS':>6}{'OOS':>6}{'trades':>7}  nightly(28/29)")
for v,s,t,tf,pf,isp,oos,n,ng in rows:
    print(f"  {v:14}{s:12}{t:9}{tf:4}{pf:>6.2f}{isp:>6.2f}{oos:>6.2f}{n:>7}   [{ng}]")
