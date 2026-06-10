"""Candidate stock/ETF universe for the planned Alpaca execution lane.

v1 — LOCKED 2026-05-30 (29 symbols). This is the **input** to the backtest — the curated
candidate set (Layer 1: liquidity + indicator-fit filtered). Walk-forward then
decides which actually deploy, exactly like the crypto `FOCUS_TOKENS → nightly
→ WF passers → Tier-C deploy` flow. We never deploy on a hunch; the WF gate
(IS passes + OOS_PF ≥ 1.2 + retention ≥ 0.6 + ≥30 trades) is the filter.

Selection criteria (Layer 1):
  - Liquid: price > $5, deep average daily dollar volume, tight spreads
    (thin names = slippage eats the edge — the memecoin lesson, worse for stocks)
  - Indicator fit: our indicators are mean-reversion (Stoch RSI, VWAP Dev) →
    favor ETFs that oscillate around a mean over single names that gap on
    idiosyncratic news / earnings
  - Enough history to clear the ≥30-trade WF threshold
  - Traded on 1D (PDT rule forces swing; also where the edge lives)

Field meanings:
  corr_group : feeds the correlation caps so we don't deploy five flavors of the
               same bet (QQQ + TQQQ + XLK + NVDA are ~one tech trade).
  tier       : 1 = deepest liquidity / cleanest MR (deploy-first)
               2 = good, slightly higher vol or single-name earnings risk
               3 = leveraged decay / very high vol — Tier-C sizing + extra scrutiny
  earnings   : True = single name with ~quarterly gap risk (stops can be jumped)

Intentionally EXCLUDED (and why): penny / sub-$5 stocks, low-ADV names (slippage),
recent IPOs without WF-length history, meme/short-squeeze names (un-modelable
gaps), inverse ETFs (-1x/-3x — long-only indicators can't use them).
"""

# symbol -> metadata. Symbol IS the Alpaca symbol (no exchange prefix needed).
STOCK_UNIVERSE = {
    # ── Broad index ETFs — cleanest mean-reversion, deepest liquidity ────────
    "SPY":  {"category": "broad_index", "corr_group": "US_BROAD", "tier": 1, "earnings": False, "note": "S&P 500 — most liquid ETF in the world"},
    "QQQ":  {"category": "broad_index", "corr_group": "US_TECH",  "tier": 1, "earnings": False, "note": "Nasdaq 100 — tech-heavy"},
    "IWM":  {"category": "broad_index", "corr_group": "US_SMALL", "tier": 1, "earnings": False, "note": "Russell 2000 small-cap — higher vol, different factor"},
    "DIA":  {"category": "broad_index", "corr_group": "US_BROAD", "tier": 1, "earnings": False, "note": "Dow 30 — lower vol, value tilt"},

    # ── Sector ETFs — sector rotation creates clean mean-reversion ───────────
    "XLK":  {"category": "sector", "corr_group": "US_TECH",    "tier": 1, "earnings": False, "note": "Technology sector"},
    "SMH":  {"category": "sector", "corr_group": "SEMIS",      "tier": 1, "earnings": False, "note": "Semiconductors — high vol, sharp reversals"},
    "XLE":  {"category": "sector", "corr_group": "ENERGY",     "tier": 1, "earnings": False, "note": "Energy — commodity-driven, mean-reverts well"},
    "XLF":  {"category": "sector", "corr_group": "FINANCIALS", "tier": 1, "earnings": False, "note": "Financials"},
    "XLV":  {"category": "sector", "corr_group": "DEFENSIVE",  "tier": 1, "earnings": False, "note": "Healthcare — defensive, steady"},
    "XLI":  {"category": "sector", "corr_group": "CYCLICAL",   "tier": 1, "earnings": False, "note": "Industrials"},
    "XLU":  {"category": "sector", "corr_group": "DEFENSIVE",  "tier": 2, "earnings": False, "note": "Utilities — low vol, fewer signals"},
    "XBI":  {"category": "sector", "corr_group": "BIOTECH",    "tier": 2, "earnings": False, "note": "Biotech — high vol, strong mean-reversion"},
    "GDX":  {"category": "sector", "corr_group": "GOLD",       "tier": 2, "earnings": False, "note": "Gold miners — volatile commodity proxy"},

    # ── Leveraged index ETFs — biggest moves (clear the fee floor) but decay ─
    # Tier 3: volatility decay erodes buy-and-hold; mean-reversion CAN work but
    # needs WF proof + Tier-C sizing. Each is ~3× its underlying = same bet.
    "TQQQ": {"category": "leveraged", "corr_group": "US_TECH",  "tier": 3, "earnings": False, "note": "3× Nasdaq 100 — amplified QQQ"},
    "SOXL": {"category": "leveraged", "corr_group": "SEMIS",    "tier": 3, "earnings": False, "note": "3× semiconductors — amplified SMH"},
    "SPXL": {"category": "leveraged", "corr_group": "US_BROAD", "tier": 3, "earnings": False, "note": "3× S&P 500"},

    # ── Mega-cap single names — liquid, but ~quarterly earnings-gap risk ──────
    "AAPL": {"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Apple"},
    "MSFT": {"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Microsoft"},
    "NVDA": {"category": "mega_cap", "corr_group": "SEMIS",   "tier": 2, "earnings": True, "note": "Nvidia — very high vol"},
    "AMZN": {"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Amazon"},
    "GOOGL":{"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Alphabet"},
    "META": {"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Meta"},
    "TSLA": {"category": "mega_cap", "corr_group": "US_TECH", "tier": 2, "earnings": True, "note": "Tesla — highest single-name vol"},
    "AMD":  {"category": "mega_cap", "corr_group": "SEMIS",   "tier": 2, "earnings": True, "note": "AMD"},

    # ── Commodity / rate diversifiers — low equity correlation, own drivers ──
    "GLD":  {"category": "commodity", "corr_group": "GOLD",   "tier": 1, "earnings": False, "note": "Gold — liquid, mean-reverts, uncorrelated to stocks"},
    "SLV":  {"category": "commodity", "corr_group": "GOLD",   "tier": 2, "earnings": False, "note": "Silver — higher vol than gold"},
    "USO":  {"category": "commodity", "corr_group": "ENERGY", "tier": 2, "earnings": False, "note": "Oil — volatile, contango drag"},
    "TLT":  {"category": "rates",     "corr_group": "RATES",  "tier": 1, "earnings": False, "note": "20yr Treasuries — rate-driven, low equity corr, mean-reverts"},
    "UNG":  {"category": "commodity", "corr_group": "ENERGY", "tier": 3, "earnings": False, "note": "Natural gas — extreme vol + contango decay"},
}

# Correlation groups → for the cap that stops us deploying many flavors of one bet.
# US_TECH is the dominant cluster (QQQ/XLK/TQQQ + mega-cap tech) — cap it hardest.
CORR_GROUPS = {
    "US_BROAD":   ["SPY", "DIA", "SPXL"],
    "US_TECH":    ["QQQ", "XLK", "TQQQ", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA"],
    "US_SMALL":   ["IWM"],
    "SEMIS":      ["SMH", "SOXL", "NVDA", "AMD"],
    "ENERGY":     ["XLE", "USO", "UNG"],
    "FINANCIALS": ["XLF"],
    "DEFENSIVE":  ["XLV", "XLU"],
    "CYCLICAL":   ["XLI"],
    "BIOTECH":    ["XBI"],
    "GOLD":       ["GDX", "GLD", "SLV"],
    "RATES":      ["TLT"],
}

# Convenience views for the backtest matrix / staged rollout.
TIER_1 = [s for s, m in STOCK_UNIVERSE.items() if m["tier"] == 1]   # deploy-first
ETFS_ONLY = [s for s, m in STOCK_UNIVERSE.items() if not m["earnings"]]  # no gap risk

if __name__ == "__main__":
    from collections import Counter
    print(f"Candidate universe: {len(STOCK_UNIVERSE)} symbols")
    print("by category:", dict(Counter(m["category"] for m in STOCK_UNIVERSE.values())))
    print("by tier    :", dict(Counter(m["tier"] for m in STOCK_UNIVERSE.values())))
    print(f"ETFs (no earnings gap): {len(ETFS_ONLY)}  |  Tier-1 deploy-first: {len(TIER_1)}")
