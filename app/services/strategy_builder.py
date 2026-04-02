"""Strategy Builder — indicator generator, parameter grid, token sweep, templates.

Provides tools to accelerate the backtest → deploy workflow:
  1. Auto-import: Parse pasted TradingView Strategy Tester summary text
  2. Generate indicator: Convert a backtest strategy into a webhook alert indicator
  3. Parameter grid: Generate Pine script variants for batch testing
  4. Token sweep: Duplicate a strategy across multiple tokens
  5. Strategy comparison: Cross-token/cross-strategy analysis
  6. Templates: Pre-built strategy starting points
"""

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("bot.strategy_builder")

# =============================================================================
# 1. AUTO-IMPORT — Parse pasted TradingView Strategy Tester output
# =============================================================================


def parse_tv_summary(text: str) -> dict:
    """Parse pasted TradingView Strategy Tester summary text into backtest fields.

    Handles the typical TradingView performance summary format with sections like:
      Net Profit, Gross Profit, Max Drawdown, Total Closed Trades, etc.
    Also handles the Overview tab format and the two-column All/Long/Short layout.
    """

    def extract_num(pattern, txt, group=1):
        m = re.search(pattern, txt, re.MULTILINE | re.IGNORECASE)
        if not m:
            return None
        val = m.group(group).strip()
        val = val.replace(",", "").replace("$", "").replace("+", "").replace("%", "").replace("−", "-").replace("–", "-").replace("\u2212", "-")
        try:
            return float(val)
        except ValueError:
            return None

    def extract_str(pattern, txt, group=1):
        m = re.search(pattern, txt, re.MULTILINE | re.IGNORECASE)
        return m.group(group).strip() if m else None

    result = {}

    # Net Profit — try multiple formats
    result["net_profit_usd"] = (
        extract_num(r"Net Profit\s*[\$]?\s*([+\-\d,.]+)", text) or
        extract_num(r"Net Profit.*?([+\-\d,.]+)\s*USD", text) or
        extract_num(r"Net Profit.*?([+\-\d,.]+)\s*%", text)  # fallback to pct
    )
    result["net_profit_pct"] = (
        extract_num(r"Net Profit.*?([+\-\d,.]+)\s*%", text) or
        extract_num(r"Net Profit\s*\$[\d,.]+\s*\(([+\-\d,.]+)%\)", text)
    )

    # Gross Profit / Loss
    result["gross_profit"] = extract_num(r"Gross Profit\s*[\$]?\s*([+\-\d,.]+)", text)
    result["gross_loss"] = extract_num(r"Gross Loss\s*[\$]?\s*-?[\$]?\s*([+\-\d,.]+)", text)

    # Profit Factor
    result["profit_factor"] = extract_num(r"Profit Factor\s*([+\-\d.]+)", text)

    # Max Drawdown
    result["max_drawdown"] = extract_num(r"Max\.?\s*Drawdown\s*[\$]?\s*([+\-\d,.]+)", text)

    # Sharpe / Sortino
    result["sharpe_ratio"] = extract_num(r"Sharpe\s*Ratio\s*([+\-\d.]+)", text)
    result["sortino_ratio"] = extract_num(r"Sortino\s*Ratio\s*([+\-\d.]+)", text)

    # Trade counts
    result["total_trades"] = int(extract_num(r"(?:Total\s*)?(?:Closed\s*)?Trades\s*(\d+)", text) or 0)
    result["winning_trades"] = int(
        extract_num(r"(?:Number\s*)?(?:of\s*)?Win(?:ning|ners?)(?:\s*Trades)?\s*(\d+)", text) or 0
    )
    result["losing_trades"] = int(
        extract_num(r"(?:Number\s*)?(?:of\s*)?Los(?:ing|ers?)(?:\s*Trades)?\s*(\d+)", text) or 0
    )

    # Win rate
    result["win_rate"] = (
        extract_num(r"(?:Percent\s*)?(?:Win\s*Rate|Profitable)\s*([+\-\d.]+)\s*%", text) or
        extract_num(r"Win\s*Rate\s*([+\-\d.]+)", text)
    )
    # If we have winners and total, calculate
    if result["win_rate"] is None and result["total_trades"] > 0 and result["winning_trades"] > 0:
        result["win_rate"] = round(result["winning_trades"] / result["total_trades"] * 100, 1)

    # Avg win/loss
    result["avg_win"] = extract_num(r"Avg\.?\s*Win(?:ning)?\s*(?:Trade)?\s*[\$]?\s*([+\-\d,.]+)", text)
    result["avg_loss"] = extract_num(r"Avg\.?\s*Los(?:ing|s)\s*(?:Trade)?\s*[\$]?\s*([+\-\d,.]+)", text)
    result["win_loss_ratio"] = extract_num(r"(?:Avg\s*)?Win[/\s]*Loss\s*Ratio\s*([+\-\d.]+)", text)
    if result["win_loss_ratio"] is None and result["avg_win"] and result["avg_loss"] and result["avg_loss"] > 0:
        result["win_loss_ratio"] = round(result["avg_win"] / result["avg_loss"], 3)

    # Largest win/loss
    result["largest_win"] = extract_num(r"Largest\s*Win(?:ning)?\s*(?:Trade)?\s*[\$]?\s*([+\-\d,.]+)", text)
    result["largest_loss"] = extract_num(r"Largest\s*Los(?:ing|s)\s*(?:Trade)?\s*[\$]?\s*([+\-\d,.]+)", text)

    # Long/Short breakdown
    result["long_trades"] = int(extract_num(r"Long\s*(?:Trades)?\s*(\d+)", text) or 0)
    result["long_win_rate"] = extract_num(r"Long.*?(?:Win\s*Rate|Profitable)\s*([+\-\d.]+)\s*%", text)
    result["long_pnl"] = extract_num(r"Long.*?(?:Net\s*Profit|P&?L)\s*[\$]?\s*([+\-\d,.]+)", text)
    result["short_trades"] = int(extract_num(r"Short\s*(?:Trades)?\s*(\d+)", text) or 0)
    result["short_win_rate"] = extract_num(r"Short.*?(?:Win\s*Rate|Profitable)\s*([+\-\d.]+)\s*%", text)
    result["short_pnl"] = extract_num(r"Short.*?(?:Net\s*Profit|P&?L)\s*[\$]?\s*([+\-\d,.]+)", text)

    # Initial capital
    result["initial_capital"] = extract_num(r"(?:Initial\s*)?Capital\s*[\$]?\s*([\d,.]+)", text)

    return {k: v for k, v in result.items() if v is not None}


# =============================================================================
# 2. INDICATOR GENERATOR — Convert backtest strategy to webhook alert indicator
# =============================================================================

INDICATOR_TEMPLATE = '''// =============================================================================
// {title} — {timeframe} ALERT INDICATOR ({token_label})
//
// Auto-generated from backtest: {strategy_name} {version}
// Generated: {generated_date}
//
// {description}
//
// SETUP:
//   1. Open TradingView -> token/USDT -> {timeframe} timeframe
//   2. Pine Editor -> paste this -> Add to chart
//   3. Set your webhook secret in indicator settings
//   4. Create alert: Condition = this indicator -> "Any alert() function call"
//   5. Webhook URL: https://your-bot-url/webhook
//   6. Expiration: max allowed, set calendar reminder to renew
// =============================================================================

//@version=6
indicator("{title} — {timeframe} Alerts", overlay=true, max_bars_back=500)

// =============================================================================
// INPUTS
// =============================================================================

grp_wh = "Webhook"
webhook_secret = input.string("CHANGE_ME", "Webhook Secret", group=grp_wh)
{inputs_section}

grp_cool = "Cooldown"
cooldown_bars = input.int({cooldown}, "Bars Between Signals", minval=0, maxval=50, group=grp_cool)

grp_dip = "Deep Dip Override"
dip_enabled      = input.bool(true, "Enable Deep Dip Override", group=grp_dip, tooltip="Capture extreme dips that normal logic misses")
dip_atr_mult     = input.float({dip_depth}, "ATR Below BB (depth)", minval=1.0, maxval=5.0, step=0.5, group=grp_dip)
dip_min_confirm  = input.int({dip_confirms}, "Min Confirms for Dip", minval=1, maxval=6, group=grp_dip)
dip_rsi_required = input.bool(true, "Require RSI Oversold", group=grp_dip)

// =============================================================================
// CALCULATIONS
// =============================================================================

{calculations_section}

// =============================================================================
// DEEP DIP OVERRIDE
// =============================================================================

dip_depth = bb_lower > 0 and atr_val > 0 ? (bb_lower - low) / atr_val : 0.0
is_deep_dip = dip_enabled and dip_depth >= dip_atr_mult{dip_gate_extra}

dip_confirms_met = is_deep_dip and long_confirms >= dip_min_confirm
dip_rsi_ok = not dip_rsi_required or {rsi_oversold_check}
deep_dip_signal = dip_confirms_met and dip_rsi_ok and cooldown_ok and barstate.isconfirmed

// =============================================================================
// SIGNAL
// =============================================================================

normal_signal = {normal_signal_condition}
long_signal = normal_signal or deep_dip_signal
is_dip_entry = deep_dip_signal and not normal_signal

if long_signal
    bars_since_signal := 0

// Confidence
dip_bonus = is_dip_entry ? math.min(15, math.round(dip_depth * 5)) : 0
long_conf = math.min(100, math.round({confidence_formula} + dip_bonus))
pos_size_pct = long_conf >= 85 ? 15.0 : long_conf >= 70 ? 10.0 : long_conf >= 55 ? 7.0 : 5.0
calc_lev(c) => c >= 85 ? 5 : c >= 70 ? 3 : c >= 55 ? 2 : 1

// =============================================================================
// ALERT
// =============================================================================

alertcondition(long_signal, "BUY Signal", "BUY")

if long_signal
    alert('{{"secret":"' + webhook_secret + '","signal_type":"BUY","symbol":"' + syminfo.ticker + '","entry_price_estimate":' + str.tostring(close) + ',"confidence_score":' + str.tostring(long_conf) + ',"suggested_leverage":' + str.tostring(calc_lev(long_conf)) + ',"suggested_position_size_percent":' + str.tostring(pos_size_pct) + ',"bull_score":' + str.tostring(long_confirms) + ',"bear_score":0,"banker_entry":0,"macd_divergence":0,"rsi":' + str.tostring(math.round(rsi_val, 2)) + ',"atr":' + str.tostring(atr_val) + ',"deep_dip":' + str.tostring(is_dip_entry ? 1 : 0) + ',"dip_depth_atr":' + str.tostring(math.round(dip_depth, 2)) + ',"timeframe":"' + timeframe.period + '"}}', alert.freq_once_per_bar_close)

// =============================================================================
// VISUALS
// =============================================================================

{visuals_section}

plotshape(normal_signal, "MR BUY", shape.triangleup, location.belowbar, color.lime, size=size.normal, text="MR BUY")
plotshape(is_dip_entry, "DIP BUY", shape.triangleup, location.belowbar, color.fuchsia, size=size.normal, text="DIP BUY")

bgcolor(close <= bb_lower ? color.new(color.green, 90) : na, title="BB Lower Touch")
bgcolor(is_deep_dip ? color.new(color.fuchsia, 88) : na, title="Deep Dip Zone")

// =============================================================================
// INFO TABLE
// =============================================================================

{table_section}
'''


def generate_indicator(
    strategy_name: str,
    version: str,
    timeframe: str,
    pine_code: str,
    tokens: list[str] = None,
) -> str:
    """Generate a webhook alert indicator from a backtest strategy Pine script.

    Performs the mechanical conversion:
    - Strips strategy() call, replaces with indicator()
    - Removes strategy.entry/exit calls
    - Adds webhook alert JSON
    - Adds cooldown, deep dip override, info table
    - Adds visual markers for entries

    Returns the generated Pine script as a string.
    """
    token_label = ", ".join(tokens) if tokens else "Multi-Token"
    title = f"{strategy_name} {version}"

    # Detect timeframe-specific settings
    tf_map = {"4H": ("240", 2.0, 2), "1H": ("60", 2.5, 3), "D": ("D", 2.0, 2), "2H": ("120", 2.5, 3)}
    tf_code, dip_depth, dip_confirms = tf_map.get(timeframe.upper(), ("60", 2.5, 3))
    cooldown = 3

    # Extract key sections from the strategy Pine code
    lines = pine_code.split("\n")

    # Find inputs section (between INPUTS markers or input.* lines)
    inputs = []
    calcs = []
    in_inputs = False
    in_calcs = False
    has_bb = False
    has_rsi = False

    for line in lines:
        stripped = line.strip()

        # Skip strategy-specific lines
        if stripped.startswith("strategy(") or stripped.startswith("strategy."):
            continue
        if "default_qty" in stripped or "initial_capital" in stripped:
            continue

        # Track inputs (skip webhook/cooldown — we generate our own)
        if "input." in stripped and "Webhook" not in stripped and "Cooldown" not in stripped:
            inputs.append(line)

        # Track BB and RSI presence
        if "ta.bb(" in stripped:
            has_bb = True
        if "ta.rsi(" in stripped:
            has_rsi = True

    # Build a minimal generated indicator
    # For complex strategies, it's better to provide the full code as-is with
    # strategy calls stripped, rather than trying to parse everything
    clean_lines = []
    skip_until_visual = False
    in_strategy_exec = False

    for line in lines:
        stripped = line.strip()

        # Skip Pine version and strategy declaration
        if stripped.startswith("//@version") or stripped.startswith("strategy("):
            continue

        # Skip strategy execution blocks
        if "strategy.entry" in stripped or "strategy.exit" in stripped or "strategy.close" in stripped:
            continue

        # Skip visual/table sections — we generate our own
        if "// VISUALS" in stripped or "// INFO TABLE" in stripped:
            skip_until_visual = True
            continue
        if skip_until_visual and stripped.startswith("//") and "=====" in stripped:
            skip_until_visual = False
            continue
        if skip_until_visual:
            continue

        # Skip strategy execution section header
        if "STRATEGY EXECUTION" in stripped:
            in_strategy_exec = True
            continue
        if in_strategy_exec and stripped.startswith("//") and "=====" in stripped:
            in_strategy_exec = False
            continue
        if in_strategy_exec:
            continue

        clean_lines.append(line)

    # Build the output — inject our alert/visual/table code into the cleaned strategy
    result_lines = [
        f"// =============================================================================",
        f"// {title} — {timeframe} ALERT INDICATOR ({token_label})",
        f"//",
        f"// Auto-generated from backtest strategy on {datetime.utcnow().strftime('%Y-%m-%d')}",
        f"// Original: {strategy_name} {version}",
        f"// =============================================================================",
        f"",
        f"//@version=6",
        f'indicator("{title} — {timeframe} Alerts", overlay=true, max_bars_back=500)',
        f"",
    ]

    # Add webhook secret input at the top of inputs
    injected_webhook = False
    injected_dip = False
    injected_cooldown = False

    for line in clean_lines:
        stripped = line.strip()

        # Inject webhook secret before first input group
        if not injected_webhook and stripped.startswith("grp_"):
            result_lines.append('grp_wh = "Webhook"')
            result_lines.append('webhook_secret = input.string("CHANGE_ME", "Webhook Secret", group=grp_wh)')
            result_lines.append("")
            injected_webhook = True

        # Inject deep dip override after cooldown section
        if not injected_dip and "cooldown_ok" in stripped and "bars_since_signal" in stripped:
            result_lines.append(line)
            result_lines.append("")
            result_lines.append("// =============================================================================")
            result_lines.append("// DEEP DIP OVERRIDE")
            result_lines.append("// =============================================================================")
            result_lines.append("")
            result_lines.append('grp_dip = "Deep Dip Override"')
            result_lines.append(f'dip_enabled      = input.bool(true, "Enable Deep Dip Override", group=grp_dip)')
            result_lines.append(f'dip_atr_mult     = input.float({dip_depth}, "ATR Below BB (depth)", minval=1.0, maxval=5.0, step=0.5, group=grp_dip)')
            result_lines.append(f'dip_min_confirm  = input.int({dip_confirms}, "Min Confirms for Dip", minval=1, maxval=6, group=grp_dip)')
            result_lines.append(f'dip_rsi_required = input.bool(true, "Require RSI Oversold", group=grp_dip)')
            result_lines.append("")
            result_lines.append("dip_depth = bb_lower > 0 and atr_val > 0 ? (bb_lower - low) / atr_val : 0.0")
            if has_bb:
                result_lines.append("is_deep_dip = dip_enabled and dip_depth >= dip_atr_mult and (close <= bb_lower or low <= bb_lower)")
            else:
                result_lines.append("is_deep_dip = dip_enabled and dip_depth >= dip_atr_mult")
            result_lines.append("")
            result_lines.append("dip_confirms_met = is_deep_dip and long_confirms >= dip_min_confirm")
            rsi_check = "confirm_rsi" if has_rsi else "(rsi_val < rsi_os)"
            result_lines.append(f"dip_rsi_ok = not dip_rsi_required or {rsi_check}")
            result_lines.append("deep_dip_signal = dip_confirms_met and dip_rsi_ok and cooldown_ok and barstate.isconfirmed")
            injected_dip = True
            continue

        # Modify signal line to include deep dip
        if "long_signal" in stripped and "=" in stripped and "barstate.isconfirmed" in stripped and "deep_dip" not in stripped:
            result_lines.append(f"normal_signal = {stripped.split('=', 1)[1].strip()}")
            result_lines.append("long_signal = normal_signal or deep_dip_signal")
            result_lines.append("is_dip_entry = deep_dip_signal and not normal_signal")
            continue

        result_lines.append(line)

    # Add alert section at the end
    result_lines.extend([
        "",
        "// =============================================================================",
        "// ALERT",
        "// =============================================================================",
        "",
        'alertcondition(long_signal, "BUY Signal", "BUY")',
        "",
        "// Confidence",
        "dip_bonus = is_dip_entry ? math.min(15, math.round(dip_depth * 5)) : 0",
        "long_conf = math.min(100, math.round(long_confirms / 6.0 * 50 + (vol_spike ? 15 : 0) + (pst_bullish ? 15 : 0) + (banker_bull_recent ? 20 : 0) + dip_bonus))",
        "pos_size_pct = long_conf >= 85 ? 15.0 : long_conf >= 70 ? 10.0 : long_conf >= 55 ? 7.0 : 5.0",
        "calc_lev(c) => c >= 85 ? 5 : c >= 70 ? 3 : c >= 55 ? 2 : 1",
        "",
        "if long_signal",
        """    alert('{"secret":"' + webhook_secret + '","signal_type":"BUY","symbol":"' + syminfo.ticker + '","entry_price_estimate":' + str.tostring(close) + ',"confidence_score":' + str.tostring(long_conf) + ',"suggested_leverage":' + str.tostring(calc_lev(long_conf)) + ',"suggested_position_size_percent":' + str.tostring(pos_size_pct) + ',"bull_score":' + str.tostring(long_confirms) + ',"bear_score":0,"banker_entry":0,"macd_divergence":0,"rsi":' + str.tostring(math.round(rsi_val, 2)) + ',"atr":' + str.tostring(atr_val) + ',"deep_dip":' + str.tostring(is_dip_entry ? 1 : 0) + ',"dip_depth_atr":' + str.tostring(math.round(dip_depth, 2)) + ',"timeframe":"' + timeframe.period + '"}', alert.freq_once_per_bar_close)""",
        "",
        "// =============================================================================",
        "// VISUALS",
        "// =============================================================================",
        "",
        'plotshape(normal_signal, "MR BUY", shape.triangleup, location.belowbar, color.lime, size=size.normal, text="MR BUY")',
        'plotshape(is_dip_entry, "DIP BUY", shape.triangleup, location.belowbar, color.fuchsia, size=size.normal, text="DIP BUY")',
        "",
        "bgcolor(is_deep_dip ? color.new(color.fuchsia, 88) : na, title=\"Deep Dip Zone\")",
    ])

    return "\n".join(result_lines)


# =============================================================================
# 3. PARAMETER GRID GENERATOR
# =============================================================================


def generate_parameter_grid(
    pine_code: str,
    param_ranges: dict,
    strategy_name: str = "Strategy",
) -> list[dict]:
    """Generate Pine script variants by sweeping parameter ranges.

    param_ranges: dict of param_name -> {min, max, step} or list of values
    Example: {"min_confirm": {"min": 3, "max": 5, "step": 1}, "bb_mult": [1.5, 2.0, 2.5]}

    Returns list of {params: {...}, pine_code: str, label: str}
    """
    import itertools

    # Build parameter value lists
    param_names = []
    param_values = []

    for name, spec in param_ranges.items():
        param_names.append(name)
        if isinstance(spec, list):
            param_values.append(spec)
        elif isinstance(spec, dict):
            vals = []
            v = spec["min"]
            while v <= spec["max"]:
                vals.append(round(v, 6))
                v += spec["step"]
            param_values.append(vals)

    # Generate all combinations
    variants = []
    for combo in itertools.product(*param_values):
        params = dict(zip(param_names, combo))

        # Generate modified Pine code
        modified = pine_code
        for pname, pvalue in params.items():
            # Replace input default values — matches patterns like:
            # input.int(4, "Label"  or  input.float(2.0, "Label"
            pattern = rf'(input\.(?:int|float))\(\s*[\d.]+\s*,\s*"([^"]*{re.escape(pname)}[^"]*)"'
            replacement = rf'\1({pvalue}, "\2"'
            new_modified = re.sub(pattern, replacement, modified, flags=re.IGNORECASE)

            # If regex didn't match by label, try matching by variable name
            if new_modified == modified:
                # Match: var_name = input.int(VALUE, or input.float(VALUE,
                var_pattern = rf'({re.escape(pname)}\s*=\s*input\.(?:int|float))\(\s*[\d.]+'
                var_replacement = rf'\1({pvalue}'
                new_modified = re.sub(var_pattern, new_modified, modified)
                if new_modified == modified:
                    # Direct value replacement as fallback
                    var_pattern2 = rf'({re.escape(pname)}\s*=\s*input\.(?:int|float)\()[\d.]+'
                    new_modified = re.sub(var_pattern2, rf'\g<1>{pvalue}', modified)

            modified = new_modified

        # Update strategy title to include params
        param_label = "_".join(f"{k}{v}" for k, v in params.items())
        modified = re.sub(
            r'(strategy\(["\'])([^"\']+)(["\'])',
            rf'\1\2 [{param_label}]\3',
            modified,
            count=1,
        )

        variants.append({
            "params": params,
            "pine_code": modified,
            "label": param_label,
        })

    logger.info(f"Generated {len(variants)} parameter variants for {strategy_name}")
    return variants


# =============================================================================
# 4. TOKEN SWEEP GENERATOR
# =============================================================================

# Known token symbols and their typical config
TOKEN_CONFIGS = {
    "SOL": {"pair": "SOLUSDT", "exchange": "BINANCE"},
    "JTO": {"pair": "JTOUSDT", "exchange": "BINANCE"},
    "WIF": {"pair": "WIFUSDT", "exchange": "BINANCE"},
    "BONK": {"pair": "BONKUSDT", "exchange": "BINANCE"},
    "PYTH": {"pair": "PYTHUSDT", "exchange": "BINANCE"},
    "RAY": {"pair": "RAYUSDT", "exchange": "BINANCE"},
}


def generate_token_sweep(
    pine_code: str,
    tokens: list[str],
    strategy_name: str = "Strategy",
) -> list[dict]:
    """Generate a strategy variant for each token.

    Returns list of {token, pair, pine_code, label}
    """
    variants = []

    for token in tokens:
        cfg = TOKEN_CONFIGS.get(token.upper(), {"pair": f"{token.upper()}USDT", "exchange": "BINANCE"})
        modified = pine_code

        # Update strategy title with token name
        modified = re.sub(
            r'((?:strategy|indicator)\(["\'])([^"\']+)(["\'])',
            rf'\1\2 [{token.upper()}]\3',
            modified,
            count=1,
        )

        variants.append({
            "token": token.upper(),
            "pair": cfg["pair"],
            "exchange": cfg["exchange"],
            "pine_code": modified,
            "label": f"{strategy_name}_{token.upper()}",
        })

    logger.info(f"Generated {len(variants)} token variants for {strategy_name}")
    return variants


# =============================================================================
# 5. STRATEGY COMPARISON
# =============================================================================


def compare_strategies(backtests: list[dict]) -> dict:
    """Analyze and compare backtests across strategies and tokens.

    Returns structured comparison with rankings and recommendations.
    """
    if not backtests:
        return {"strategies": [], "rankings": [], "best_combos": []}

    # Group by strategy+version
    by_strategy = {}
    for bt in backtests:
        key = f"{bt['strategy_name']} {bt['version']}"
        by_strategy.setdefault(key, []).append(bt)

    # Group by token
    by_token = {}
    for bt in backtests:
        token = bt.get("symbol", "").replace("USDT", "").replace("USD", "")
        by_token.setdefault(token, []).append(bt)

    # Build strategy summaries
    strategies = []
    for key, bts in by_strategy.items():
        pfs = [b["profit_factor"] for b in bts if b.get("profit_factor")]
        wrs = [b["win_rate"] for b in bts if b.get("win_rate")]
        trades = [b["total_trades"] for b in bts if b.get("total_trades")]
        tokens_tested = list(set(b.get("symbol", "") for b in bts))

        strategies.append({
            "strategy": key,
            "tokens_tested": tokens_tested,
            "avg_pf": round(sum(pfs) / len(pfs), 3) if pfs else 0,
            "avg_wr": round(sum(wrs) / len(wrs), 1) if wrs else 0,
            "total_trades": sum(trades),
            "profitable_tokens": len([b for b in bts if (b.get("profit_factor") or 0) >= 1.0]),
            "total_tokens": len(bts),
            "backtests": bts,
        })

    # Rank all strategy+token combos by profit factor
    rankings = []
    for bt in backtests:
        if bt.get("profit_factor") and bt.get("total_trades", 0) >= 10:
            rankings.append({
                "strategy": f"{bt['strategy_name']} {bt['version']}",
                "symbol": bt.get("symbol", ""),
                "timeframe": bt.get("timeframe", ""),
                "pf": bt["profit_factor"],
                "wr": bt.get("win_rate", 0),
                "trades": bt.get("total_trades", 0),
                "pnl": bt.get("net_profit_pct", 0),
                "sharpe": bt.get("sharpe_ratio"),
                "deployable": bt["profit_factor"] >= 1.0 and bt.get("total_trades", 0) >= 20,
            })

    rankings.sort(key=lambda x: x["pf"], reverse=True)

    # Best combos (PF >= 1.0 with enough trades)
    best = [r for r in rankings if r["deployable"]]

    return {
        "strategies": sorted(strategies, key=lambda x: x["avg_pf"], reverse=True),
        "rankings": rankings[:20],
        "best_combos": best[:10],
        "by_token": {
            token: {
                "count": len(bts),
                "avg_pf": round(sum(b["profit_factor"] for b in bts if b.get("profit_factor")) / max(1, len([b for b in bts if b.get("profit_factor")])), 3),
                "best_strategy": max(bts, key=lambda b: b.get("profit_factor", 0)).get("strategy_name", "") if bts else "",
            }
            for token, bts in by_token.items()
        },
    }


# =============================================================================
# 6. STRATEGY TEMPLATES
# =============================================================================

TEMPLATES = {
    "mean_reversion": {
        "name": "Mean Reversion",
        "description": "Buy dips at Bollinger Band lows with multi-factor confirmation. Best for ranging/mean-reverting assets.",
        "timeframes": ["4H", "D"],
        "direction": "LONG only",
        "entry": "BB lower touch + 4/6 confirms (RSI, volume, choppiness, banker flow, swing bottom, supertrend)",
        "exit": "TP 4x ATR / SL 1.5x ATR",
        "best_for": ["SOL", "JTO", "WIF"],
        "pine_code": '''// Mean Reversion Template — customize parameters below
//@version=6
strategy("Mean Reversion Template", overlay=true, max_bars_back=500,
         initial_capital=1000, default_qty_type=strategy.percent_of_equity,
         default_qty_value=15, commission_type=strategy.commission.percent,
         commission_value=0.1, slippage=2)

// === INPUTS ===
grp_entry = "Entry"
min_confirm = input.int(4, "Min Confirmations (of 6)", minval=1, maxval=6, group=grp_entry)

grp_bb = "Bollinger Bands"
bb_len  = input.int(20, "Length", group=grp_bb)
bb_mult = input.float(2.0, "Multiplier", group=grp_bb)

grp_rsi = "RSI"
rsi_len = input.int(14, "Length", group=grp_rsi)
rsi_os  = input.float(35, "Oversold Threshold", group=grp_rsi)

grp_vol = "Volume"
vol_ma_len     = input.int(20, "MA Length", group=grp_vol)
vol_spike_mult = input.float(1.3, "Spike Multiplier", group=grp_vol)

grp_chop = "Choppiness"
chop_len       = input.int(14, "Length", group=grp_chop)
chop_threshold = input.float(55, "Threshold", group=grp_chop)

grp_risk = "Risk Management"
sl_atr_mult = input.float(1.5, "SL ATR Multiplier", group=grp_risk)
tp_atr_mult = input.float(4.0, "TP ATR Multiplier", group=grp_risk)

grp_cool = "Cooldown"
cooldown_bars = input.int(3, "Bars Between Signals", minval=0, maxval=20, group=grp_cool)

// === HELPER ===
_wsa(float src, int length, int weight) =>
    var float s = 0.0
    var float ma = 0.0
    var float out = 0.0
    s := nz(s[1]) - nz(src[length]) + src
    ma := na(src[length]) ? na : s / length
    out := na(out[1]) ? ma : (src * weight + out[1] * (length - weight)) / length
    out

// === CALCULATIONS ===
[bb_mid, bb_upper, bb_lower] = ta.bb(close, bb_len, bb_mult)
rsi_val = ta.rsi(close, rsi_len)
vol_ma = ta.sma(volume, vol_ma_len)
vol_spike = volume > vol_ma * vol_spike_mult
atr_val = ta.atr(14)

// Choppiness
chop_atr = ta.atr(1)
chop_sum = math.sum(chop_atr, chop_len)
chop_hi = ta.highest(high, chop_len)
chop_lo = ta.lowest(low, chop_len)
chop_range = chop_hi - chop_lo
chop_val = chop_range > 0 ? 100 * math.log10(chop_sum / chop_range) / math.log10(chop_len) : 50
chop_ok = chop_val < chop_threshold

// Pivot SuperTrend
pst_pivot_len = 3
pst_atr_mult  = 2.0
pst_atr_len   = 10
pivot_high = ta.pivothigh(high, pst_pivot_len, pst_pivot_len)
pivot_low = ta.pivotlow(low, pst_pivot_len, pst_pivot_len)
var float last_pivot_high = na
var float last_pivot_low = na
if not na(pivot_high)
    last_pivot_high := pivot_high
if not na(pivot_low)
    last_pivot_low := pivot_low
pst_atr = ta.atr(pst_atr_len)
pivot_center = nz((last_pivot_high + last_pivot_low) / 2, close)
pst_upper = pivot_center + pst_atr_mult * pst_atr
pst_lower = pivot_center - pst_atr_mult * pst_atr
var float pst_upper_band = na
var float pst_lower_band = na
var int pst_direction = 1
pst_upper_band := nz(pst_upper_band[1])
pst_lower_band := nz(pst_lower_band[1])
if close > pst_upper_band[1]
    pst_direction := -1
if close < pst_lower_band[1]
    pst_direction := 1
pst_upper_band := pst_direction == 1 ? math.min(pst_upper, nz(pst_upper_band[1], pst_upper)) : pst_upper
pst_lower_band := pst_direction == -1 ? math.max(pst_lower, nz(pst_lower_band[1], pst_lower)) : pst_lower
pst_bullish = pst_direction == -1

// Banker Fund Flow
bff_pct = (close - ta.lowest(low, 27)) / (ta.highest(high, 27) - ta.lowest(low, 27)) * 100
bff_wsa1 = _wsa(bff_pct, 5, 1)
fund_flow_trend = (3 * bff_wsa1 - 2 * _wsa(bff_wsa1, 3, 1) - 50) * 1.032 + 50
bff_typical = (2 * close + high + low + open) / 5
bull_bear_line = ta.ema((bff_typical - ta.lowest(low, 34)) / (ta.highest(high, 34) - ta.lowest(low, 34)) * 100, 13)
banker_bull = ta.crossover(fund_flow_trend, bull_bear_line) and bull_bear_line < 25
banker_bull_recent = banker_bull or banker_bull[1] or banker_bull[2] or banker_bull[3]

// Swing Reversal
swing_lookback = 3
swing_bottom = low == ta.lowest(low, swing_lookback * 2 + 1) and close > open
swing_bottom_recent = swing_bottom or swing_bottom[1] or swing_bottom[2] or swing_bottom[3]

// === SCORING ===
bb_long_gate = close <= bb_lower or low <= bb_lower
confirm_rsi = rsi_val < rsi_os
confirm_banker = banker_bull_recent
confirm_vol = vol_spike
confirm_chop = chop_ok
confirm_swing = swing_bottom_recent
confirm_trend = pst_bullish
long_confirms = (confirm_rsi ? 1 : 0) + (confirm_banker ? 1 : 0) + (confirm_vol ? 1 : 0) + (confirm_chop ? 1 : 0) + (confirm_swing ? 1 : 0) + (confirm_trend ? 1 : 0)

// Cooldown
var int bars_since_signal = 100
bars_since_signal := bars_since_signal + 1
cooldown_ok = bars_since_signal >= cooldown_bars

// === SIGNAL ===
long_signal = bb_long_gate and long_confirms >= min_confirm and cooldown_ok and barstate.isconfirmed
if long_signal
    bars_since_signal := 0

// === EXECUTION ===
if long_signal
    sl = close - sl_atr_mult * atr_val
    tp = close + tp_atr_mult * atr_val
    strategy.entry("MR Long", strategy.long)
    strategy.exit("MR Exit", "MR Long", stop=sl, limit=tp)

// === VISUALS ===
p_bb_upper = plot(bb_upper, "BB Upper", color=color.new(color.blue, 60))
p_bb_lower = plot(bb_lower, "BB Lower", color=color.new(color.blue, 60))
plot(bb_mid, "BB Mid", color=color.new(color.orange, 40), linewidth=2)
fill(p_bb_upper, p_bb_lower, color=color.new(color.blue, 92))
plotshape(long_signal, "BUY", shape.triangleup, location.belowbar, color.lime, size=size.normal, text="BUY")
''',
    },
    "momentum_breakout": {
        "name": "Momentum Breakout",
        "description": "Buy breakouts above resistance with volume confirmation and trend alignment. Best for trending assets.",
        "timeframes": ["1H", "4H"],
        "direction": "LONG only",
        "entry": "Close above BB upper + EMA ribbon bullish + ADX > 25 + volume spike 2x",
        "exit": "TP 3x ATR / SL 2x ATR + trailing stop",
        "best_for": ["SOL", "RAY", "JTO"],
        "pine_code": '''// Momentum Breakout Template — customize parameters below
//@version=6
strategy("Momentum Breakout Template", overlay=true, max_bars_back=500,
         initial_capital=1000, default_qty_type=strategy.percent_of_equity,
         default_qty_value=10, commission_type=strategy.commission.percent,
         commission_value=0.1, slippage=2)

// === INPUTS ===
grp_ma = "Moving Averages"
ema_fast = input.int(9, "EMA Fast", group=grp_ma)
ema_mid  = input.int(21, "EMA Mid", group=grp_ma)
ema_slow = input.int(50, "EMA Slow", group=grp_ma)

grp_bb = "Bollinger Bands"
bb_len  = input.int(20, "Length", group=grp_bb)
bb_mult = input.float(2.0, "Multiplier", group=grp_bb)

grp_adx = "ADX"
adx_len   = input.int(14, "Length", group=grp_adx)
adx_min   = input.float(25, "Min ADX", group=grp_adx)

grp_vol = "Volume"
vol_ma_len     = input.int(20, "MA Length", group=grp_vol)
vol_spike_mult = input.float(2.0, "Spike Multiplier", group=grp_vol)

grp_risk = "Risk Management"
sl_atr_mult = input.float(2.0, "SL ATR Multiplier", group=grp_risk)
tp_atr_mult = input.float(3.0, "TP ATR Multiplier", group=grp_risk)
trail_atr   = input.float(1.5, "Trail ATR Offset", group=grp_risk)

grp_cool = "Cooldown"
cooldown_bars = input.int(5, "Bars Between Signals", minval=0, maxval=20, group=grp_cool)

// === CALCULATIONS ===
ema9  = ta.ema(close, ema_fast)
ema21 = ta.ema(close, ema_mid)
ema50 = ta.ema(close, ema_slow)
[bb_mid, bb_upper, bb_lower] = ta.bb(close, bb_len, bb_mult)
rsi_val = ta.rsi(close, 14)
atr_val = ta.atr(14)
vol_ma = ta.sma(volume, vol_ma_len)
vol_spike = volume > vol_ma * vol_spike_mult

// ADX
up_move   = high - high[1]
down_move = low[1] - low
plus_dm   = (up_move > down_move and up_move > 0) ? up_move : 0.0
minus_dm  = (down_move > up_move and down_move > 0) ? down_move : 0.0
true_range = ta.atr(1)
plus_di   = 100 * ta.rma(plus_dm, adx_len) / ta.rma(true_range, adx_len)
minus_di  = 100 * ta.rma(minus_dm, adx_len) / ta.rma(true_range, adx_len)
dx        = 100 * math.abs(plus_di - minus_di) / (plus_di + minus_di)
adx_val   = ta.rma(dx, adx_len)

// === SCORING ===
ema_bullish = ema9 > ema21 and ema21 > ema50
breakout = close > bb_upper
strong_trend = adx_val > adx_min
long_confirms = (ema_bullish ? 1 : 0) + (breakout ? 1 : 0) + (strong_trend ? 1 : 0) + (vol_spike ? 1 : 0)

// Cooldown
var int bars_since_signal = 100
bars_since_signal := bars_since_signal + 1
cooldown_ok = bars_since_signal >= cooldown_bars

// === SIGNAL ===
long_signal = breakout and ema_bullish and strong_trend and vol_spike and cooldown_ok and barstate.isconfirmed
if long_signal
    bars_since_signal := 0

// === EXECUTION ===
if long_signal
    sl = close - sl_atr_mult * atr_val
    tp = close + tp_atr_mult * atr_val
    strategy.entry("Breakout Long", strategy.long)
    strategy.exit("Breakout Exit", "Breakout Long", stop=sl, limit=tp, trail_offset=trail_atr * atr_val / syminfo.mintick, trail_points=trail_atr * atr_val / syminfo.mintick)

// === VISUALS ===
plot(ema9, "EMA 9", color=color.lime)
plot(ema21, "EMA 21", color=color.yellow)
plot(ema50, "EMA 50", color=color.orange)
plotshape(long_signal, "BUY", shape.triangleup, location.belowbar, color.lime, size=size.normal, text="BUY")
''',
    },
    "confluence_multi": {
        "name": "Confluence Multi-Factor",
        "description": "9-factor scoring system with ADX trend filter and HTF confirmation. Balanced approach for multiple tokens.",
        "timeframes": ["1H", "4H"],
        "direction": "LONG (shorts optional in bear markets)",
        "entry": "Bull score >= 5/9 + volume spike + HTF EMA bullish + ADX > 20",
        "exit": "TP 3.5x ATR / SL 1.5x ATR + trailing stop + breakeven",
        "best_for": ["BONK", "PYTH", "RAY", "WIF"],
        "pine_code": '''// Confluence Multi-Factor Template — customize parameters below
//@version=5
strategy("Confluence Multi-Factor Template", overlay=true, max_bars_back=500,
         initial_capital=100, default_qty_type=strategy.percent_of_equity,
         default_qty_value=25, commission_type=strategy.commission.percent,
         commission_value=0.1, slippage=3)

// === INPUTS ===
grp_entry = "Entry"
min_confluence = input.int(5, "Min Confluence (bull /9)", minval=2, maxval=9, group=grp_entry)

grp_ma = "Moving Averages"
ema_fast = input.int(9, "EMA Fast", group=grp_ma)
ema_mid  = input.int(21, "EMA Mid", group=grp_ma)
ema_slow = input.int(50, "EMA Slow", group=grp_ma)
sma_200  = input.int(200, "SMA 200", group=grp_ma)

grp_rsi = "RSI"
rsi_len = input.int(14, "Length", group=grp_rsi)
rsi_ob  = input.float(70, "Overbought", group=grp_rsi)
rsi_os  = input.float(30, "Oversold", group=grp_rsi)

grp_bb = "Bollinger Bands"
bb_len  = input.int(20, "Length", group=grp_bb)
bb_mult = input.float(2.0, "Multiplier", group=grp_bb)

grp_vol = "Volume"
vol_ma_len     = input.int(20, "MA Length", group=grp_vol)
vol_spike_mult = input.float(1.5, "Spike Multiplier", group=grp_vol)

grp_adx = "ADX"
adx_len       = input.int(14, "Length", group=grp_adx)
adx_threshold = input.float(20, "Minimum", group=grp_adx)

grp_htf = "Higher Timeframe"
htf_tf      = input.timeframe("240", "Timeframe", group=grp_htf)
htf_ema_len = input.int(50, "EMA Length", group=grp_htf)

grp_risk = "Risk Management"
sl_atr_mult = input.float(1.5, "SL ATR Multiplier", group=grp_risk)
tp_atr_mult = input.float(3.5, "TP ATR Multiplier", group=grp_risk)

grp_cool = "Cooldown"
cooldown_bars = input.int(3, "Bars Between Signals", minval=0, maxval=20, group=grp_cool)

// === CALCULATIONS ===
ema9   = ta.ema(close, ema_fast)
ema21  = ta.ema(close, ema_mid)
ema50  = ta.ema(close, ema_slow)
sma200 = ta.sma(close, sma_200)
rsi_val = ta.rsi(close, rsi_len)
[macd_line, signal_line, macd_hist] = ta.macd(close, 12, 26, 9)
[st_line, st_dir] = ta.supertrend(3.0, 10)
[bb_mid, bb_upper, bb_lower] = ta.bb(close, bb_len, bb_mult)
vwap_val = ta.vwap(hlc3)[1]
atr_val = ta.atr(14)
vol_ma = ta.sma(volume, vol_ma_len)
vol_spike = volume > vol_ma * vol_spike_mult

// ADX
up_move   = high - high[1]
down_move = low[1] - low
plus_dm   = (up_move > down_move and up_move > 0) ? up_move : 0.0
minus_dm  = (down_move > up_move and down_move > 0) ? down_move : 0.0
true_range = ta.atr(1)
plus_di   = 100 * ta.rma(plus_dm, adx_len) / ta.rma(true_range, adx_len)
minus_di  = 100 * ta.rma(minus_dm, adx_len) / ta.rma(true_range, adx_len)
dx        = 100 * math.abs(plus_di - minus_di) / (plus_di + minus_di)
adx_val   = ta.rma(dx, adx_len)
adx_trending = adx_val > adx_threshold

// HTF filter
htf_ema   = request.security(syminfo.tickerid, htf_tf, ta.ema(close, htf_ema_len)[1], lookahead=barmerge.lookahead_off)
htf_close = request.security(syminfo.tickerid, htf_tf, close[1], lookahead=barmerge.lookahead_off)
htf_bullish = htf_close > htf_ema

// Fibonacci
swing_high = ta.highest(high, 50)
swing_low  = ta.lowest(low, 50)
fib_range  = swing_high - swing_low
fib_618 = swing_high - fib_range * 0.618
fib_500 = swing_high - fib_range * 0.500
fib_382 = swing_high - fib_range * 0.382
near_fib = (close <= fib_618 * 1.01 and close >= fib_618 * 0.99) or
           (close <= fib_500 * 1.01 and close >= fib_500 * 0.99) or
           (close <= fib_382 * 1.01 and close >= fib_382 * 0.99)

// Ichimoku Cloud
tenkan   = (ta.highest(high, 9) + ta.lowest(low, 9)) / 2
kijun    = (ta.highest(high, 26) + ta.lowest(low, 26)) / 2
senkou_a = (tenkan + kijun) / 2
senkou_b = (ta.highest(high, 52) + ta.lowest(low, 52)) / 2
cloud_top = math.max(senkou_a[26], senkou_b[26])

// === BULL SCORE (max 9) ===
bull_score = (ema9 > ema21 and ema21 > ema50 and close > sma200 ? 1 : 0) +
             (ta.crossover(rsi_val, rsi_os) or (rsi_val > 40 and rsi_val < 65 and rsi_val > rsi_val[1]) ? 1 : 0) +
             (macd_hist > 0 and macd_hist > macd_hist[1] ? 1 : 0) +
             (st_dir < 0 ? 1 : 0) +
             (close < bb_mid and close > bb_lower and close > close[1] ? 1 : 0) +
             (close > vwap_val and close > cloud_top ? 1 : 0) +
             (near_fib and close > close[1] ? 1 : 0) +
             0 + 0  // Banker + MACD div slots (add if needed)

long_confirms = bull_score

// Cooldown
var int bars_since_signal = 100
bars_since_signal := bars_since_signal + 1
cooldown_ok = bars_since_signal >= cooldown_bars

// === SIGNAL ===
long_signal = bull_score >= min_confluence and vol_spike and htf_bullish and adx_trending and cooldown_ok and barstate.isconfirmed
if long_signal
    bars_since_signal := 0

// === EXECUTION ===
if long_signal
    sl = close - sl_atr_mult * atr_val
    tp = close + tp_atr_mult * atr_val
    strategy.entry("Conf Long", strategy.long)
    strategy.exit("Conf Exit", "Conf Long", stop=sl, limit=tp)

// === VISUALS ===
plot(ema9, "EMA 9", color=color.lime)
plot(ema21, "EMA 21", color=color.yellow)
plot(ema50, "EMA 50", color=color.orange)
plot(sma200, "SMA 200", color=color.red, linewidth=2)
plotshape(long_signal, "BUY", shape.triangleup, location.belowbar, color.lime, size=size.normal, text="BUY")
''',
    },
}


def get_templates() -> list[dict]:
    """Return template metadata (without full Pine code)."""
    return [
        {
            "id": tid,
            "name": t["name"],
            "description": t["description"],
            "timeframes": t["timeframes"],
            "direction": t["direction"],
            "entry": t["entry"],
            "exit": t["exit"],
            "best_for": t["best_for"],
        }
        for tid, t in TEMPLATES.items()
    ]


def get_template_code(template_id: str) -> str:
    """Return the full Pine code for a template."""
    t = TEMPLATES.get(template_id)
    if not t:
        return ""
    return t["pine_code"]
