"""Live NIFTY/BankNifty option-chain + spot fetch from the public NSE API.

NSE gates its JSON behind a browser cookie, so we prime a Session by hitting
the homepage + option-chain page first, then call the data endpoints. No auth /
API key needed. Endpoints verified working 2026-07-17:
  - /api/allIndices                       -> live index spot
  - /api/option-chain-contract-info       -> list of expiry dates
  - /api/option-chain-v3?type=Indices...  -> full chain (LTP + IV + OI) per expiry
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/option-chain",
}
_BASE = "https://www.nseindia.com"
_INDEX_KEY = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}


@dataclass
class OptionQuote:
    strike: float
    opt_type: str  # "CE" or "PE"
    last_price: float
    iv: float  # implied volatility, percent (e.g. 10.72)
    oi: float
    expiry: str  # "21-Jul-2026"


class NSEClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(_HEADERS)
        self._primed = False

    def _prime(self):
        if self._primed:
            return
        self.s.get(_BASE + "/", timeout=10)
        self.s.get(_BASE + "/option-chain", timeout=10)
        self._primed = True

    def _get_json(self, url: str, tries: int = 3):
        self._prime()
        last = None
        for i in range(tries):
            r = self.s.get(url, timeout=10)
            if r.status_code == 200 and r.text.strip().startswith(("{", "[")):
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:80]}"
            self._primed = False
            self._prime()
            time.sleep(0.6 * (i + 1))
        raise RuntimeError(f"NSE fetch failed for {url}\n  {last}")

    def spot(self, symbol: str = "NIFTY") -> float:
        data = self._get_json(_BASE + "/api/allIndices")
        key = _INDEX_KEY.get(symbol.upper(), "NIFTY 50")
        for row in data["data"]:
            if row["index"] == key:
                return float(row["last"])
        raise RuntimeError(f"index {key} not found in allIndices")

    def expiries(self, symbol: str = "NIFTY") -> list[str]:
        data = self._get_json(_BASE + f"/api/option-chain-contract-info?symbol={symbol.upper()}")
        return data["expiryDates"]

    def chain(self, symbol: str = "NIFTY", expiry: str | None = None):
        """Return (underlying_spot, {(strike, 'CE'|'PE'): OptionQuote})."""
        if expiry is None:
            expiry = self.expiries(symbol)[0]  # nearest
        import urllib.parse

        url = (
            _BASE + "/api/option-chain-v3?type=Indices&symbol="
            + symbol.upper() + "&expiry=" + urllib.parse.quote(expiry)
        )
        data = self._get_json(url)
        rec = data["records"]
        spot = float(rec["underlyingValue"])
        out: dict[tuple, OptionQuote] = {}
        for row in rec["data"]:
            k = float(row["strikePrice"])
            for side in ("CE", "PE"):
                if side in row:
                    o = row[side]
                    out[(k, side)] = OptionQuote(
                        strike=k,
                        opt_type=side,
                        last_price=float(o.get("lastPrice") or 0),
                        iv=float(o.get("impliedVolatility") or 0),
                        oi=float(o.get("openInterest") or 0),
                        expiry=o.get("expiryDate", expiry),
                    )
        return spot, out

    def quote(self, symbol: str, strike: float, opt_type: str, expiry: str | None = None):
        spot, ch = self.chain(symbol, expiry)
        key = (float(strike), opt_type.upper())
        if key not in ch:
            avail = sorted({s for (s, t) in ch})
            raise RuntimeError(
                f"strike {strike} {opt_type} not in chain. "
                f"Available strikes: {avail[0]:.0f}..{avail[-1]:.0f}"
            )
        return spot, ch[key]
