#!/usr/bin/env python3
"""Streamlit app: NIFTY option tools.

Tab 1 — Live P&L Simulator: live NSE snapshot + Black-Scholes reprice/decay grid.
Tab 2 — Intraday Replay:     real 1-minute option data (Upstox, no login) replayed
                            minute-by-minute as an animated P&L for a chosen day.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402
import pandas as pd
import streamlit as st

# --- CRITICAL: keep this app 100% pyarrow-free (Python 3.14 segfault) ------- #
# pyarrow's bundled mimalloc allocator segfaults during per-thread heap init
# (mi_thread_init) inside Streamlit's fresh ScriptRunner thread on every rerun —
# it crashed the whole server. Two pyarrow entry points had to be closed:
#   1. pandas 3.0 stores string columns in a pyarrow backend by default → force
#      classic numpy/object strings so no DataFrame op touches pyarrow.
try:
    pd.set_option("future.infer_string", False)
    pd.set_option("mode.string_storage", "python")
except Exception:
    pass
#   2. Streamlit serializes DataFrames to Arrow for st.dataframe / st.altair_chart /
#      native charts → this app renders charts as matplotlib PNGs (st.image) and
#      tables as HTML (st.html) instead. DO NOT reintroduce st.dataframe / altair.

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bs import IST, bs_price, intrinsic, years_to_expiry  # noqa: E402
from nse_data import NSEClient  # noqa: E402
import upstox_data as ud  # noqa: E402
import replay_engine as rp  # noqa: E402

LOT = 65

st.set_page_config(page_title="Option P&L Simulator", page_icon="📈", layout="wide")


# --------------------------------------------------------------------------- #
# cached live data (Live tab) — plain returns so Streamlit can pickle them
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=30, show_spinner=False)
def get_expiries(symbol: str) -> list[str]:
    return NSEClient().expiries(symbol)


@st.cache_data(ttl=30, show_spinner=False)
def get_chain(symbol: str, expiry: str):
    """Return (spot, {(strike, side): (last, iv, oi, expiry_str)})."""
    spot, ch = NSEClient().chain(symbol, expiry)
    flat = {k: (q.last_price, q.iv, q.oi, q.expiry) for k, q in ch.items()}
    return spot, flat


def implied_vol(price, spot, strike, t, opt_type, lo=0.5, hi=300.0):
    if t <= 0 or price <= intrinsic(spot, strike, opt_type) + 1e-6:
        return 0.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, opt_type) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def rupees(x):
    return f"{'+' if x >= 0 else '-'}{abs(x):,.0f}"


def _fig_png(fig) -> bytes:
    """Serialize a matplotlib Figure to PNG bytes (no pyplot, thread-safe)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    buf = io.BytesIO()
    FigureCanvasAgg(fig).print_png(buf)
    fig.clear()
    return buf.getvalue()


def payoff_png(pdf, lo, hi, breakeven, spot) -> bytes:
    """Render the Now-vs-expiry payoff chart as a PNG (Arrow-free, replaces altair)."""
    fig = Figure(figsize=(9.4, 3.4))
    ax = fig.subplots()
    fig.subplots_adjust(left=0.09, right=0.98, top=0.95, bottom=0.14)
    for name, col in [("Now", "#2563eb"), ("At expiry", "#16a34a")]:
        sub = pdf[pdf["Valuation"] == name]
        ax.plot(sub["Spot"], sub["P&L (₹)"], color=col, lw=1.9, label=name)
    ax.axhline(0, color="#888", ls="--", lw=1.0)
    ax.axvline(breakeven, color="#f59e0b", ls="--", lw=1.2)
    ax.axvline(spot, color="#dc2626", lw=1.2)
    ax.set_xlim(lo, hi)
    ax.set_xlabel("NIFTY spot")
    ax.set_ylabel("P&L (₹)")
    ax.grid(True, color="#e0e0e0", lw=0.5)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.8)
    return _fig_png(fig)


def matrix_html(mat, vmax) -> str:
    """Render the P&L matrix as a colour-graded HTML table (Arrow-free)."""
    def cell(v):
        a = min(1.0, abs(v) / vmax)
        if v >= 0:
            bg, fg = f"rgba(22,163,74,{0.12 + 0.55*a})", "#0a3d1f"
        else:
            bg, fg = f"rgba(220,38,38,{0.12 + 0.55*a})", "#4a0d0d"
        return (f'<td style="background:{bg};color:{fg};padding:4px 10px;'
                f'text-align:right;font-variant-numeric:tabular-nums">{rupees(v)}</td>')
    head = "".join(f'<th style="padding:4px 10px;text-align:right">{c}</th>' for c in mat.columns)
    body = ""
    for idx, row in mat.iterrows():
        body += (f'<tr><th style="padding:4px 10px;text-align:right;'
                 f'white-space:nowrap">{idx}</th>' + "".join(cell(v) for v in row) + "</tr>")
    return (
        '<div style="overflow-x:auto"><table style="border-collapse:collapse;'
        'font-size:0.86rem;width:100%">'
        f'<thead><tr><th style="padding:4px 10px;text-align:left">Spot</th>{head}</tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


# --------------------------------------------------------------------------- #
# cached free data (Replay tab) — Upstox, no login
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def r_expiries(underlying: str) -> list[str]:
    return ud.expiries(underlying)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def r_strikes(underlying: str, expiry: str) -> list[float]:
    return ud.strikes(underlying, expiry)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def r_resolve(underlying: str, expiry: str, strike: float, opt_type: str) -> dict:
    return ud.resolve(underlying, expiry, strike, opt_type)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def r_days(instrument_key: str) -> list[str]:
    return ud.available_days(instrument_key)


@st.cache_data(ttl=300, show_spinner=False)
def r_day_1m(instrument_key: str, day: str) -> pd.DataFrame:
    return ud.fetch_day_1m(instrument_key, day)


@st.cache_data(ttl=1800, show_spinner=False)
def r_build_mp4(instrument_key: str, day: str, entry_idx: int, entry_px: float,
                sign: int, qty: int, exit_idx: int, label: str, day_lbl: str,
                underlying_key: str, duration_s: float) -> bytes:
    """Render the replay MP4 (cached by all inputs). Thread-safe (no pyplot).
    Draws the underlying (NIFTY 50) 1-min candles in the top panel."""
    df = r_day_1m(instrument_key, day)
    idx_df = r_day_1m(underlying_key, day) if underlying_key else None
    ctx = rp.compute(df, entry_idx, entry_px, sign, qty, exit_idx, idx_df=idx_df)
    ctx.label = label
    ctx.day = day_lbl
    return rp.render_mp4(ctx, duration_s=duration_s, dpi=100)


@st.cache_data(ttl=1800, show_spinner=False)
def r_build_gif(instrument_key: str, day: str, entry_idx: int, entry_px: float,
                sign: int, qty: int, exit_idx: int, label: str, day_lbl: str,
                underlying_key: str, duration_s: float) -> bytes:
    """Render the replay as an animated GIF (cached by all inputs)."""
    df = r_day_1m(instrument_key, day)
    idx_df = r_day_1m(underlying_key, day) if underlying_key else None
    ctx = rp.compute(df, entry_idx, entry_px, sign, qty, exit_idx, idx_df=idx_df)
    ctx.label = label
    ctx.day = day_lbl
    return rp.render_gif(ctx, duration_s=duration_s)


# Intraday Replay is the default (first) tab; the Live simulator is second.
tab_replay, tab_live = st.tabs(["🎬 Intraday Replay (real 1-min)",
                                "📈 Live P&L Simulator"])

# =========================================================================== #
# TAB 1 — LIVE P&L SIMULATOR  (controls live in the sidebar)
# =========================================================================== #
with tab_live:
    st.sidebar.header("Position (Live tab)")
    symbol = st.sidebar.selectbox("Underlying", ["NIFTY", "BANKNIFTY", "FINNIFTY"], index=0)

    if st.sidebar.button("🔄 Refresh live data"):
        st.cache_data.clear()

    fetch_ok = True
    err_msg = ""
    try:
        expiries = get_expiries(symbol)
        expiry = st.sidebar.selectbox("Expiry", expiries, index=0)
        spot, chain = get_chain(symbol, expiry)
        strikes = sorted({s for (s, side) in chain})
    except Exception as e:  # NSE often blocks cloud/non-IN IPs — offer manual entry
        fetch_ok = False
        err_msg = str(e)
        expiry = st.sidebar.text_input("Expiry (manual)", "21-Jul-2026")
        strikes = []
        spot = None
        chain = {}

    opt_type = st.sidebar.radio("Type", ["CE", "PE"], horizontal=True)
    side = st.sidebar.radio("Side", ["buy", "sell"], horizontal=True,
                            format_func=lambda s: "Long (buy)" if s == "buy" else "Short (sell)")

    if fetch_ok and strikes:
        default_ix = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        strike = st.sidebar.selectbox("Strike", strikes, index=default_ix,
                                      format_func=lambda x: f"{x:,.0f}")
        live_ltp, nse_iv, _, exp_str = chain.get((strike, opt_type), (0.0, 0.0, 0.0, expiry))
    else:
        strike = st.sidebar.number_input("Strike", value=24300.0, step=50.0)
        live_ltp = st.sidebar.number_input("Live option LTP (manual)", value=141.95, step=0.05)
        nse_iv = st.sidebar.number_input("IV %% (manual)", value=11.0, step=0.5)
        spot = st.sidebar.number_input("Spot (manual)", value=24334.0, step=1.0)
        exp_str = expiry

    entry = st.sidebar.number_input("Entry price (your 'entry point')", value=72.55, step=0.05)
    lots = st.sidebar.number_input("Lots", value=1, min_value=1, step=1)
    qty = int(lots) * LOT
    st.sidebar.caption(f"Quantity = {qty}  (lot {LOT})")

    with st.sidebar.expander("Advanced"):
        iv_override = st.number_input("IV %% override (0 = auto-calibrate)", value=0.0, step=0.5)
        spot_range = st.slider("Spot ladder ± %", 1.0, 8.0, 2.5, 0.5)
        n_rows = st.slider("Ladder rows", 5, 20, 11)

    if not fetch_ok:
        st.warning(
            "⚠️ Could not reach NSE from this host (common on Streamlit Cloud — NSE "
            f"blocks non-Indian/cloud IPs). Using **manual** spot/LTP/IV from the sidebar.\n\n`{err_msg}`"
        )

    sign = 1 if side == "buy" else -1
    t = years_to_expiry(exp_str)
    days_left = t * 365

    iv = iv_override if iv_override > 0 else implied_vol(live_ltp, spot, strike, t, opt_type)
    if iv <= 0:
        iv = nse_iv or 12.0

    cur_pnl = sign * (live_ltp - entry) * qty
    breakeven = strike + (entry if opt_type == "CE" else -entry)

    st.title("📈 Option P&L Simulator")
    st.caption(
        f"**{'LONG' if sign > 0 else 'SHORT'} {int(lots)} lot ({qty} qty)  "
        f"{symbol} {int(strike)} {opt_type}**  ·  expiry {exp_str}  ·  {days_left:.2f} days left"
        + ("  ·  live NSE data" if fetch_ok else "  ·  manual data")
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spot", f"{spot:,.2f}")
    c2.metric("Option LTP", f"{live_ltp:,.2f}", help=f"NSE IV {nse_iv:.1f}% · calib IV {iv:.1f}%")
    c3.metric("Current P&L", f"₹{rupees(cur_pnl)}", f"{sign*(live_ltp-entry):+.2f} pts")
    c4.metric("Breakeven @ expiry", f"{breakeven:,.2f}",
              help=f"spot must {'rise above' if opt_type=='CE' else 'fall below'} this")

    lo, hi = spot * (1 - spot_range / 100), spot * (1 + spot_range / 100)
    xs = [lo + (hi - lo) * i / 200 for i in range(201)]
    rows = []
    for s in xs:
        now_price = bs_price(s, strike, t, iv, opt_type)
        rows.append({"Spot": s, "P&L (₹)": sign * (now_price - entry) * qty, "Valuation": "Now"})
        rows.append({"Spot": s, "P&L (₹)": sign * (intrinsic(s, strike, opt_type) - entry) * qty,
                     "Valuation": "At expiry"})
    pdf = pd.DataFrame(rows)

    st.image(payoff_png(pdf, lo, hi, breakeven, spot), width="stretch")
    st.caption("🔵 Now (Black-Scholes, current IV & time-decay)  ·  🟢 At expiry (payoff)  "
               "·  🟠 breakeven  ·  🔴 current spot")

    st.subheader("P&L matrix — spot move × time decay")
    ladder = [lo + (hi - lo) * i / (n_rows - 1) for i in range(n_rows)]
    now = datetime.now(IST)
    ncols = min(6, max(2, int(days_left) + 1))
    offsets = [days_left * i / (ncols - 1) for i in range(ncols)]
    col_t = [max(0.0, (days_left - o) / 365) for o in offsets]
    col_t[-1] = 0.0
    col_labels = ["EXP" if ct <= 0 else (now + timedelta(days=o)).strftime("%d-%b")
                  for ct, o in zip(col_t, offsets)]

    data = {}
    for lbl, ct in zip(col_labels, col_t):
        col = []
        for s in ladder:
            price = bs_price(s, strike, ct, iv, opt_type) if ct > 0 else intrinsic(s, strike, opt_type)
            col.append(sign * (price - entry) * qty)
        data[lbl] = col
    mat = pd.DataFrame(data, index=[f"{s:,.0f}" for s in ladder])
    mat.index.name = "Spot"

    vmax = max(1.0, mat.abs().to_numpy().max())
    st.html(matrix_html(mat, vmax))
    st.caption(f"Rows = NIFTY spot ({-spot_range:+.1f}%..{spot_range:+.1f}%). "
               "Columns = valuation date (EXP = expiry payoff). Assumes IV held constant.")

    with st.expander("Notes & caveats"):
        st.markdown(
            "- NIFTY options are **European** → Black-Scholes is appropriate.\n"
            "- The grid holds **IV constant**; a real vol move (IV crush on/after an "
            "event) shifts the *Now* column. Use the IV override to stress-test.\n"
            "- Base value uses **last-traded price**; wide bid/ask on far strikes makes it noisy.\n"
            "- NSE data is delayed a few seconds and only served during/after market hours.\n"
            "- If deployed where NSE blocks the IP, enter spot/LTP/IV manually in the sidebar."
        )

# =========================================================================== #
# TAB 2 — INTRADAY REPLAY  (real 1-min option data via Upstox, no login)
# =========================================================================== #
with tab_replay:
    st.subheader("🎬 Intraday P&L Replay — real 1-minute option data")
    st.caption("Pick a contract + your entry point, pull the **real** 1-minute prices for a day, "
               "and watch your P&L build minute-by-minute. Data: Upstox public historical API "
               "(no login). Covers contracts still listed today (recent weeks).")

    cc = st.columns([1.1, 1.1, 1.2, 0.8, 0.8])
    r_ul = cc[0].selectbox("Underlying", ud.INDEX_UNDERLYINGS, index=0, key="r_ul")
    try:
        exps = r_expiries(r_ul)
    except Exception as e:
        st.error(f"Could not load Upstox instrument master: {e}")
        st.stop()
    r_exp = cc[1].selectbox("Expiry", exps, index=0, key="r_exp")
    ks = r_strikes(r_ul, r_exp)
    # default to the near-ATM 24300 strike (falls back to the middle strike)
    _def_ix = min(range(len(ks)), key=lambda i: abs(ks[i] - 24300)) if ks else 0
    r_strike = cc[2].selectbox("Strike", ks, index=_def_ix, key="r_strike",
                               format_func=lambda x: f"{x:,.0f}")
    r_type = cc[3].radio("Type", ["CE", "PE"], horizontal=True, key="r_type")
    r_side = cc[4].radio("Side", ["buy", "sell"], horizontal=True, key="r_side",
                         format_func=lambda s: "Long" if s == "buy" else "Short")

    try:
        info = r_resolve(r_ul, r_exp, r_strike, r_type)
    except Exception as e:
        st.error(str(e))
        st.stop()
    lot_size = info["lot_size"] or LOT
    days = r_days(info["instrument_key"])

    cc2 = st.columns([1.2, 0.8, 1.0, 1.4])
    if not days:
        cc2[0].warning("No trading days available for this contract yet.")
        st.stop()
    r_day = cc2[0].selectbox("Day to replay", days, index=0, key="r_day")
    r_lots = cc2[1].number_input("Lots", value=1, min_value=1, step=1, key="r_lots")
    r_qty = int(r_lots) * lot_size
    cc2[2].metric("Contract", info["trading_symbol"].split(" ", 1)[0] + f" {int(r_strike)}{r_type}",
                  help=info["trading_symbol"])
    do_fetch = cc2[3].button("📥 Fetch real 1-min data & build replay", type="primary",
                             width="stretch")

    key = f"{info['instrument_key']}|{r_day}"
    if do_fetch:
        from_cache = ud.is_day_cached(info["instrument_key"], r_day)
        spin = ("Loading 1-minute prices from local cache…" if from_cache
                else f"Fetching real 1-minute prices for {info['trading_symbol']} on {r_day}…")
        with st.spinner(spin):
            try:
                df = r_day_1m(info["instrument_key"], r_day)
            except Exception as e:
                st.error(f"Fetch failed: {e}")
                df = pd.DataFrame()
        if df.empty:
            st.error("No 1-minute data returned for that day (holiday, or before the "
                     "contract was listed).")
        else:
            st.session_state["replay"] = {
                "df": df, "key": key, "symbol": info["trading_symbol"],
                "day": r_day, "qty": r_qty, "lot_size": lot_size,
                "cached": from_cache,
            }

    state = st.session_state.get("replay")
    if state and state["key"] == key and state["qty"] == r_qty:
        df = state["df"]
        n = len(df)
        times = [pd.Timestamp(t).strftime("%H:%M") for t in df["ts"]]
        sign = 1 if r_side == "buy" else -1

        if state.get("cached"):
            src = "⚡ served from local cache (no re-fetch)"
        elif r_day < datetime.now(IST).date().isoformat():
            src = "⬇ fetched from Upstox · saved to local cache for next time"
        else:
            src = "⬇ fetched from Upstox · today's live session (not cached)"
        st.caption(f"{n} one-minute bars · {src}")

        st.divider()
        ec = st.columns(3)
        _def_entry = "09:19" if "09:19" in times else times[0]      # your fill minute
        entry_lbl = ec[0].select_slider("Entry time", options=times, value=_def_entry, key="r_entry_t")
        entry_idx = times.index(entry_lbl)
        default_entry_px = round(float(df["close"].iloc[entry_idx]), 2)
        entry_px = ec[1].number_input("Entry price", value=default_entry_px, step=0.05,
                                      key="r_entry_px",
                                      help="Auto-filled from the entry minute's close; override with your real fill.")
        exit_lbl = ec[2].select_slider("Exit time", options=times, value=times[-1], key="r_exit_t")
        exit_idx = times.index(exit_lbl)
        if exit_idx <= entry_idx:
            exit_idx = n - 1

        ctx = rp.compute(df, entry_idx, entry_px, sign, r_qty, exit_idx)
        ctx.label = (f"{'LONG' if sign > 0 else 'SHORT'} {int(r_lots)} lot "
                     f"{r_ul} {int(r_strike)} {r_type}")
        ctx.day = state["day"]
        s = ctx.stats

        ret_pct = sign * (s["exit_price"] - entry_px) / entry_px * 100 if entry_px else 0.0
        m = st.columns(6)
        # Final P&L = (LTP − entry) × qty, gross — identical to Kite's Positions P&L.
        m[0].metric("Final P&L (Kite)", rp._fmt_rupees(s["final_pnl"]),
                    f"{sign*(s['exit_price']-entry_px):+.2f} pts")
        m[1].metric("Return", f"{ret_pct:+.2f}%", "Kite 'Chg.'", delta_color="off")
        m[2].metric("Max profit", rp._fmt_rupees(s["max_profit"]),
                    f"at {rp._hhmm(s['max_profit_time'])}", delta_color="off")
        m[3].metric("Max drawdown", rp._fmt_rupees(s["max_dd"]),
                    f"at {rp._hhmm(s['max_dd_time'])}", delta_color="off")
        m[4].metric("Time in profit", f"{s['pct_time_in_profit']:.0f}%")
        m[5].metric("Entry → LTP", f"{entry_px:.1f} → {s['exit_price']:.1f}",
                    f"{s['n_minutes']} min held", delta_color="off")
        st.caption("P&L = (LTP − entry) × qty, gross — matches Kite exactly when "
                   "**Entry price = your Kite Avg** and **Exit = 15:29 (LTP)**.")

        # ---- build the replay as an MP4 and play it (robust: no per-frame render
        #      in the Streamlit thread, so it can't segfault the session) ----------
        bc = st.columns([1.5, 1.3, 2.2])
        build = bc[0].button("🎬 Build replay video", type="primary", key="r_build",
                             width="stretch")
        duration_s = bc[1].select_slider("Video length", options=[5, 8, 12, 18, 25, 35],
                                         value=12, format_func=lambda s: f"{s}s", key="r_dur")
        auto = bc[2].checkbox("Auto-rebuild when I change inputs", value=False,
                              help="Off by default so typing an entry price doesn't "
                                   "re-render on every keystroke.")
        sig = (f"{info['instrument_key']}|{r_day}|{entry_idx}|{round(entry_px,2)}|"
               f"{sign}|{r_qty}|{exit_idx}|{duration_s}")

        mp4 = st.session_state.get("replay_mp4")
        need = build or (auto and (not mp4 or mp4.get("sig") != sig))
        if need:
            with st.spinner("Rendering replay video (real 1-min frames → H.264)…"):
                try:
                    data = r_build_mp4(info["instrument_key"], r_day, entry_idx, entry_px,
                                       sign, r_qty, exit_idx, ctx.label, ctx.day,
                                       info.get("underlying_key"), duration_s)
                    st.session_state["replay_mp4"] = {"sig": sig, "bytes": data}
                    mp4 = st.session_state["replay_mp4"]
                except Exception as e:
                    st.error(f"Video render failed: {e}")
                    mp4 = None

        if mp4 and mp4.get("sig") == sig:
            st.video(mp4["bytes"])
            fname = f"replay_{r_ul}_{int(r_strike)}{r_type}_{r_day}"
            dcol = st.columns([1.2, 1.4, 3])
            dcol[0].download_button("⬇ MP4", mp4["bytes"], file_name=f"{fname}.mp4",
                                    mime="video/mp4", width="stretch")
            gif = st.session_state.get("replay_gif")
            if gif and gif.get("sig") == sig:
                dcol[1].download_button("⬇ GIF", gif["bytes"], file_name=f"{fname}.gif",
                                        mime="image/gif", width="stretch")
            elif dcol[1].button("🎞️ Make GIF", width="stretch", key="r_makegif"):
                with st.spinner("Rendering GIF…"):
                    try:
                        gdata = r_build_gif(info["instrument_key"], r_day, entry_idx, entry_px,
                                            sign, r_qty, exit_idx, ctx.label, ctx.day,
                                            info.get("underlying_key"), duration_s)
                        st.session_state["replay_gif"] = {"sig": sig, "bytes": gdata}
                        st.rerun()
                    except Exception as e:
                        st.error(f"GIF render failed: {e}")
            st.caption("**NIFTY 50 1-min candles** (left axis) + **P&L ₹** as an indigo dotted line "
                       "(right axis); 🟠 entry at 0 P&L, faint dotted = running peak & trough. Real "
                       "traded 1-min data. MP4 = smaller/higher quality; GIF = easy to paste anywhere.")
        elif mp4 and mp4.get("sig") != sig:
            st.info("Inputs changed — click **🎬 Build replay video** to re-render.")
        else:
            st.info("Click **🎬 Build replay video** to render the minute-by-minute animation.")
    else:
        st.info("Set the contract, strike, side and day above, then click "
                "**Fetch real 1-min data & build replay**.")
