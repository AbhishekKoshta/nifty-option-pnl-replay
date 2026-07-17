"""Free, no-login historical 1-minute option data via Upstox.

Upstox serves 1-minute OHLC + OI candles for NSE F&O contracts through a public
historical-candle API that needs NO auth token, plus a public instrument master
that maps (underlying, expiry, strike, CE/PE) -> instrument_key. We use those two
to replay a real trading day's option prices.

Verified working 2026-07-17:
  master  : https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
  1-min   : https://api.upstox.com/v3/historical-candle/{key}/minutes/1/{to}/{from}
  today   : https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/1

Coverage: any contract still LISTED in today's master (expiry >= today), over its
full life (the current weekly goes back to its listing ~4 weeks). Already-expired
weeklies drop out of the master and need the auth-gated expired API -> not covered.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
_DIR = os.path.dirname(os.path.abspath(__file__))
_MASTER_GZ = os.path.join(_DIR, "upstox_nse.json.gz")
_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_HDRS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
         "Accept": "application/json"}
_MASTER_TTL_H = 12          # refresh the instrument master at most twice a day
_SESSION = requests.Session()
_SESSION.headers.update(_HDRS)

# index-option underlyings we care about (name field in the master)
INDEX_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

_MASTER_CACHE: list | None = None       # parsed master, in-memory


# --------------------------------------------------------------------------- #
# instrument master
# --------------------------------------------------------------------------- #
def _download_master() -> None:
    r = _SESSION.get(_MASTER_URL, timeout=30)
    r.raise_for_status()
    with open(_MASTER_GZ, "wb") as f:
        f.write(r.content)


def _master_stale() -> bool:
    if not os.path.exists(_MASTER_GZ):
        return True
    age_h = (time.time() - os.path.getmtime(_MASTER_GZ)) / 3600
    return age_h > _MASTER_TTL_H


def load_master(force: bool = False) -> list:
    """Return the parsed Upstox NSE instrument master (cached on disk + memory)."""
    global _MASTER_CACHE
    if force or _master_stale():
        _download_master()
        _MASTER_CACHE = None
    if _MASTER_CACHE is None:
        with gzip.open(_MASTER_GZ) as f:
            _MASTER_CACHE = json.load(f)
    return _MASTER_CACHE


def _exp_to_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, IST).date()


def _options(underlying: str) -> list:
    m = load_master()
    up = underlying.upper()
    return [d for d in m
            if d.get("name") == up
            and d.get("instrument_type") in ("CE", "PE")
            and d.get("strike_price")]


def expiries(underlying: str) -> list[str]:
    """Sorted 'YYYY-MM-DD' expiries currently listed for the underlying."""
    exps = {_exp_to_date(d["expiry"]) for d in _options(underlying)}
    return [e.isoformat() for e in sorted(exps)]


def strikes(underlying: str, expiry: str) -> list[float]:
    exp = date.fromisoformat(expiry)
    ks = {float(d["strike_price"]) for d in _options(underlying)
          if _exp_to_date(d["expiry"]) == exp}
    return sorted(ks)


def resolve(underlying: str, expiry: str, strike: float, opt_type: str) -> dict:
    """Return {instrument_key, trading_symbol, lot_size, expiry} for the contract."""
    exp = date.fromisoformat(expiry)
    for d in _options(underlying):
        if (d["instrument_type"] == opt_type.upper()
                and float(d["strike_price"]) == float(strike)
                and _exp_to_date(d["expiry"]) == exp):
            return {"instrument_key": d["instrument_key"],
                    "trading_symbol": d["trading_symbol"],
                    "lot_size": int(d.get("lot_size") or 0),
                    "underlying_key": d.get("underlying_key") or d.get("asset_key"),
                    "expiry": expiry}
    raise LookupError(f"{underlying} {int(strike)}{opt_type.upper()} {expiry} not in "
                      "Upstox master (contract may be expired / not listed).")


# --------------------------------------------------------------------------- #
# 1-minute candles
# --------------------------------------------------------------------------- #
_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def _candles_to_df(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=_COLS)
    df = pd.DataFrame(candles, columns=_COLS[: len(candles[0])])
    df["ts"] = pd.to_datetime(df["ts"])                 # tz-aware IST offset
    for c in ("open", "high", "low", "close", "volume", "oi"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("ts").reset_index(drop=True)


# local on-disk cache of 1-minute bars (completed days are immutable -> cache
# forever; today's session is still forming -> never cached).
_CACHE_DIR = os.path.join(_DIR, "min_cache")


def _cache_path(instrument_key: str, day: str) -> str:
    safe = instrument_key.replace("|", "_").replace("/", "_").replace(":", "_")
    return os.path.join(_CACHE_DIR, f"{safe}_{day}.csv")


def is_day_cached(instrument_key: str, day: str) -> bool:
    return os.path.exists(_cache_path(instrument_key, day))


def _read_cache(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["ts"])
    df["ts"] = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    for c in ("open", "high", "low", "close", "volume", "oi"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _fetch_day_1m_net(instrument_key: str, day: str) -> pd.DataFrame:
    ikq = urllib.parse.quote(instrument_key, safe="")
    is_today = day == datetime.now(IST).date().isoformat()
    if is_today:
        url = f"https://api.upstox.com/v3/historical-candle/intraday/{ikq}/minutes/1"
    else:
        url = f"https://api.upstox.com/v3/historical-candle/{ikq}/minutes/1/{day}/{day}"
    r = _SESSION.get(url, timeout=25)
    r.raise_for_status()
    candles = (r.json().get("data") or {}).get("candles") or []
    return _candles_to_df(candles)


def fetch_day_1m(instrument_key: str, day: str, use_cache: bool = True) -> pd.DataFrame:
    """1-minute OHLC+OI for a single trading day ('YYYY-MM-DD').

    Completed days (day < today) are cached to `min_cache/*.csv` and served from
    disk on subsequent calls — no re-fetch. Today's still-forming session is never
    cached. Uses the intraday endpoint for today, else the historical endpoint.
    Returns an ascending DataFrame [ts, open, high, low, close, volume, oi].
    """
    today = datetime.now(IST).date().isoformat()
    completed = day < today
    path = _cache_path(instrument_key, day)

    if use_cache and completed and os.path.exists(path):
        try:
            df = _read_cache(path)
            if not df.empty:
                return df
        except Exception:
            pass  # corrupt cache -> refetch below

    df = _fetch_day_1m_net(instrument_key, day)

    if completed and not df.empty:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            df.to_csv(path, index=False)
        except Exception:
            pass
    return df


def available_days(instrument_key: str, back: int = 40) -> list[str]:
    """Trading days (desc) that have data for this contract, from daily candles."""
    ikq = urllib.parse.quote(instrument_key, safe="")
    end = datetime.now(IST).date()
    start = end - timedelta(days=back)
    url = (f"https://api.upstox.com/v3/historical-candle/{ikq}/days/1/"
           f"{end.isoformat()}/{start.isoformat()}")
    try:
        r = _SESSION.get(url, timeout=20)
        candles = (r.json().get("data") or {}).get("candles") or []
    except Exception:
        candles = []
    days = sorted({str(c[0])[:10] for c in candles}, reverse=True)
    # the historical endpoint excludes the current day — prepend today if it
    # has intraday data (so an in-progress / just-closed session is replayable).
    today = datetime.now(IST).date().isoformat()
    if today not in days:
        try:
            if not fetch_day_1m(instrument_key, today).empty:
                days = [today] + days
        except Exception:
            pass
    return days


if __name__ == "__main__":          # smoke test
    print("expiries NIFTY:", expiries("NIFTY")[:4])
    exp = expiries("NIFTY")[0]
    ks = strikes("NIFTY", exp)
    print(f"{exp}: {len(ks)} strikes {ks[0]:.0f}..{ks[-1]:.0f}")
    atm = min(ks, key=lambda k: abs(k - 24350))
    info = resolve("NIFTY", exp, atm, "CE")
    print("resolved:", info)
    days = available_days(info["instrument_key"])
    print("available days:", days[:5])
    if len(days) > 1:
        df = fetch_day_1m(info["instrument_key"], days[1])
        print(f"1-min bars for {days[1]}: {len(df)}")
        print(df.head(2).to_string())
        print(df.tail(1).to_string())
