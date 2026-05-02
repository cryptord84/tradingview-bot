"""OHLCV fetcher: Binance US (primary) + Coinbase + OKX + CoinGecko (fallbacks)."""

import time
from typing import Optional

import httpx
import pandas as pd

BINANCE_BASE  = "https://api.binance.us"
COINBASE_BASE = "https://api.exchange.coinbase.com"
OKX_BASE      = "https://www.okx.com"
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
    # Tier 3 additions (2026-05-02) — Jupiter-tradeable Solana tokens with Binance US data
    "ME":       "MEUSDT",
    # EVM research additions (2026-05-02) — multi-year Binance.US history.
    # NOT yet executable: no Solana mints in trade_engine.py. Backtest-only until
    # an EVM wallet integration lands. If a WF passer emerges here, that informs
    # the EVM expansion decision.
    "LINK":     "LINKUSDT",   # DeFi blue chip, 7+ years
    "UNI":      "UNIUSDT",    # DeFi blue chip
    "AAVE":     "AAVEUSDT",   # DeFi blue chip
    "MKR":      "MKRUSDT",    # DeFi blue chip
    "COMP":     "COMPUSDT",   # DeFi blue chip
    "LDO":      "LDOUSDT",    # Liquid staking
    "AVAX":     "AVAXUSDT",   # L1
    "ATOM":     "ATOMUSDT",   # L1
    "NEAR":     "NEARUSDT",   # L1
    "ARB":      "ARBUSDT",    # L2
    "OP":       "OPUSDT",     # L2
    "MATIC":    "MATICUSDT",  # L2
    "SHIB":     "SHIBUSDT",   # Established meme
    "PEPE":     "PEPEUSDT",   # Established meme
    "FLOKI":    "FLOKIUSDT",  # Established meme
    # Cosmos research additions (2026-05-02) — Binance.US-listed Cosmos ecosystem
    "TIA":      "TIAUSDT",    # Celestia (modular blockchain)
    "KAVA":     "KAVAUSDT",   # Kava (Cosmos DeFi)
}

# Coinbase Advanced — used for Solana tokens not on Binance US.
# Coinbase has no native 4H granularity (only 1m/5m/15m/1H/6H/1D).
# We fetch 1H and resample to 4H downstream.
# Note: tokens here have limited Coinbase history (~30-60 days). WBTC was removed
# 2026-05-02 — Coinbase data ended Dec 2024 (delisted/stale).
COINBASE_TOKENS = {
    "KMNO": "KMNO-USD",
    "DBR":  "DBR-USD",
    # EVM research addition (2026-05-02) — Binance.US doesn't list INJ
    "INJ":  "INJ-USD",
}

# OKX — used for Solana memecoins not on Binance US or Coinbase.
# Note: GRASS removed 2026-05-02 — only ~49 bars on 4H, too thin for WF.
OKX_TOKENS = {
    "ACT":   "ACT-USDT",
    "GOAT":  "GOAT-USDT",
    "ZEUS":  "ZEUS-USDT",  # ⚠ thin liquidity (~1.1% PI on $1k); Tier C sizing only
    # Cosmos research addition (2026-05-02) — DYDX not on Binance.US
    "DYDX":  "DYDX-USDT",
}

# CoinGecko IDs for tokens not on Binance US, Coinbase, or OKX.
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

TOKENS = {
    **BINANCE_TOKENS,
    **{k: f"CB:{v}"  for k, v in COINBASE_TOKENS.items()},
    **{k: f"OKX:{v}" for k, v in OKX_TOKENS.items()},
    **{k: f"CG:{v}"  for k, v in COINGECKO_TOKENS.items()},
}

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


def fetch_coinbase(pair: str, interval: str, bars: int = 2000) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Coinbase Advanced with pagination.

    Coinbase granularity (seconds): 60, 300, 900, 3600, 21600, 86400.
    No native 4H — we fetch 1H and resample 4×.
    Max 300 bars per request → paginate backwards by ~300 bars at a time.
    """
    granularity_map = {"15m": 900, "1h": 3600, "1d": 86400}
    needs_resample_4h = interval == "4h"
    fetch_interval = "1h" if needs_resample_4h else interval
    granularity = granularity_map.get(fetch_interval)
    if granularity is None:
        return None

    # If resampling 1H→4H, we need 4× the bars
    target_bars = bars * 4 if needs_resample_4h else bars

    chunks = []
    end = int(time.time())
    remaining = target_bars
    request_secs = 300 * granularity  # 300 bars per request
    first_call = True

    while remaining > 0:
        start = end - request_secs
        # First call without start/end gets Coinbase's default "latest 300" — works
        # for tokens where explicit time windows reject (some new listings).
        if first_call:
            params = {"granularity": granularity}
            first_call = False
        else:
            params = {
                "granularity": granularity,
                "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                "end":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
            }

        try:
            resp = httpx.get(f"{COINBASE_BASE}/products/{pair}/candles",
                             params=params, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        except Exception as e:
            print(f"\n  [coinbase] {pair}/{interval}: {e}")
            return None if not chunks else None  # bail on error

        raw = resp.json()
        if not raw:
            break
        # Coinbase returns [time, low, high, open, close, volume], newest first
        df = pd.DataFrame(raw, columns=["ts", "low", "high", "open", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
        df = df.set_index("ts").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        chunks.insert(0, df[["open", "high", "low", "close", "volume"]])
        remaining -= len(raw)
        # After first call, set end to the oldest ts we got so next page goes further back
        end = int(df.index[0].timestamp())
        if len(raw) < 300:
            break
        time.sleep(0.35)  # Coinbase rate limit: 3 req/s public

    if not chunks:
        return None
    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="first")]

    if needs_resample_4h:
        df = df.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    return df


def fetch_okx(pair: str, interval: str, bars: int = 2000) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from OKX with pagination via history-candles.

    OKX bar values: 15m, 1H, 4H, 1D (uppercase).
    Max 100 bars per request.
    """
    bar_map = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    bar = bar_map.get(interval)
    if bar is None:
        return None

    chunks = []
    after_ts = None  # OKX uses 'after' for pagination (older than this ts)
    fetched = 0
    max_pages = max(1, (bars + 99) // 100) + 2

    for _ in range(max_pages):
        params = {"instId": pair, "bar": bar, "limit": 100}
        if after_ts is not None:
            params["after"] = str(after_ts)

        try:
            resp = httpx.get(f"{OKX_BASE}/api/v5/market/history-candles",
                             params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "0":
                if not chunks:  # first call failed
                    print(f"\n  [okx] {pair}/{interval}: {data.get('msg', 'unknown')[:60]}")
                    return None
                break
            raw = data.get("data", [])
        except Exception as e:
            print(f"\n  [okx] {pair}/{interval}: {e}")
            return None if not chunks else None  # bail if any error

        if not raw:
            break

        # OKX shape: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        df = pd.DataFrame(raw, columns=[
            "ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm",
        ])
        df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
        df = df.set_index("ts").sort_index()
        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = df[col].astype(float)
        df = df.rename(columns={"vol": "volume"})
        chunks.insert(0, df[["open", "high", "low", "close", "volume"]])
        fetched += len(raw)

        if fetched >= bars:
            break
        # Paginate: fetch older bars by setting after = oldest ts in this batch
        after_ts = int(raw[-1][0])
        time.sleep(0.15)  # OKX rate limit ~20 req / 2s public

    if not chunks:
        return None
    df = pd.concat(chunks).sort_index()
    return df[~df.index.duplicated(keep="first")]


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

    # Coinbase tokens (no native 4H — fetch_coinbase resamples 1H→4H)
    for token, pair in COINBASE_TOKENS.items():
        print(f"  {token} (Coinbase:{pair})…", end=" ", flush=True)
        df = fetch_coinbase(pair, interval, bars)
        if df is not None and len(df) >= 50:
            results[token] = df
            print(f"{len(df)} bars")
        else:
            print("skipped")
        time.sleep(0.4)

    # OKX tokens (paginated, 100 bars/request)
    for token, pair in OKX_TOKENS.items():
        print(f"  {token} (OKX:{pair})…", end=" ", flush=True)
        df = fetch_okx(pair, interval, bars)
        if df is not None and len(df) >= 50:
            results[token] = df
            print(f"{len(df)} bars")
        else:
            print("skipped")
        time.sleep(0.2)

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
