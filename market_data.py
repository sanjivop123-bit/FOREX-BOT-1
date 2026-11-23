"""
market_data.py - Stage 1: live forex candle provider (Twelve Data).

Only fetches and VALIDATES data. It never invents prices.
Env vars: TWELVE_DATA_API_KEY (required), TD_RPM (default 8), TD_DAILY_LIMIT (default 800, 0 = unlimited)

Main functions:
    get_candles(symbol, timeframe)      -> pandas DataFrame of CLOSED candles (UTC index)
    get_candles_async(symbol, timeframe)
    market_status()                     -> dict (is forex market open now?)
    normalize_symbol("eurusd")          -> "EUR/USD"
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

log = logging.getLogger("market_data")

API_URL = "https://api.twelvedata.com/time_series"
TD_KEY = (os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY") or "").strip()

# bot timeframe -> (Twelve Data interval, seconds per candle)
TIMEFRAMES = {
    "1m": ("1min", 60),
    "5m": ("5min", 300),
    "15m": ("15min", 900),
    "30m": ("30min", 1800),
    "1h": ("1h", 3600),
    "4h": ("4h", 14400),
}

DEFAULT_ASSETS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "USD/CHF", "EUR/AUD", "GBP/AUD",
]

MIN_CANDLES = 60          # fewer closed candles than this = not enough data
MAX_RETRIES = 3


class DataError(Exception):
    """Base error: data could not be obtained or is not trustworthy."""


class RateLimitError(DataError):
    pass


class StaleDataError(DataError):
    pass


class InvalidDataError(DataError):
    pass


def _int_env(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class RateLimiter:
    """Thread-safe: spaces calls out (requests/minute) and counts a daily budget."""

    def __init__(self, rpm, daily_limit):
        self.gap = 60.0 / max(rpm, 1)
        self.daily_limit = daily_limit
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self._day = datetime.now(timezone.utc).date()
        self._used = 0

    def wait(self):
        with self._lock:
            today = datetime.now(timezone.utc).date()
            if today != self._day:
                self._day, self._used = today, 0
            if self.daily_limit and self._used >= self.daily_limit:
                raise RateLimitError("Daily API budget used up (%d calls)" % self._used)
            now = time.monotonic()
            delay = max(0.0, self._next_ok - now)
            self._next_ok = max(now, self._next_ok) + self.gap
            self._used += 1
        if delay:
            time.sleep(delay)

    @property
    def used_today(self):
        return self._used


limiter = RateLimiter(_int_env("TD_RPM", 8), _int_env("TD_DAILY_LIMIT", 800))


def normalize_symbol(raw):
    """'eurusd', 'EUR-USD', 'eur/usd' -> 'EUR/USD'. Returns None if invalid."""
    t = (raw or "").upper().replace("/", "").replace("-", "").replace("_", "").strip()
    if len(t) != 6 or not t.isalpha():
        return None
    return t[:3] + "/" + t[3:]


def market_status(now=None):
    """Forex trades roughly Sun 22:00 UTC -> Fri 22:00 UTC. Holidays are not covered."""
    now = now or datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour          # Mon=0 ... Sun=6
    closed = wd == 5 or (wd == 4 and hr >= 22) or (wd == 6 and hr < 22)
    return {
        "open": not closed,
        "session": "Forex market OPEN" if not closed else "Forex market CLOSED (weekend)",
        "utc_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def parse_candles(payload, tf_seconds):
    """Turn a Twelve Data JSON response into a clean DataFrame. Raises on bad data."""
    if not isinstance(payload, dict):
        raise InvalidDataError("Unexpected response from data provider")
    if payload.get("status") == "error" or "values" not in payload:
        code = payload.get("code")
        msg = payload.get("message", "unknown provider error")
        if code == 429:
            raise RateLimitError(msg)
        raise InvalidDataError("Provider error %s: %s" % (code, msg))

    df = pd.DataFrame(payload["values"])
    need = {"datetime", "open", "high", "low", "close"}
    if df.empty or not need.issubset(df.columns):
        raise InvalidDataError("Provider returned no usable candles")

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = float("nan")          # forex volume is often unavailable: stay honest

    df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
    df = df.drop_duplicates(subset="datetime").sort_values("datetime").set_index("datetime")

    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    if bad.any():
        df = df[~bad]                        # drop impossible candles
    if df.empty:
        raise InvalidDataError("All candles failed validation")
    return df[["open", "high", "low", "close", "volume"]]


def closed_only(df, tf_seconds, now=None):
    """Remove the candle that is still forming (its close time is in the future)."""
    now = now or datetime.now(timezone.utc)
    cutoff = pd.Timestamp(now) - pd.Timedelta(seconds=tf_seconds)
    return df[df.index <= cutoff]


def check_fresh(df, tf_seconds, now=None):
    """Raise StaleDataError if the newest closed candle is too old (market open only)."""
    now = now or datetime.now(timezone.utc)
    if not market_status(now)["open"]:
        raise StaleDataError("Forex market is closed - no fresh data")
    last_close = df.index[-1].to_pydatetime() + timedelta(seconds=tf_seconds)
    age = (now - last_close).total_seconds()
    if age > tf_seconds * 3 + 120:
        raise StaleDataError("Latest candle is %.0f min old (stale)" % (age / 60))


def get_candles(symbol, timeframe="1m", size=300, require_fresh=True):
    """
    Returns a DataFrame (index = candle open time, UTC) of CLOSED candles.
    Raises DataError subclasses if data is missing, invalid or stale.
    """
    if not TD_KEY:
        raise DataError("TWELVE_DATA_API_KEY is not set")
    tf = TIMEFRAMES.get(str(timeframe).lower())
    if not tf:
        raise DataError("Unsupported timeframe: %s" % timeframe)
    sym = normalize_symbol(symbol)
    if not sym:
        raise DataError("Invalid symbol: %s" % symbol)
    interval, secs = tf
    params = {"symbol": sym, "interval": interval, "outputsize": size,
              "timezone": "UTC", "order": "DESC", "apikey": TD_KEY}

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            limiter.wait()
            resp = requests.get(API_URL, params=params, timeout=20)
            if resp.status_code == 429:
                raise RateLimitError("HTTP 429 from provider")
            resp.raise_for_status()
            df = closed_only(parse_candles(resp.json(), secs), secs)
            if len(df) < MIN_CANDLES:
                raise InvalidDataError("Only %d closed candles (need %d)" % (len(df), MIN_CANDLES))
            if require_fresh:
                check_fresh(df, secs)
            return df
        except RateLimitError as e:
            last_err = e
            log.warning("Rate limited (%s %s), attempt %d", sym, timeframe, attempt)
            time.sleep(10 * attempt)
        except (requests.RequestException, ValueError) as e:
            last_err = DataError("Network/parse error: %s" % e)
            log.warning("Fetch failed (%s %s): %s", sym, timeframe, e)
            time.sleep(2 * attempt)
    raise last_err or DataError("Unknown data error")


async def get_candles_async(symbol, timeframe="1m", size=300, require_fresh=True):
    """Same as get_candles but does not block the bot's event loop."""
    return await asyncio.to_thread(get_candles, symbol, timeframe, size, require_fresh)


def latest_price(df):
    """Last CLOSE of the newest closed candle (observed price, not a quote)."""
    return float(df["close"].iloc[-1])
