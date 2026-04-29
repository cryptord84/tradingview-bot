"""Technical indicator calculations using pandas + numpy only."""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def stoch_rsi(
    close: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Returns (K, D) lines, both in [0, 100]."""
    rsi_vals = rsi(close, rsi_period)
    rsi_min = rsi_vals.rolling(stoch_period).min()
    rsi_max = rsi_vals.rolling(stoch_period).max()
    raw_k = (rsi_vals - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan) * 100
    k = raw_k.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    factor: float = 3.0,
    period: int = 10,
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (supertrend_line, direction) where:
      direction == -1  →  bullish (price above line)
      direction ==  1  →  bearish (price below line)
    """
    atr_vals = atr(high, low, close, period).values
    hl2 = ((high + low) / 2).values
    close_arr = close.values
    n = len(close_arr)

    basic_upper = hl2 + factor * atr_vals
    basic_lower = hl2 - factor * atr_vals

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    st = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)

    # Initialise first valid bar (after ATR warmup)
    warmup = period + 1
    if warmup >= n:
        return pd.Series(st, index=close.index), pd.Series(direction, index=close.index)

    st[warmup] = basic_upper[warmup]
    direction[warmup] = 1

    for i in range(warmup + 1, n):
        # Tighten upper band downward; only loosen if price was above it last bar
        if basic_upper[i] < final_upper[i - 1] or close_arr[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Tighten lower band upward; only loosen if price was below it last bar
        if basic_lower[i] > final_lower[i - 1] or close_arr[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Direction flip logic
        if direction[i - 1] == 1:  # previously bearish
            if close_arr[i] > final_upper[i]:
                direction[i] = -1        # flipped bullish
                st[i] = final_lower[i]
            else:
                direction[i] = 1
                st[i] = final_upper[i]
        else:  # previously bullish
            if close_arr[i] < final_lower[i]:
                direction[i] = 1         # flipped bearish
                st[i] = final_upper[i]
            else:
                direction[i] = -1
                st[i] = final_lower[i]

    return pd.Series(st, index=close.index), pd.Series(direction, index=close.index)


def donchian(
    high: pd.Series, low: pd.Series, period: int = 20
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper_band, mid_band, lower_band) over rolling window."""
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    mid = (upper + lower) / 2
    return upper, mid, lower


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def swing_highs_lows(
    high: pd.Series, low: pd.Series, lookback: int = 10
) -> tuple[pd.Series, pd.Series]:
    """
    Rolling swing high/low over `lookback` bars (prior bars only — no look-ahead).
    Returns (swing_high, swing_low).
    """
    return (
        high.rolling(lookback).max().shift(1),
        low.rolling(lookback).min().shift(1),
    )


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average Directional Index — measures trend strength (0-100).
    ADX > 25 = trending, ADX < 20 = ranging/choppy.
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    # Zero out when the other DM is larger
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_val = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def bollinger_squeeze(
    close: pd.Series, bb_period: int = 20, kc_period: int = 20, kc_mult: float = 1.5
) -> pd.Series:
    """Bollinger Band squeeze detector.
    Returns True when BB is inside Keltner Channel (low volatility squeeze).
    """
    # Bollinger Bands
    bb_mid = sma(close, bb_period)
    bb_std = close.rolling(bb_period).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    # Keltner Channel
    high_low_range = close.rolling(kc_period).apply(
        lambda x: pd.Series(x).diff().abs().mean(), raw=True
    )
    kc_upper = bb_mid + kc_mult * high_low_range
    kc_lower = bb_mid - kc_mult * high_low_range

    # Squeeze = BB inside KC
    return (bb_lower > kc_lower) & (bb_upper < kc_upper)


def rolling_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Rolling VWAP with ±2 std-dev bands.
    Returns (vwap, upper_band, lower_band).
    """
    typical = (high + low + close) / 3
    vwap_line = (typical * volume).rolling(period).sum() / volume.rolling(period).sum()
    std = typical.rolling(period).std()
    return vwap_line, vwap_line + 2 * std, vwap_line - 2 * std
