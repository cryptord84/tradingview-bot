"""
Strategy signal generators.

Each function takes a standard OHLCV DataFrame and returns a signals DataFrame
with boolean columns: entry_long, exit_long, entry_short, exit_short.
"""

import pandas as pd

from backtesting.indicators import (
    adx, atr, bollinger_squeeze, donchian, ema, macd, rolling_vwap, sma,
    stoch_rsi, supertrend, rsi, swing_highs_lows,
)


def _vol_filter(volume: pd.Series, period: int = 20, mult: float = 1.2) -> pd.Series:
    """True where volume > mult × SMA(volume)."""
    return volume > sma(volume, period) * mult


def _signals(n: int, index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        False,
        index=index,
        columns=["entry_long", "exit_long", "entry_short", "exit_short"],
    )


# ── 1. Supertrend ─────────────────────────────────────────────────────────────

def strategy_supertrend(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    ATR-based trend flip with volume filter.
    Long on bullish flip, Short on bearish flip.
    """
    st_line, direction = supertrend(df["high"], df["low"], df["close"], factor=3.0, period=10)
    vol_ok = _vol_filter(df["volume"])

    bull_flip = (direction == -1) & (direction.shift(1) == 1)
    bear_flip = (direction == 1)  & (direction.shift(1) == -1)

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = bull_flip & vol_ok
    sig["exit_long"]   = bear_flip
    sig["entry_short"] = bear_flip & vol_ok & enable_short
    sig["exit_short"]  = bull_flip
    return sig


# ── 2. Donchian Breakout ──────────────────────────────────────────────────────

def strategy_donchian(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    20-bar channel breakout with volume confirmation.
    Long on close > prior-bar upper band, Short on close < prior-bar lower band.
    Exit on mid-channel cross.
    """
    upper, mid, lower = donchian(df["high"], df["low"], period=20)
    vol_ok = _vol_filter(df["volume"], mult=1.5)

    # Use prior bar's bands to avoid look-ahead
    upper_prev = upper.shift(1)
    lower_prev = lower.shift(1)
    mid_prev   = mid.shift(1)

    long_entry  = (df["close"] > upper_prev) & vol_ok
    short_entry = (df["close"] < lower_prev) & vol_ok & enable_short

    # Exit longs below mid, exit shorts above mid
    long_exit  = df["close"] < mid_prev
    short_exit = df["close"] > mid_prev

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 3. EMA Ribbon ─────────────────────────────────────────────────────────────

def strategy_ema_ribbon(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    3/8/21/55 EMA stack alignment.
    Long when all 4 EMAs aligned bullish (3>8>21>55).
    Short when all 4 EMAs aligned bearish (3<8<21<55).
    Exit when alignment breaks.
    """
    e3  = ema(df["close"], 3)
    e8  = ema(df["close"], 8)
    e21 = ema(df["close"], 21)
    e55 = ema(df["close"], 55)

    bull_aligned = (e3 > e8) & (e8 > e21) & (e21 > e55)
    bear_aligned = (e3 < e8) & (e8 < e21) & (e21 < e55)

    # Entry on first bar of alignment; exit when no longer aligned
    long_entry  = bull_aligned & ~bull_aligned.shift(1, fill_value=False)
    short_entry = bear_aligned & ~bear_aligned.shift(1, fill_value=False) & enable_short

    long_exit  = ~bull_aligned
    short_exit = ~bear_aligned

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 4. VWAP Deviation ─────────────────────────────────────────────────────────

def strategy_vwap_dev(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    Mean-reversion off rolling VWAP ±2σ bands.
    Long when price crosses back above lower band.
    Short when price crosses back below upper band.
    Exit at VWAP.
    """
    vwap, upper, lower = rolling_vwap(
        df["high"], df["low"], df["close"], df["volume"], period=20
    )

    close = df["close"]
    prev_close = close.shift(1)

    # Cross back above lower band (was below, now above) → long entry
    long_entry  = (prev_close < lower.shift(1)) & (close > lower)
    # Cross back below upper band (was above, now below) → short entry
    short_entry = (prev_close > upper.shift(1)) & (close < upper) & enable_short

    # Exit at VWAP cross
    long_exit  = close >= vwap
    short_exit = close <= vwap

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 5. Stoch RSI Cross ────────────────────────────────────────────────────────

def strategy_stoch_rsi(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    Stochastic RSI K/D crossover with zone filter.
    Long: K crosses above D while both < 20 (oversold) AND RSI < 50.
    Short: K crosses below D while both > 80 (overbought) AND RSI > 50.
    Exit: opposite zone crossover.
    """
    k, d = stoch_rsi(df["close"])
    rsi_val = rsi(df["close"], period=14)

    k_prev = k.shift(1)
    d_prev = d.shift(1)

    # K crosses above D
    k_cross_up   = (k > d) & (k_prev <= d_prev)
    # K crosses below D
    k_cross_down = (k < d) & (k_prev >= d_prev)

    long_entry  = k_cross_up   & (k < 20) & (rsi_val < 50)
    short_entry = k_cross_down & (k > 80) & (rsi_val > 50) & enable_short

    # Exit when K crosses back into opposite territory
    long_exit  = k_cross_down & (k > 80)
    short_exit = k_cross_up   & (k < 20)

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 6. Fair Value Gap (FVG) ───────────────────────────────────────────────────

def strategy_fvg(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    ICT Fair Value Gap — 3-candle imbalance momentum play.

    Bullish FVG: current low > high 2 bars ago (upward gap formed).
    Bearish FVG: current high < low 2 bars ago (downward gap formed).

    Entry: immediately on bar after FVG forms (momentum trade in gap direction).
    Volume filter: volume surge confirms institutional intent.
    Exit: price closes back inside the gap (gap filled) or opposite FVG forms.
    """
    high = df["high"]
    low  = df["low"]

    # Detect FVGs on the current bar (formed by bar[i-2], bar[i-1], bar[i])
    bull_fvg = low  > high.shift(2)   # gap up:   low[i] > high[i-2]
    bear_fvg = high < low.shift(2)    # gap down: high[i] < low[i-2]

    vol_ok = _vol_filter(df["volume"], mult=1.5)

    # Enter on the bar AFTER the FVG forms (shift(1) = detected last bar)
    long_entry  = bull_fvg.shift(1, fill_value=False) & vol_ok
    short_entry = bear_fvg.shift(1, fill_value=False) & vol_ok & enable_short

    # Exit: close back inside the gap (gap filled) or opposite signal
    # For bull FVG exit: close drops back below high[i-2] at time of entry
    # Approximate with: close < rolling min of lows over 3 bars
    fvg_ref_high = high.shift(2)   # reference for bull FVG lower bound
    fvg_ref_low  = low.shift(2)    # reference for bear FVG upper bound

    long_exit  = (df["close"] < fvg_ref_high) | bear_fvg
    short_exit = (df["close"] > fvg_ref_low)  | bull_fvg

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 7. MACD + Volume Momentum ─────────────────────────────────────────────────

def strategy_macd_vol(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    MACD crossover + expanding histogram + volume surge + EMA-200 trend filter.

    Long:  MACD line crosses above signal AND histogram expanding bullish
           AND volume surge AND price above EMA-200.
    Short: MACD crosses below signal AND histogram expanding bearish
           AND volume surge AND price below EMA-200.
    Exit:  opposite MACD cross.
    """
    macd_line, signal_line, hist = macd(df["close"], fast=12, slow=26, signal=9)
    ema200 = ema(df["close"], 200)
    vol_ok = _vol_filter(df["volume"], mult=1.3)

    bull_cross = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))
    bear_cross = (macd_line < signal_line) & (macd_line.shift(1) >= signal_line.shift(1))

    hist_bullish = hist > hist.shift(1)   # histogram growing (momentum expanding)
    hist_bearish = hist < hist.shift(1)

    long_entry  = bull_cross & hist_bullish & vol_ok & (df["close"] > ema200)
    short_entry = bear_cross & hist_bearish & vol_ok & (df["close"] < ema200) & enable_short

    long_exit  = bear_cross
    short_exit = bull_cross

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── 8. Liquidity Sweep Reversal ───────────────────────────────────────────────

def strategy_liq_sweep(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    ICT Liquidity Sweep — stop-hunt reversal.

    Identifies when price wicks past a significant swing high/low (sweeping stops),
    then closes back inside the range, signalling institutional reversal.

    Bullish sweep: current low < swing low (10-bar) BUT close > swing low → long.
    Bearish sweep: current high > swing high (10-bar) BUT close < swing high → short.
    Volume filter: sweep must occur on above-average volume.
    Exit: opposite sweep forms.
    """
    swing_high, swing_low = swing_highs_lows(df["high"], df["low"], lookback=10)

    # Sweep of lows: wick through swing low but close above it = bull reversal
    bull_sweep = (df["low"] < swing_low) & (df["close"] > swing_low)
    # Sweep of highs: wick through swing high but close below it = bear reversal
    bear_sweep = (df["high"] > swing_high) & (df["close"] < swing_high)

    vol_ok = _vol_filter(df["volume"], mult=1.2)

    long_entry  = bull_sweep & vol_ok
    short_entry = bear_sweep & vol_ok & enable_short

    # Exit on opposite sweep
    long_exit  = bear_sweep
    short_exit = bull_sweep

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── Regime-Filtered Strategy Wrappers ────────────────────────────────────────

def _apply_regime_filter(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    regime: str,  # "trend" or "mean_revert"
    adx_trend_thresh: float = 25.0,
    adx_range_thresh: float = 20.0,
    squeeze_block: bool = True,
) -> pd.DataFrame:
    """Apply regime filter to suppress entries in unfavorable conditions.

    regime="trend": only allow entries when ADX > adx_trend_thresh (strong trend)
    regime="mean_revert": only allow entries when ADX < adx_range_thresh (ranging)

    squeeze_block: if True, block entries during Bollinger squeeze (low vol chop)
    """
    adx_val = adx(df["high"], df["low"], df["close"], period=14)

    if regime == "trend":
        regime_ok = adx_val > adx_trend_thresh
    else:  # mean_revert
        regime_ok = adx_val < adx_range_thresh

    if squeeze_block:
        in_squeeze = bollinger_squeeze(df["close"])
        regime_ok = regime_ok & ~in_squeeze

    # Only gate entries, not exits
    signals["entry_long"]  = signals["entry_long"] & regime_ok
    signals["entry_short"] = signals["entry_short"] & regime_ok
    return signals


def _apply_higher_tf_filter(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    htf_multiplier: int = 4,
    ema_length: int = 20,
) -> pd.DataFrame:
    """Gate entries on higher-TF EMA slope (trend-context filter).

    Resamples OHLC to htf_multiplier × current TF, computes EMA(ema_length),
    and allows only:
      - longs  when HTF EMA is rising
      - shorts when HTF EMA is falling

    Current TF is inferred from the median gap of the df index.
    Safe for look-ahead: resample uses closed='right', label='right', so the
    HTF bar is only available AFTER it closes; we then ffill onto df.
    """
    if len(df) < (ema_length + 1) * htf_multiplier:
        return signals  # not enough history, pass through

    gap = df.index.to_series().diff().median()
    if pd.isna(gap) or gap.total_seconds() <= 0:
        return signals
    htf_freq = gap * htf_multiplier

    htf = df.resample(htf_freq, closed="right", label="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    if len(htf) < ema_length + 1:
        return signals

    htf_ema = ema(htf["close"], ema_length)
    # Cast to int so ffill/fillna stays numeric and avoids FutureWarning on bool downcast
    ema_rising  = ((htf_ema > htf_ema.shift(1)).astype("int8")).shift(1, fill_value=0)
    ema_falling = ((htf_ema < htf_ema.shift(1)).astype("int8")).shift(1, fill_value=0)

    rising_on_df  = ema_rising.reindex(df.index,  method="ffill").fillna(0).astype(bool)
    falling_on_df = ema_falling.reindex(df.index, method="ffill").fillna(0).astype(bool)

    signals = signals.copy()
    signals["entry_long"]  = signals["entry_long"]  & rising_on_df.to_numpy()
    signals["entry_short"] = signals["entry_short"] & falling_on_df.to_numpy()
    return signals


def with_htf_filter(strat_fn, htf_multiplier: int = 4, ema_length: int = 20):
    """Wrap a strategy fn with a higher-TF EMA-slope entry filter."""
    def _wrapped(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
        sig = strat_fn(df, enable_short=enable_short)
        return _apply_higher_tf_filter(df, sig, htf_multiplier, ema_length)
    _wrapped.__name__ = f"{strat_fn.__name__}_htf"
    return _wrapped


def strategy_supertrend_regime(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """Supertrend with ADX trend filter — only trades in trending markets."""
    sig = strategy_supertrend(df, enable_short)
    return _apply_regime_filter(df, sig, regime="trend")


def strategy_donchian_regime(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """Donchian with ADX trend filter — breakouts only in trending markets."""
    sig = strategy_donchian(df, enable_short)
    return _apply_regime_filter(df, sig, regime="trend")


def strategy_vwap_dev_regime(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """VWAP Dev with ADX range filter — mean reversion only in ranging markets."""
    sig = strategy_vwap_dev(df, enable_short)
    return _apply_regime_filter(df, sig, regime="mean_revert")


def strategy_stoch_rsi_regime(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """Stoch RSI with ADX range filter — oscillator trades in ranging markets."""
    sig = strategy_stoch_rsi(df, enable_short)
    return _apply_regime_filter(df, sig, regime="mean_revert")


def strategy_ema_ribbon_regime(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """EMA Ribbon with ADX trend filter — alignment matters when trending."""
    sig = strategy_ema_ribbon(df, enable_short)
    return _apply_regime_filter(df, sig, regime="trend")


# ── RSI Divergence (ported from live TradingView indicator) ──────────────────

def strategy_rsi_divergence(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    RSI divergence — price makes new low but RSI makes higher low (bullish),
    or price makes new high but RSI makes lower high (bearish).

    Uses 14-period RSI and 10-bar swing detection.
    Exit: RSI crosses back through neutral zone.
    """
    rsi_val = rsi(df["close"], period=14)
    swing_high, swing_low = swing_highs_lows(df["high"], df["low"], lookback=10)

    # Price at new swing low but RSI higher than at previous swing low
    price_lower_low = df["low"] < swing_low
    rsi_higher_low = rsi_val > rsi_val.shift(10)
    bull_div = price_lower_low & rsi_higher_low & (rsi_val < 40)

    # Price at new swing high but RSI lower
    price_higher_high = df["high"] > swing_high
    rsi_lower_high = rsi_val < rsi_val.shift(10)
    bear_div = price_higher_high & rsi_lower_high & (rsi_val > 60)

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = bull_div
    sig["exit_long"]   = rsi_val > 55
    sig["entry_short"] = bear_div & enable_short
    sig["exit_short"]  = rsi_val < 45
    return sig


# ── Mean Reversion (ported from live TradingView Mean Rev indicator) ─────────

def strategy_mean_reversion(df: pd.DataFrame, enable_short: bool = True) -> pd.DataFrame:
    """
    Multi-indicator mean reversion strategy.

    Entry: RSI oversold (<30) + price below lower Bollinger Band + volume spike.
    Exit: price returns to 20-SMA (the mean).
    """
    rsi_val = rsi(df["close"], period=14)
    mean = sma(df["close"], 20)
    std = df["close"].rolling(20).std()
    bb_lower = mean - 2 * std
    bb_upper = mean + 2 * std
    vol_ok = _vol_filter(df["volume"], period=20, mult=1.2)

    long_entry  = (rsi_val < 30) & (df["close"] < bb_lower) & vol_ok
    short_entry = (rsi_val > 70) & (df["close"] > bb_upper) & vol_ok & enable_short

    long_exit  = df["close"] >= mean
    short_exit = df["close"] <= mean

    sig = _signals(len(df), df.index)
    sig["entry_long"]  = long_entry
    sig["exit_long"]   = long_exit
    sig["entry_short"] = short_entry
    sig["exit_short"]  = short_exit
    return sig


# ── Registry ──────────────────────────────────────────────────────────────────

STRATEGIES = {
    # Original strategies (no regime filter)
    "Supertrend":   strategy_supertrend,
    "Donchian":     strategy_donchian,
    "EMA Ribbon":   strategy_ema_ribbon,
    "VWAP Dev":     strategy_vwap_dev,
    "Stoch RSI":    strategy_stoch_rsi,
    "FVG":          strategy_fvg,
    "MACD Vol":     strategy_macd_vol,
    "Liq Sweep":    strategy_liq_sweep,
    # Regime-filtered variants
    "ST+ADX":       strategy_supertrend_regime,
    "Donch+ADX":    strategy_donchian_regime,
    "EMA+ADX":      strategy_ema_ribbon_regime,
    "VWAP+ADX":     strategy_vwap_dev_regime,
    "StRSI+ADX":    strategy_stoch_rsi_regime,
    # Ported from live TradingView indicators
    "RSI Div":      strategy_rsi_divergence,
    "Mean Rev":     strategy_mean_reversion,
}
