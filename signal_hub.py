"""
signal_hub.py - Forex signals via Twelve Data -> Telegram (Telebot)

Env vars (Render): TWELVE_DATA_API_KEY
Usage in your existing bot (see README_INTEGRATION.txt):
    import signal_hub
    signal_hub.on_start(bot, message.chat.id)
"""
import os
import json
import time
import threading
import logging
import requests
import pandas as pd

log = logging.getLogger("signal_hub")

API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "XAU/USD"]
INTERVAL = "15min"          # chart timeframe
SCAN_EVERY = 15 * 60        # seconds between full scans
CALL_GAP = 8                # seconds between API calls (free plan = 8 credits/min)
MIN_SCORE = 3               # signal strength needed (max 5)

SUBS_FILE = "subscribers.json"   # NOTE: Render free disk is wiped on redeploy
_lock = threading.Lock()
_started = False
_latest = {}                # pair -> last signal text (sent to new /start users)
_last_dir = {}              # pair -> last direction (avoid repeat spam)


# ---------- subscribers ----------
def _load():
    try:
        with open(SUBS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save(subs):
    try:
        with open(SUBS_FILE, "w") as f:
            json.dump(list(subs), f)
    except Exception as e:
        log.warning("save subs failed: %s", e)


def add_subscriber(chat_id):
    with _lock:
        subs = _load()
        subs.add(chat_id)
        _save(subs)


# ---------- data + indicators ----------
def fetch(symbol, interval=INTERVAL, size=150):
    r = requests.get(BASE_URL, params={
        "symbol": symbol, "interval": interval,
        "outputsize": size, "apikey": API_KEY}, timeout=20)
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "bad response"))
    df = pd.DataFrame(data["values"])
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)   # oldest -> newest


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def analyze(df):
    """Returns dict(direction, score, entry, sl, tp, reasons) or None."""
    c = df["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    r = rsi(c)
    macd = ema(c, 12) - ema(c, 26)
    hist = macd - ema(macd, 9)
    a = atr(df).iloc[-1]

    score, why = 0, []
    # 1 trend
    if e20.iloc[-1] > e50.iloc[-1]:
        score += 1; why.append("EMA20 > EMA50 (uptrend)")
    else:
        score -= 1; why.append("EMA20 < EMA50 (downtrend)")
    # 2 price vs EMA50
    if c.iloc[-1] > e50.iloc[-1]:
        score += 1; why.append("Price above EMA50")
    else:
        score -= 1; why.append("Price below EMA50")
    # 3 MACD
    if hist.iloc[-1] > 0:
        score += 1; why.append("MACD histogram positive")
    else:
        score -= 1; why.append("MACD histogram negative")
    # 4 MACD momentum
    if hist.iloc[-1] > hist.iloc[-2]:
        score += 1; why.append("Momentum rising")
    else:
        score -= 1; why.append("Momentum falling")
    # 5 RSI
    rv = r.iloc[-1]
    if 50 < rv < 70:
        score += 1; why.append(f"RSI {rv:.0f} bullish")
    elif 30 < rv < 50:
        score -= 1; why.append(f"RSI {rv:.0f} bearish")
    else:
        why.append(f"RSI {rv:.0f} extreme (no vote)")

    if abs(score) < MIN_SCORE:
        return None
    buy = score > 0
    entry = c.iloc[-1]
    sl = entry - 1.5 * a if buy else entry + 1.5 * a
    tp = entry + 3.0 * a if buy else entry - 3.0 * a
    return {"direction": "BUY" if buy else "SELL", "score": abs(score),
            "entry": entry, "sl": sl, "tp": tp, "reasons": why}


def fmt(pair, s):
    dp = 2 if pair.startswith("XAU") or "JPY" in pair else 5
    icon = "🟢 UP" if s["direction"] == "BUY" else "🔴 DOWN"
    return (f"{icon} {pair} ({INTERVAL})\n"
            f"Signal: {s['direction']}   Strength: {s['score']}/5\n"
            f"Entry: {s['entry']:.{dp}f}\n"
            f"SL: {s['sl']:.{dp}f}\n"
            f"TP: {s['tp']:.{dp}f}\n\n"
            + "\n".join("• " + w for w in s["reasons"])
            + "\n\n⚠️ Not financial advice.")


# ---------- scanner ----------
def _broadcast(bot, text):
    for cid in _load():
        try:
            bot.send_message(cid, text)
        except Exception as e:
            log.warning("send to %s failed: %s", cid, e)


def _scan_loop(bot):
    while True:
        for pair in PAIRS:
            try:
                sig = analyze(fetch(pair))
                if sig is None:
                    _last_dir.pop(pair, None)
                    _latest.pop(pair, None)
                else:
                    text = fmt(pair, sig)
                    _latest[pair] = text
                    if _last_dir.get(pair) != sig["direction"]:
                        _last_dir[pair] = sig["direction"]
                        _broadcast(bot, text)
            except Exception as e:
                log.warning("%s scan error: %s", pair, e)
            time.sleep(CALL_GAP)
        time.sleep(SCAN_EVERY)


def start_scanner(bot):
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_scan_loop, args=(bot,), daemon=True).start()


def on_start(bot, chat_id):
    """Call this inside your existing /start handler."""
    if not API_KEY:
        bot.send_message(chat_id, "⚠️ Signal service not configured.")
        return
    add_subscriber(chat_id)
    start_scanner(bot)
    bot.send_message(chat_id, "✅ Forex signals started. New BUY/SELL signals "
                              "will be sent here automatically.")
    for text in list(_latest.values()):
        bot.send_message(chat_id, text)
