"""Black-Scholes pricing for European index options (NIFTY/BankNifty are European).

Used to re-price the option at hypothetical spot levels *before* expiry, so the
simulator can show P&L for an intraday/multi-day move (not just expiry payoff).
Uses math.erf for the normal CDF so there's no scipy dependency.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
RISK_FREE = 0.065  # India ~6.5%; option value is fairly insensitive to this


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, iv_pct: float,
             opt_type: str, r: float = RISK_FREE) -> float:
    """Theoretical option price. iv_pct is annualised vol in percent (e.g. 10.72)."""
    sigma = iv_pct / 100.0
    if t_years <= 0 or sigma <= 0:  # at/after expiry -> intrinsic value
        return intrinsic(spot, strike, opt_type)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)
    if opt_type.upper() == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def intrinsic(spot: float, strike: float, opt_type: str) -> float:
    if opt_type.upper() == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def years_to_expiry(expiry_str: str, now: datetime | None = None) -> float:
    """Fraction of a year until 15:30 IST on the expiry date. Accepts
    '21-Jul-2026' or '21-07-2026'."""
    now = now or datetime.now(IST)
    exp = None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d-%B-%Y"):
        try:
            exp = datetime.strptime(expiry_str, fmt)
            break
        except ValueError:
            continue
    if exp is None:
        raise ValueError(f"unparseable expiry: {expiry_str}")
    exp = exp.replace(hour=15, minute=30, tzinfo=IST)
    secs = (exp - now).total_seconds()
    return max(0.0, secs / (365.0 * 24 * 3600))
