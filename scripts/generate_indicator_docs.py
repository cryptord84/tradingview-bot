"""Generate per-indicator PDF documentation from Pine source files.

Output: static/docs/indicators/*.pdf — served directly via FastAPI static mount
and linked from static/cheatsheet.html.

Run:  venv/bin/python scripts/generate_indicator_docs.py

Sanitization: webhook secret placeholders are kept as "CHANGE_ME" (already safe).
No other secrets exist in Pine source. Webhook URL is never in Pine — it's
configured in the TV alert UI, not the script.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    KeepTogether, Preformatted,
)

ROOT = Path(__file__).resolve().parents[1]
PINE_DIR = ROOT / "Indicators" / "staged"
OUT_DIR = ROOT / "static" / "docs" / "indicators"

# Per-indicator narrative metadata. The Pine header comments already contain
# the backtest deployment matrix — we pull that automatically. Everything below
# describes the *why* of the strategy, which can't be inferred from code alone.
INDICATORS = [
    {
        "file": "indicator_ema_ribbon_v1.0.pine",
        "slug": "ema_ribbon",
        "title": "EMA Ribbon",
        "version": "v1.0",
        "category": "Trend-Following",
        "overlay": True,
        "purpose": (
            "Captures sustained directional moves by requiring four exponential "
            "moving averages (3, 8, 21, 55 periods) to stack in strict order. "
            "Alignment is a classic momentum confirmation: when the fastest EMA "
            "is above the next, which is above the next, and so on, price is in "
            "a persistent trend with minimal whipsaw risk."
        ),
        "market_regime": (
            "Best in trending markets with moderate-to-high volatility. "
            "Underperforms in sideways chop where EMAs cluster and the "
            "alignment condition flips rapidly. The 55-period anchor is the "
            "key filter — it keeps short-term noise from triggering false entries."
        ),
        "entry_logic": (
            "BUY fires on the FIRST bar where all four EMAs become bullish-aligned "
            "(EMA3 > EMA8 > EMA21 > EMA55). Requires the previous bar to NOT be "
            "aligned, so each uptrend produces exactly one entry signal."
        ),
        "exit_logic": (
            "CLOSE fires on the first bar where bullish alignment breaks "
            "(any of the ordering conditions fails). This typically happens "
            "when the fast EMA rolls over before slower ones, signaling "
            "momentum loss before price breaks structure."
        ),
        "notes": (
            "Top-performing strategy in the current backtest matrix — produces "
            "the 5 Tier-A slots in the deploy shortlist (PF ≥ 2.5 on 4H charts "
            "across RENDER, BONK, WIF, SOL, ETH). The 4H timeframe consistently "
            "outperforms 1H because the anchor EMA (55 × 4H = 9 days) filters "
            "out weekly mean-reversion cycles that whipsaw the 1H version."
        ),
    },
    {
        "file": "indicator_vwap_dev_v1.0.pine",
        "slug": "vwap_dev",
        "title": "VWAP Deviation",
        "version": "v1.0",
        "category": "Mean-Reversion",
        "overlay": True,
        "purpose": (
            "Identifies price extremes relative to volume-weighted average by "
            "computing a rolling 20-period VWAP and ±2 standard deviation bands. "
            "Mean reversion logic: when price moves too far from the anchor, it "
            "tends to revert toward the mean."
        ),
        "market_regime": (
            "Thrives in range-bound markets where volatility expansion is "
            "followed by reversion. Loses in strong trends where the band-cross "
            "signals keep firing against the prevailing direction. The VWAP "
            "anchor adjusts to volume, so it's more responsive than pure SMA "
            "bands during high-volume breakouts."
        ),
        "entry_logic": (
            "BUY fires when price crosses back ABOVE the lower band (2σ below "
            "VWAP). Specifically: close[1] < lower[1] AND close > lower. "
            "The two-bar confirmation rejects transient wicks."
        ),
        "exit_logic": (
            "CLOSE fires when price crosses back above the VWAP anchor — the "
            "mean-reversion target. An intra-bar guard prevents single violent "
            "bars that cross both the band AND VWAP from emitting BUY+CLOSE "
            "simultaneously."
        ),
        "notes": (
            "Highly variable performance — can produce outsized wins (JTO 4H "
            "PF=2.94, NP=418%) or large drawdowns. The wide OOS PF range "
            "in nightly results (PENGU 1H OOS_PF=9.45 vs median ~2.0) "
            "suggests sensitivity to regime — use with a portfolio of "
            "low-correlated indicators to smooth equity curve."
        ),
    },
    {
        "file": "indicator_fvg_v1.0.pine",
        "slug": "fvg",
        "title": "Fair Value Gap (FVG)",
        "version": "v1.0",
        "category": "ICT / Order Flow",
        "overlay": True,
        "purpose": (
            "Detects three-candle imbalance patterns where price skips past a "
            "zone with no overlap, leaving an unfilled gap. ICT theory treats "
            "these gaps as institutional order imbalances that price often "
            "revisits to 'fill' before continuing in the direction of the gap."
        ),
        "market_regime": (
            "Works across regimes because gaps form in both trending and "
            "ranging markets. Most effective on higher timeframes (4H+) where "
            "volume surges validate the gap as institutional activity rather "
            "than thin-order-book noise. 1H gaps are noisier."
        ),
        "entry_logic": (
            "Bullish FVG: current bar's low > high of 2 bars ago (upward imbalance). "
            "BUY fires on the bar AFTER the gap forms (shift by 1) with a volume "
            "surge confirmation (1.5× the 20-bar volume MA)."
        ),
        "exit_logic": (
            "CLOSE fires when price drops below the gap's lower reference "
            "(high[2] — the top of the pre-gap bar) OR when an opposite "
            "bearish FVG forms. A same-bar guard prevents instant BUY→CLOSE "
            "loops when entry and exit conditions both trigger."
        ),
        "notes": (
            "Strong consistency across tokens — six backtest-validated "
            "deployments on 4H with PF in the 2.0-2.4 range. Less timeframe-"
            "sensitive than trend indicators. The volume filter is critical: "
            "without it, small gaps on thin books produce false entries."
        ),
    },
    {
        "file": "indicator_liq_sweep_v1.0.pine",
        "slug": "liq_sweep",
        "title": "Liquidity Sweep",
        "version": "v1.0",
        "category": "ICT / Order Flow",
        "overlay": True,
        "purpose": (
            "Detects stop-hunt reversals where price briefly breaches a swing "
            "low, triggers stop-loss liquidity sitting below, but fails to "
            "close below the level — a signature of institutional accumulation "
            "where large players absorb retail stops."
        ),
        "market_regime": (
            "Best at range boundaries and after extended consolidations where "
            "stops have accumulated. Less effective in strong trends where "
            "swing-low breaks are genuine continuation breakdowns rather than "
            "sweeps."
        ),
        "entry_logic": (
            "BUY fires when the bar's low breaches the 10-bar swing low AND "
            "close remains above it AND volume exceeds 1.2× the 20-bar MA. "
            "The wick-through-but-close-above pattern is the reversal signature."
        ),
        "exit_logic": (
            "CLOSE fires on a bearish sweep (opposite pattern at swing high). "
            "A same-bar guard prevents a wide double-wick bar that sweeps "
            "both high and low from emitting BUY+CLOSE simultaneously."
        ),
        "notes": (
            "Strong ETH 1H performance (PF=3.43, WR=75%) — the 1H works here "
            "because ETH's liquidity depth makes stop-hunt patterns cleaner "
            "than on thinner memecoins. Combines well with trend indicators "
            "as a pullback entry in established uptrends."
        ),
    },
    {
        "file": "indicator_stoch_rsi_v1.0.pine",
        "slug": "stoch_rsi",
        "title": "Stochastic RSI",
        "version": "v1.0",
        "category": "Oscillator / Mean-Reversion",
        "overlay": False,
        "purpose": (
            "Two-stage momentum oscillator: applies the stochastic formula "
            "(where-in-range) to the RSI itself, producing a more sensitive "
            "overbought/oversold signal than either indicator alone. Paired "
            "with an RSI trend filter to avoid buying oversold conditions "
            "inside a broader downtrend."
        ),
        "market_regime": (
            "Ideal in oscillating markets with defined support/resistance. "
            "The oversold+RSI<50 dual filter specifically rejects setups "
            "where momentum is already rolling over — protects against "
            "catching falling knives during trend breaks."
        ),
        "entry_logic": (
            "BUY fires on K-line crossing ABOVE D-line while both are below the "
            "oversold threshold (default 20) AND the underlying RSI is below 50 "
            "(bearish-to-neutral zone, preventing buys in strong uptrends where "
            "oversold dips are shallow and brief)."
        ),
        "exit_logic": (
            "CLOSE fires on K-line crossing DOWN while K is above the overbought "
            "threshold (default 80). Long-only — the short-exit condition was "
            "removed because it fired on the same bar as BUY during violent "
            "reversals, causing instant BUY→CLOSE loops."
        ),
        "notes": (
            "RENDER 1H is the standout performer (PF=4.53, WR=80%) — RENDER's "
            "1H cycle matches the Stoch RSI's sensitivity window well. The "
            "indicator is plotted in a separate pane (overlay=false) unlike "
            "the other trend/price-based indicators."
        ),
    },
    {
        "file": "indicator_donchian_v1.0.pine",
        "slug": "donchian",
        "title": "Donchian Breakout",
        "version": "v1.0",
        "category": "Breakout / Trend-Following",
        "overlay": True,
        "purpose": (
            "Classic turtle-trader breakout system: enters when price breaks "
            "above a 20-bar rolling high, riding the new trend until momentum "
            "fades (mid-channel cross). The volume confirmation filters breakouts "
            "that lack genuine order flow."
        ),
        "market_regime": (
            "Best at volatility expansion events — range breakouts, news-driven "
            "surges, session-open moves. Underperforms in low-volatility chop "
            "where breakouts immediately fail (the channel period essentially "
            "tracks the current consolidation box)."
        ),
        "entry_logic": (
            "BUY fires when close crosses above the prior bar's 20-period "
            "channel high AND volume exceeds 1.5× the 20-bar volume MA. "
            "The [1] offset avoids look-ahead bias (entry can't trigger on "
            "the same bar that sets the new high)."
        ),
        "exit_logic": (
            "CLOSE fires when close crosses below mid-channel (midpoint of "
            "the 20-bar high/low range) — a momentum fade signal."
        ),
        "notes": (
            "Lower win-rate profile (39% on SOL 1H) with asymmetric R:R — "
            "relies on large winners more than hit rate. Use smaller position "
            "sizing than mean-reversion strategies. Currently the weakest "
            "strategy in the deployment matrix but offers uncorrelated "
            "signal character for portfolio diversification."
        ),
    },
]

STYLES = getSampleStyleSheet()

TITLE_STYLE = ParagraphStyle(
    "IndTitle", parent=STYLES["Heading1"], fontSize=20, textColor=colors.HexColor("#1e40af"),
    spaceAfter=4, leading=24,
)
SUBTITLE_STYLE = ParagraphStyle(
    "IndSubtitle", parent=STYLES["Normal"], fontSize=10, textColor=colors.HexColor("#64748b"),
    spaceAfter=14, italic=True,
)
H2_STYLE = ParagraphStyle(
    "IndH2", parent=STYLES["Heading2"], fontSize=13, textColor=colors.HexColor("#0f172a"),
    spaceBefore=14, spaceAfter=6, leading=16,
)
BODY_STYLE = ParagraphStyle(
    "IndBody", parent=STYLES["BodyText"], fontSize=10, leading=14, spaceAfter=6,
    textColor=colors.HexColor("#1e293b"),
)
CODE_STYLE = ParagraphStyle(
    "IndCode", parent=STYLES["Code"], fontSize=7.5, leading=9.5,
    textColor=colors.HexColor("#0f172a"), backColor=colors.HexColor("#f1f5f9"),
    leftIndent=6, borderPadding=4,
)
SMALL_STYLE = ParagraphStyle(
    "IndSmall", parent=STYLES["Normal"], fontSize=8.5, leading=11,
    textColor=colors.HexColor("#475569"),
)


def parse_backtest_matrix(source: str) -> list[list[str]]:
    """Extract the backtest results table from Pine header comments."""
    rows = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            if in_block:
                break
            continue
        content = stripped.lstrip("/").strip()
        if "Passed backtesting" in content:
            in_block = True
            continue
        if in_block:
            if not content or "Signal logic" in content:
                break
            # Parse "TOKEN TF PF=X WR=Y% N trades DD=Z% NP=W%"
            match = re.match(
                r"(\S+)\s+(\S+)\s+PF=(\S+)\s+WR=(\S+)\s+(\d+)\s+trades\s+DD=(\S+)\s+NP=(\S+)",
                content,
            )
            if match:
                rows.append(list(match.groups()))
    return rows


def sanitize_pine(source: str) -> str:
    """Pine source is already sanitized (CHANGE_ME placeholder). Keep defensively:
    strip any line that looks like it contains a real webhook URL or secret."""
    lines = []
    for line in source.splitlines():
        if re.search(r"https?://[^/]+\.(com|io|net|org|dev)/[^\s\"']+", line, re.IGNORECASE):
            lines.append(re.sub(r"https?://\S+", "[REDACTED_URL]", line))
            continue
        lines.append(line)
    return "\n".join(lines)


def code_to_flowables(source: str) -> list:
    """Convert sanitized Pine source into paged Preformatted blocks so ReportLab
    can split the code across pages automatically."""
    # Preformatted handles page-break splitting; one block is sufficient.
    return [Preformatted(source, CODE_STYLE)]


def build_indicator_pdf(ind: dict, out_path: Path) -> None:
    pine_path = PINE_DIR / ind["file"]
    source = pine_path.read_text()
    sanitized = sanitize_pine(source)
    matrix = parse_backtest_matrix(source)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"{ind['title']} {ind['version']} — Indicator Documentation",
        author="TradingView-Bot",
    )

    story = []
    story.append(Paragraph(f"{ind['title']} <font size=14 color='#64748b'>{ind['version']}</font>", TITLE_STYLE))
    story.append(Paragraph(
        f"{ind['category']} • {'Price overlay' if ind['overlay'] else 'Separate pane'} • "
        f"Generated {date.today().isoformat()}",
        SUBTITLE_STYLE,
    ))

    story.append(Paragraph("Purpose", H2_STYLE))
    story.append(Paragraph(ind["purpose"], BODY_STYLE))

    story.append(Paragraph("Market Conditions", H2_STYLE))
    story.append(Paragraph(ind["market_regime"], BODY_STYLE))

    story.append(Paragraph("Entry Trigger (BUY)", H2_STYLE))
    story.append(Paragraph(ind["entry_logic"], BODY_STYLE))

    story.append(Paragraph("Exit Trigger (CLOSE)", H2_STYLE))
    story.append(Paragraph(ind["exit_logic"], BODY_STYLE))

    if matrix:
        story.append(Paragraph("Backtest-Validated Deployments", H2_STYLE))
        story.append(Paragraph(
            "Results from the project's walk-forward validated backtesting engine. "
            "These are the token/timeframe pairs that met the PF ≥ 1.5, DD ≤ 20%, "
            "and 70/30 in-sample/out-of-sample consistency thresholds.",
            SMALL_STYLE,
        ))
        story.append(Spacer(1, 6))
        header = ["Token", "TF", "Profit Factor", "Win Rate", "Trades", "Max DD", "Net Profit"]
        tbl_data = [header] + matrix
        tbl = Table(tbl_data, colWidths=[0.9 * inch] * 7)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(tbl)

    story.append(Paragraph("Notes", H2_STYLE))
    story.append(Paragraph(ind["notes"], BODY_STYLE))

    story.append(Paragraph("Webhook Alert Payload", H2_STYLE))
    story.append(Paragraph(
        "The indicator fires alert() calls with a JSON payload delivered to the "
        "FastAPI webhook. The secret field is a shared-secret HMAC-equivalent "
        "check — the placeholder 'CHANGE_ME' must be replaced with the bot's "
        "webhook_secret from config.yaml before deployment. The webhook URL "
        "is configured in the TradingView alert UI, not in the Pine source.",
        BODY_STYLE,
    ))
    payload_example = (
        '{\n'
        '  "secret": "CHANGE_ME",\n'
        '  "signal_type": "BUY",\n'
        '  "symbol": "SOLUSDT",\n'
        '  "entry_price_estimate": 142.35,\n'
        '  "confidence_score": 70,\n'
        '  "suggested_leverage": 2,\n'
        '  "suggested_position_size_percent": 15.0,\n'
        '  "rsi": 54.21,\n'
        '  "atr": 3.14,\n'
        '  "timeframe": "240",\n'
        f'  "strategy": "{ind["title"]} {ind["version"]}"\n'
        '}'
    )
    story.append(Preformatted(payload_example, CODE_STYLE))

    story.append(PageBreak())
    story.append(Paragraph("Pine Source Code", H2_STYLE))
    story.append(Paragraph(
        f"File: Indicators/staged/{ind['file']} • Pine v6 • "
        f"{len(source.splitlines())} lines. Sanitized: URLs redacted if present; "
        "webhook secret is the default 'CHANGE_ME' placeholder.",
        SMALL_STYLE,
    ))
    story.append(Spacer(1, 8))
    story.extend(code_to_flowables(sanitized))

    doc.build(story)


def build_index_pdf(out_path: Path) -> None:
    """Single combined PDF containing all indicators."""
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title="TradingView Bot — Indicator Reference (All)",
        author="TradingView-Bot",
    )
    story = []
    story.append(Paragraph("TradingView Bot", TITLE_STYLE))
    story.append(Paragraph(
        f"Complete Indicator Reference • {len(INDICATORS)} strategies • "
        f"Generated {date.today().isoformat()}",
        SUBTITLE_STYLE,
    ))
    story.append(Paragraph("Contents", H2_STYLE))
    for i, ind in enumerate(INDICATORS, 1):
        story.append(Paragraph(
            f"{i}. <b>{ind['title']}</b> {ind['version']} — {ind['category']}",
            BODY_STYLE,
        ))
    story.append(PageBreak())

    for ind in INDICATORS:
        pine_path = PINE_DIR / ind["file"]
        source = pine_path.read_text()
        sanitized = sanitize_pine(source)
        matrix = parse_backtest_matrix(source)

        story.append(Paragraph(f"{ind['title']} <font size=14 color='#64748b'>{ind['version']}</font>", TITLE_STYLE))
        story.append(Paragraph(
            f"{ind['category']} • {'Price overlay' if ind['overlay'] else 'Separate pane'}",
            SUBTITLE_STYLE,
        ))
        story.append(Paragraph("Purpose", H2_STYLE))
        story.append(Paragraph(ind["purpose"], BODY_STYLE))
        story.append(Paragraph("Market Conditions", H2_STYLE))
        story.append(Paragraph(ind["market_regime"], BODY_STYLE))
        story.append(Paragraph("Entry (BUY)", H2_STYLE))
        story.append(Paragraph(ind["entry_logic"], BODY_STYLE))
        story.append(Paragraph("Exit (CLOSE)", H2_STYLE))
        story.append(Paragraph(ind["exit_logic"], BODY_STYLE))
        if matrix:
            story.append(Paragraph("Backtest-Validated Deployments", H2_STYLE))
            header = ["Token", "TF", "PF", "WR", "Trades", "DD", "NP"]
            tbl = Table([header] + matrix, colWidths=[0.9 * inch] * 7)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]))
            story.append(tbl)
        story.append(Paragraph("Notes", H2_STYLE))
        story.append(Paragraph(ind["notes"], BODY_STYLE))
        story.append(Paragraph("Pine Source", H2_STYLE))
        story.append(Paragraph(
            f"File: Indicators/staged/{ind['file']} • {len(source.splitlines())} lines",
            SMALL_STYLE,
        ))
        story.append(Spacer(1, 6))
        story.extend(code_to_flowables(sanitized))
        story.append(PageBreak())

    doc.build(story)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ind in INDICATORS:
        out = OUT_DIR / f"{ind['slug']}_{ind['version']}.pdf"
        build_indicator_pdf(ind, out)
        print(f"Wrote {out.relative_to(ROOT)}  ({out.stat().st_size // 1024} KB)")
    combined = OUT_DIR / "all_indicators.pdf"
    build_index_pdf(combined)
    print(f"Wrote {combined.relative_to(ROOT)}  ({combined.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
