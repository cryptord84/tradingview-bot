"""OHLCV fetcher: Binance US (primary) + CoinGecko (fallback for unlisted tokens)."""

import time
from typing import Optional

import httpx
import pandas as pd

BINANCE_BASE = "https://api.binance.us"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Binance US trading pairs
BINANCE_TOKENS = {
    "SOL":      "SOLUSDT",
    "ETH":      "ETHUSDT",
    "JTO":      "JTOUSDT",
    "WIF":      "WIFUSDT",
    "BONK":     "BONKUSDT",
    "ORCA":     "ORCAUSDT",
    "RENDER":   "RENDERUSDT",
    # Tier 1 additions — Solana-native, Jupiter-tradeable, Binance US data
    "JUP":      "JUPUSDT",
    "PENGU":    "PENGUUSDT",
    "FARTCOIN": "FARTCOINUSDT",
    "POPCAT":   "POPCATUSDT",
    "MEW":      "MEWUSDT",
    "PNUT":     "PNUTUSDT",
    "MOODENG":  "MOODENGUSDT",
}

# CoinGecko IDs for tokens not on Binance US
COINGECKO_TOKENS = {
    "PYTH":  "pyth-network",
    "RAY":   "raydium",
    "W":     "wormhole",
    # Tier 2 additions — Jupiter-tradeable, CoinGecko data only
    "HNT":   "helium",
    "DRIFT": "drift-protocol",
    "TNSR":  "tensor",
    # DOG (dog-go-to-the-moon-rune) — CoinGecko OHLC not available for Runes tokens
}

TOKENS = {**BINANCE_TOKENS, **{k: f"CG:{v}" for k, v in COINGECKO_TOKENS.items()}}

TIMEFRAMES = {
    "15m": "15m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
}

_BINANCE_MS = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _parse_klines(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("open_time")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


def fetch_binance(symbol: str, interval: str, bars: int = 2000) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV from Binance US with pagination support (> 1000 bars).
    Returns None if symbol not found.
    """
    chunks = []
    remaining = bars
    end_time = None  # fetch most-recent bars first, paginate backwards

    while remaining > 0:
        limit = min(remaining, 1000)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time

        try:
            resp = httpx.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=15)
            if resp.status_code in (400, 451):
                return None
            resp.raise_for_status()
        except Exception as e:
            print(f"\n  [binance] {symbol}/{interval}: {e}")
            return None

        raw = resp.json()
        if not raw:
            break

        chunk = _parse_klines(raw)
        chunks.insert(0, chunk)          # prepend so oldest is first
        remaining -= len(raw)

        if len(raw) < limit:             # no more history available
            break

        # Move end_time back for next page
        end_time = int(raw[0][0]) - 1   # one ms before first bar of this batch
        time.sleep(0.05)

    if not chunks:
        return None
    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def fetch_coingecko(cg_id: str, interval: str, retries: int = 3) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV from CoinGecko for tokens not on Binance US.

    CoinGecko free tier OHLC granularity:
      - days=1  → 30-min candles
      - days=7–max → 4-hour candles
    For 1H backtest we use market_chart (hourly, 90 days max free tier).
    For 4H backtest we use /ohlc with days=365.
    """
    for attempt in range(retries):
        if attempt > 0:
            wait = 15 * attempt
            print(f"\n  [coingecko] rate limited, waiting {wait}s…", end=" ", flush=True)
            time.sleep(wait)
        try:
            _do_fetch = True
            break
        except Exception:
            pass
    else:
        return None

    try:
        if interval == "1h":
            # market_chart gives hourly prices (close only) — we approximate OHLCV
            resp = httpx.get(
                f"{COINGECKO_BASE}/coins/{cg_id}/market_chart",
                params={"vs_currency": "usd", "days": "90", "interval": "hourly"},
                timeout=20,
            )
            if resp.status_code == 429:
                print(f"\n  [coingecko] 429 on {cg_id}, skipping")
                return None
            resp.raise_for_status()
            data = resp.json()
            prices = data.get("prices", [])
            volumes = data.get("total_volumes", [])
            if not prices:
                return None

            price_df  = pd.DataFrame(prices,  columns=["ts", "close"])
            volume_df = pd.DataFrame(volumes, columns=["ts", "volume"])
            df = price_df.merge(volume_df, on="ts", how="left")
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts")
            # Approximate high/low from close (conservative ±0.5%)
            df["open"]  = df["close"].shift(1).fillna(df["close"])
            df["high"]  = df["close"] * 1.005
            df["low"]   = df["close"] * 0.995
            df["volume"] = df["volume"].fillna(0)
            return df[["open", "high", "low", "close", "volume"]]

        else:  # 4H
            resp = httpx.get(
                f"{COINGECKO_BASE}/coins/{cg_id}/ohlc",
                params={"vs_currency": "usd", "days": "365"},
                timeout=20,
            )
            if resp.status_code == 429:
                print(f"\n  [coingecko] 429 on {cg_id}, skipping")
                return None
            resp.raise_for_status()
            raw = resp.json()
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts")
            df["volume"] = 0.0   # CoinGecko OHLC endpoint doesn't include volume
            return df[["open", "high", "low", "close", "volume"]]

    except Exception as e:
        print(f"\n  [coingecko] {cg_id}/{interval}: {e}")
        return None


def fetch_all(timeframe: str = "1H", bars: int = 2000) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for all tokens on a given timeframe.
    Uses Binance US for main tokens, CoinGecko for the rest.

    Returns a dict {token: DataFrame}, silently skipping unavailable tokens.
    """
    interval = TIMEFRAMES[timeframe]
    results = {}

    # Binance US tokens
    for token, pair in BINANCE_TOKENS.items():
        print(f"  {token} ({pair})…", end=" ", flush=True)
        df = fetch_binance(pair, interval, bars)
        if df is not None and len(df) >= 50:
            results[token] = df
            print(f"{len(df)} bars")
        else:
            print("skipped")
        time.sleep(0.1)

    # CoinGecko fallback tokens
    for token, cg_id in COINGECKO_TOKENS.items():
        print(f"  {token} (CoinGecko:{cg_id})…", end=" ", flush=True)
        df = fetch_coingecko(cg_id, interval)
        if df is not None and len(df) >= 50:
            results[token] = df
            print(f"{len(df)} bars")
        else:
            print("skipped")
        time.sleep(3.0)  # CoinGecko free tier: ~10-30 req/min

    return results
