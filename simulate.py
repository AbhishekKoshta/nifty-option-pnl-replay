#!/usr/bin/env python3
"""Live NIFTY option P&L simulator.

Given a position (strike / CE-PE / entry price / qty) it pulls the live spot +
option price from NSE and shows how your P&L changes as the underlying moves,
both RIGHT NOW (Black-Scholes reprice, time-decay aware) and AT EXPIRY (payoff).

Example (the position in the screenshot -- long 1 lot 24300 CE @ 72.55):
    python3 simulate.py --strike 24300 --type CE --entry 72.55 --lots 1

    python3 simulate.py --strike 24300 --type CE --entry 72.55 --qty 65 \
        --range 3 --steps 12            # +/-3% spot ladder, 12 rows

    python3 simulate.py --list-expiries         # show available expiries
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from bs import IST, bs_price, intrinsic, years_to_expiry
from nse_data import NSEClient

LOT = 65  # NIFTY lot size (house standard, 2026)


def implied_vol(price, spot, strike, t, opt_type, lo=0.5, hi=300.0):
    """Solve the IV (percent) that reprices `price` via bisection."""
    if price <= intrinsic(spot, strike, opt_type) + 1e-6 or t <= 0:
        return 0.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, opt_type) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def money(pts, qty):
    return pts * qty


def fmt_money(x):
    s = f"{abs(x):,.0f}"
    return (f"+{s}" if x >= 0 else f"-{s}")


def main():
    ap = argparse.ArgumentParser(description="Live NIFTY option P&L simulator (NSE data)")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--strike", type=float)
    ap.add_argument("--type", dest="opt_type", choices=["CE", "PE", "ce", "pe"])
    ap.add_argument("--entry", type=float, help="your entry price (the 'entry point')")
    ap.add_argument("--lots", type=int, help="number of lots (qty = lots * 65)")
    ap.add_argument("--qty", type=int, help="explicit quantity (overrides --lots)")
    ap.add_argument("--side", choices=["buy", "sell"], default="buy",
                    help="buy = long option, sell = short option")
    ap.add_argument("--expiry", default=None, help="e.g. 21-Jul-2026 (default: nearest)")
    ap.add_argument("--range", type=float, default=2.5, help="spot ladder +/- percent")
    ap.add_argument("--steps", type=int, default=10, help="rows in the ladder")
    ap.add_argument("--iv", type=float, default=None,
                    help="override IV %% (default: calibrate to live price)")
    ap.add_argument("--list-expiries", action="store_true")
    args = ap.parse_args()

    cli = NSEClient()

    if args.list_expiries:
        print("Expiries for", args.symbol.upper(), ":")
        for e in cli.expiries(args.symbol):
            print("  ", e)
        return

    if not (args.strike and args.opt_type and args.entry is not None):
        ap.error("need --strike, --type and --entry (or use --list-expiries)")

    opt_type = args.opt_type.upper()
    qty = args.qty if args.qty else (args.lots or 1) * LOT
    sign = 1 if args.side == "buy" else -1

    spot, q = cli.quote(args.symbol, args.strike, opt_type, args.expiry)
    expiry = q.expiry  # e.g. 21-07-2026
    t = years_to_expiry(expiry)
    ltp = q.last_price

    # Calibrate the vol used for the "now" reprice so the base cell == live LTP.
    iv = args.iv if args.iv is not None else implied_vol(ltp, spot, args.strike, t, opt_type)
    if iv <= 0:
        iv = q.iv or 12.0

    days_left = t * 365
    cur_pnl_pts = sign * (ltp - args.entry)
    breakeven = args.strike + (args.entry if opt_type == "CE" else -args.entry)

    # ---- header ----------------------------------------------------------
    print("=" * 72)
    print(f"  {args.symbol.upper()} {int(args.strike)} {opt_type}   exp {expiry}"
          f"   ({days_left:.2f} days left)")
    print(f"  {'LONG' if sign > 0 else 'SHORT'} {qty // LOT} lot(s) = qty {qty}"
          f"   entry {args.entry}")
    print("=" * 72)
    print(f"  Live spot        : {spot:,.2f}")
    print(f"  Live option LTP  : {ltp:,.2f}   (NSE IV {q.iv:.1f}% | calib IV {iv:.1f}%)")
    print(f"  Current P&L      : {fmt_money(money(cur_pnl_pts, qty))}  "
          f"({cur_pnl_pts:+.2f} pts x {qty})")
    print(f"  Breakeven@expiry : {breakeven:,.2f}  "
          f"(spot must {'rise above' if opt_type=='CE' else 'fall below'} this)")
    print()

    # ---- spot ladder -----------------------------------------------------
    steps = args.steps
    lo, hi = spot * (1 - args.range / 100), spot * (1 + args.range / 100)
    ladder = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
    # snapshot columns: now, and a few evenly spaced dates up to expiry
    now = datetime.now(IST)
    ncols = min(5, max(2, int(days_left) + 1))
    day_offsets = [days_left * i / (ncols - 1) for i in range(ncols)]

    col_dates = [now + timedelta(days=off) for off in day_offsets]
    col_t = [max(0.0, (days_left - off) / 365) for off in day_offsets]
    col_t[-1] = 0.0  # last column is the expiry payoff exactly

    print("  P&L (Rupees) as spot moves x days-decay   [assumes IV constant]")
    hdr = "  Spot        " + "".join(
        f"{('EXP' if ct<=0 else d.strftime('%d-%b')):>11}" for ct, d in zip(col_t, col_dates)
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in ladder:
        row = f"  {s:>9,.0f}  "
        for ct in col_t:
            price = bs_price(s, args.strike, ct, iv, opt_type) if ct > 0 else intrinsic(s, args.strike, opt_type)
            pnl = money(sign * (price - args.entry), qty)
            row += f"{fmt_money(pnl):>11}"
        print(row)
    print()
    print(f"  Cols = valuation date (EXP = at expiry payoff). Rows = NIFTY spot "
          f"({-args.range:+.1f}%..{args.range:+.1f}%).")
    if sign > 0:  # doubling the premium only makes sense for a long option
        dbl = args.strike + 2 * args.entry if opt_type == "CE" else args.strike - 2 * args.entry
        print(f"  Spot to double premium (expiry): {'>' if opt_type=='CE' else '<'} {dbl:,.0f}")
    else:
        print(f"  Max profit (expiry) = premium kept = {fmt_money(money(args.entry, qty))}; "
              f"loss is open-ended past breakeven.")


if __name__ == "__main__":
    main()
