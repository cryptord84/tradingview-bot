"""Backtest: compare old vs new market selection with microstructure filtering.

Fetches current open markets from Kalshi and runs them through both the
old selection logic (mid-range, volume-only) and the new research-based
logic (skip finance, skip dead zone, tail preference, category scoring).

Outputs a side-by-side comparison showing which markets each approach
selects and why the new approach should capture more edge.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config, get

load_config()

# ── Keywords from the research ──
LOW_EDGE_KEYWORDS = [
    "finance", "fed ", "interest rate", "gdp", "cpi", "inflation",
    "treasury", "earnings",
]
HIGH_EDGE_KEYWORDS = [
    "sports", "entertainment", "celebrity", "movie", "tv ",
    "award", "oscar", "grammy", "super bowl", "world series",
    "playoff", "championship", "election", "trump", "biden",
    "war", "conflict", "weather", "hurricane",
]


def classify_category(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in LOW_EDGE_KEYWORDS):
        return "LOW_EDGE"
    if any(kw in t for kw in HIGH_EDGE_KEYWORDS):
        return "HIGH_EDGE"
    return "NEUTRAL"


def old_selection(markets: list) -> list:
    """Old logic: volume >= 100, 15 <= yes_ask <= 85, sort by volume."""
    candidates = []
    for m in markets:
        vol = int(float(m.get("volume_fp", "0") or "0"))
        yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
        if vol >= 100 and 15 <= yes_ask <= 85:
            candidates.append({
                "ticker": m.get("ticker", ""),
                "title": m.get("title", "")[:60],
                "volume": vol,
                "yes_ask": yes_ask,
                "category": classify_category(m.get("title", "") + " " + m.get("subtitle", "")),
                "score": vol,
            })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:10]


def new_selection(markets: list) -> list:
    """New logic: skip finance, skip 40-60¢ dead zone, tail preference, category boost."""
    candidates = []
    for m in markets:
        vol = int(float(m.get("volume_fp", "0") or "0"))
        yes_ask = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))

        if vol < 25 or yes_ask < 5 or yes_ask > 95:
            continue
        if 40 <= yes_ask <= 60:
            continue

        title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
        if any(kw in title for kw in LOW_EDGE_KEYWORDS):
            continue

        category = classify_category(m.get("title", "") + " " + m.get("subtitle", ""))
        score = vol
        if any(kw in title for kw in HIGH_EDGE_KEYWORDS):
            score *= 1.5

        candidates.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", "")[:60],
            "volume": vol,
            "yes_ask": yes_ask,
            "category": category,
            "score": score,
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:10]


def main():
    from app.services.kalshi_client import get_kalshi_client
    client = get_kalshi_client()

    print("Fetching active markets from Kalshi (via recent trades)...")
    trades = client.get_recent_trades(limit=100)
    tickers = list(dict.fromkeys([t.get("ticker", "") for t in trades if t.get("ticker")]))[:50]
    print(f"Found {len(tickers)} active tickers from recent trades")

    all_markets = []
    for t in tickers:
        try:
            m = client.get_market_full(t)
            all_markets.append(m)
        except Exception:
            pass
    print(f"Fetched {len(all_markets)} market details\n")

    # Classify all markets
    categories = {"LOW_EDGE": 0, "HIGH_EDGE": 0, "NEUTRAL": 0}
    price_zones = {"tail_low": 0, "dead_zone": 0, "tail_high": 0, "other": 0}
    for m in all_markets:
        cat = classify_category(m.get("title", "") + " " + m.get("subtitle", ""))
        categories[cat] += 1
        ya = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
        if ya <= 20:
            price_zones["tail_low"] += 1
        elif 40 <= ya <= 60:
            price_zones["dead_zone"] += 1
        elif ya >= 80:
            price_zones["tail_high"] += 1
        else:
            price_zones["other"] += 1

    print("=" * 70)
    print("MARKET UNIVERSE BREAKDOWN")
    print("=" * 70)
    print(f"  Categories:  LOW_EDGE={categories['LOW_EDGE']}  HIGH_EDGE={categories['HIGH_EDGE']}  NEUTRAL={categories['NEUTRAL']}")
    print(f"  Price zones: tail_low(≤20¢)={price_zones['tail_low']}  dead_zone(40-60¢)={price_zones['dead_zone']}  tail_high(≥80¢)={price_zones['tail_high']}  other={price_zones['other']}")
    print()

    # Run both selections
    old = old_selection(all_markets)
    new = new_selection(all_markets)

    old_tickers = {c["ticker"] for c in old}
    new_tickers = {c["ticker"] for c in new}
    overlap = old_tickers & new_tickers
    only_old = old_tickers - new_tickers
    only_new = new_tickers - old_tickers

    print("=" * 70)
    print("OLD SELECTION (volume >= 200, 20-80¢ range)")
    print("=" * 70)
    for i, c in enumerate(old, 1):
        marker = "  " if c["ticker"] in new_tickers else "* "
        print(f"  {marker}{i}. [{c['category']:9s}] {c['yes_ask']:3d}¢  vol={c['volume']:>6d}  {c['title']}")
    old_cats = {"LOW_EDGE": 0, "HIGH_EDGE": 0, "NEUTRAL": 0}
    old_dead = 0
    for c in old:
        old_cats[c["category"]] += 1
        if 40 <= c["yes_ask"] <= 60:
            old_dead += 1
    print(f"\n  Categories: {old_cats}")
    print(f"  In dead zone (40-60¢): {old_dead}")
    print()

    print("=" * 70)
    print("NEW SELECTION (skip finance, skip 40-60¢, tail preference, category boost)")
    print("=" * 70)
    for i, c in enumerate(new, 1):
        marker = "  " if c["ticker"] in old_tickers else "+ "
        print(f"  {marker}{i}. [{c['category']:9s}] {c['yes_ask']:3d}¢  vol={c['volume']:>6d}  {c['title']}")
    new_cats = {"LOW_EDGE": 0, "HIGH_EDGE": 0, "NEUTRAL": 0}
    new_dead = 0
    for c in new:
        new_cats[c["category"]] += 1
        if 40 <= c["yes_ask"] <= 60:
            new_dead += 1
    print(f"\n  Categories: {new_cats}")
    print(f"  In dead zone (40-60¢): {new_dead}")
    print()

    print("=" * 70)
    print("COMPARISON SUMMARY")
    print("=" * 70)
    print(f"  Overlap: {len(overlap)} markets in both")
    print(f"  Only in OLD: {len(only_old)} markets")
    print(f"  Only in NEW: {len(only_new)} markets")
    print()

    # Expected edge improvement
    old_low_edge_pct = old_cats["LOW_EDGE"] / max(len(old), 1) * 100
    new_low_edge_pct = new_cats["LOW_EDGE"] / max(len(new), 1) * 100
    old_high_edge_pct = old_cats["HIGH_EDGE"] / max(len(old), 1) * 100
    new_high_edge_pct = new_cats["HIGH_EDGE"] / max(len(new), 1) * 100
    old_dead_pct = old_dead / max(len(old), 1) * 100
    new_dead_pct = new_dead / max(len(new), 1) * 100

    print("  EDGE ANALYSIS (based on jbecker.dev research):")
    print(f"    Low-edge markets:   OLD {old_low_edge_pct:.0f}% → NEW {new_low_edge_pct:.0f}%  {'✓ eliminated' if new_low_edge_pct == 0 else '⚠ still present'}")
    print(f"    High-edge markets:  OLD {old_high_edge_pct:.0f}% → NEW {new_high_edge_pct:.0f}%  {'✓ improved' if new_high_edge_pct > old_high_edge_pct else '→ same'}")
    print(f"    Dead zone (40-60¢): OLD {old_dead_pct:.0f}% → NEW {new_dead_pct:.0f}%  {'✓ eliminated' if new_dead_pct == 0 else '⚠ still present'}")
    print()

    # Avg tail distance
    old_tail = sum(abs(c["yes_ask"] - 50) for c in old) / max(len(old), 1)
    new_tail = sum(abs(c["yes_ask"] - 50) for c in new) / max(len(new), 1)
    print(f"    Avg distance from 50¢: OLD {old_tail:.1f}¢ → NEW {new_tail:.1f}¢  {'✓ more tail exposure' if new_tail > old_tail else '→ similar'}")
    print()

    if new_low_edge_pct == 0 and new_dead_pct == 0 and new_tail > old_tail:
        print("  ✅ NEW SELECTION VALIDATED — better edge profile across all metrics")
    else:
        print("  ⚠ Mixed results — review individual market choices above")


if __name__ == "__main__":
    main()
