"""Intraday P&L replay: turn a day of 1-minute option candles into a per-minute
P&L series and render rich multi-panel frames for an in-app animation.

Pure compute + matplotlib (Agg) so it stays testable without Streamlit.
P&L convention (house standard): points x qty, qty = lots x lot_size.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import matplotlib
matplotlib.use("Agg")                       # headless render for Streamlit
from matplotlib.figure import Figure        # noqa: E402  OO API — thread-safe
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.offsetbox import OffsetImage, AnnotationBbox  # noqa: E402
import numpy as np                          # noqa: E402
import pandas as pd                         # noqa: E402

# NOTE: we deliberately do NOT use matplotlib.pyplot. pyplot keeps a global,
# non-thread-safe figure registry; calling it from Streamlit's ScriptRunner
# thread segfaults the process (exit 139). Building bare Figure() objects and
# handing them to st.pyplot(fig) is the thread-safe pattern.

# --- dark "trading" theme -------------------------------------------------- #
BG = "#131722"          # chart surface (recessive, TradingView-dark)
GRID = "#232833"        # recessive gridlines
SPINE = "#363c4a"
INK = "#c7cbd6"         # axis labels / ticks
INK_HI = "#f2f4fa"      # headline ink
C_MUTED = "#7c8394"     # muted subtitle
C_UP = "#26a69a"        # candle up (teal-green)
C_DOWN = "#ef5350"      # candle down (red)
C_FUT = "#2b3240"       # future / not-yet-reached candles (dim)
C_PNL = "#ffca28"       # P&L dotted line — gold, distinct from candles & entry
C_ENTRY = "#4aa3ff"     # entry line + 0-P&L marker (blue)
C_ZERO = "#525a6b"      # 0-P&L reference
C_PROFIT = "#2ee6a6"    # bright profit accent (now-dot / peak)
C_LOSS = "#ff6b6b"      # bright loss accent (now-dot / trough)
C_PRICE = "#4aa3ff"     # option-line fallback
C_GRID = GRID           # back-compat alias


@dataclass
class ReplayContext:
    """Everything needed to draw any frame, precomputed once for stable axes."""
    ts: list                    # list of pandas Timestamp (IST)
    close: np.ndarray           # option close per minute
    pnl: np.ndarray             # rupee P&L per minute (NaN before entry)
    run_max: np.ndarray         # running max profit up to each minute
    run_min: np.ndarray         # running max drawdown up to each minute
    entry_idx: int
    exit_idx: int
    entry_price: float
    sign: int                   # +1 long, -1 short
    qty: int
    label: str                  # e.g. "LONG 1 lot NIFTY 24350 CE"
    day: str
    price_ylim: tuple = (0.0, 1.0)
    pnl_ylim: tuple = (-1.0, 1.0)
    stats: dict = field(default_factory=dict)
    # underlying (NIFTY 50) 1-min OHLC, aligned to `ts` (None -> draw option line)
    idx_o: np.ndarray | None = None
    idx_h: np.ndarray | None = None
    idx_l: np.ndarray | None = None
    idx_c: np.ndarray | None = None
    idx_ylim: tuple = (0.0, 1.0)
    idx_name: str = "NIFTY 50"


def _fmt_rupees(x: float) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "--"
    return f"{'+' if x >= 0 else '-'}₹{abs(x):,.0f}"


_ROCKET_RGBA = None
_ROCKET_TRIED = False


# Colour-emoji fonts to try, in order, across platforms. macOS ships Apple Color
# Emoji; Streamlit Cloud / Debian / Ubuntu install Noto Color Emoji via
# `packages.txt` (fonts-noto-color-emoji). Bitmap-strike fonts (Noto) render at
# their native size, so we normalise the crop to a fixed height afterwards to keep
# the on-chart scale identical regardless of which font was found.
_ROCKET_FONTS = (
    ("/System/Library/Fonts/Apple Color Emoji.ttc", 160),          # macOS
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 128),    # Debian/Ubuntu (Streamlit Cloud)
    ("/usr/share/fonts/noto/NotoColorEmoji.ttf", 128),             # Arch
    ("/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf", 128),  # Fedora
)


def _rocket_rgba():
    """Rasterize the 🚀 colour emoji once, from whatever colour-emoji font the OS
    has. Returns None if none is available — callers fall back to a plain marker."""
    global _ROCKET_RGBA, _ROCKET_TRIED
    if _ROCKET_TRIED:
        return _ROCKET_RGBA
    _ROCKET_TRIED = True
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        _ROCKET_RGBA = None
        return None
    for path, size in _ROCKET_FONTS:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
            img = Image.new("RGBA", (size + 40, size + 40), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((6, 6), "🚀", font=font, embedded_color=True)
            bbox = img.getbbox()
            if not bbox:
                continue
            crop = img.crop(bbox)
            # normalise to a fixed height so `zoom` is font-independent
            target_h = 150
            w, h = crop.size
            if h and h != target_h:
                crop = crop.resize((max(1, round(w * target_h / h)), target_h),
                                   Image.LANCZOS)
            _ROCKET_RGBA = np.asarray(crop)
            return _ROCKET_RGBA
        except Exception:
            continue
    _ROCKET_RGBA = None
    return None


def compute(df: pd.DataFrame, entry_idx: int, entry_price: float,
            sign: int, qty: int, exit_idx: int | None = None,
            idx_df: pd.DataFrame | None = None) -> ReplayContext:
    """Build a ReplayContext from ascending 1-min option candles + a position.

    `idx_df` (optional) is the underlying's 1-min OHLC for the same day; it is
    aligned to the option timestamps and drawn as candlesticks in the top panel.
    """
    n = len(df)
    if exit_idx is None or exit_idx >= n:
        exit_idx = n - 1
    entry_idx = max(0, min(entry_idx, exit_idx))
    close = df["close"].to_numpy(dtype=float)
    ts = list(df["ts"])

    # align the underlying OHLC to the option's minute grid
    idx_o = idx_h = idx_l = idx_c = None
    idx_ylim = (0.0, 1.0)
    if idx_df is not None and not idx_df.empty:
        m = df[["ts"]].merge(idx_df[["ts", "open", "high", "low", "close"]],
                             on="ts", how="left").ffill().bfill()
        idx_o = m["open"].to_numpy(float); idx_h = m["high"].to_numpy(float)
        idx_l = m["low"].to_numpy(float); idx_c = m["close"].to_numpy(float)
        ilo, ihi = float(np.nanmin(idx_l)), float(np.nanmax(idx_h))
        ipad = max(1.0, (ihi - ilo) * 0.08)
        idx_ylim = (ilo - ipad, ihi + ipad)

    pnl = np.full(n, np.nan)
    hold = np.arange(entry_idx, exit_idx + 1)
    pnl[hold] = sign * (close[hold] - entry_price) * qty

    # running extremes across the held window only
    run_max = np.full(n, np.nan)
    run_min = np.full(n, np.nan)
    cmax, cmin = -np.inf, np.inf
    for i in hold:
        cmax = max(cmax, pnl[i]); cmin = min(cmin, pnl[i])
        run_max[i] = cmax; run_min[i] = cmin

    held = pnl[hold]
    max_p = float(np.nanmax(held)); max_p_i = int(hold[int(np.nanargmax(held))])
    max_d = float(np.nanmin(held)); max_d_i = int(hold[int(np.nanargmin(held))])
    final = float(pnl[exit_idx])
    pct_profit = float(np.mean(held > 0) * 100)

    stats = {
        "entry_time": ts[entry_idx], "entry_price": entry_price,
        "exit_time": ts[exit_idx], "exit_price": float(close[exit_idx]),
        "final_pnl": final,
        "max_profit": max_p, "max_profit_time": ts[max_p_i],
        "max_dd": max_d, "max_dd_time": ts[max_d_i],
        "pct_time_in_profit": pct_profit,
        "n_minutes": int(len(hold)),
    }

    # stable axes with padding
    p_lo, p_hi = float(np.min(close)), float(np.max(close))
    pad = max(1.0, (p_hi - p_lo) * 0.08)
    price_ylim = (p_lo - pad, p_hi + pad)
    lo, hi = float(np.nanmin(pnl)), float(np.nanmax(pnl))
    span = max(1.0, hi - lo)
    pnl_ylim = (lo - span * 0.12, hi + span * 0.12)

    return ReplayContext(ts=ts, close=close, pnl=pnl, run_max=run_max, run_min=run_min,
                         entry_idx=entry_idx, exit_idx=exit_idx, entry_price=entry_price,
                         sign=sign, qty=qty, label="", day="",
                         price_ylim=price_ylim, pnl_ylim=pnl_ylim, stats=stats,
                         idx_o=idx_o, idx_h=idx_h, idx_l=idx_l, idx_c=idx_c,
                         idx_ylim=idx_ylim)


def _hhmm(t) -> str:
    return pd.Timestamp(t).strftime("%H:%M")


def render_frame(ctx: ReplayContext, i: int, figsize=(9.6, 5.3)) -> Figure:
    """Draw the replay up to minute `i`: NIFTY 50 candles on the primary (left)
    axis with cumulative P&L overlaid as a dotted line on a secondary (right)
    axis. Returns a matplotlib Figure."""
    i = int(max(ctx.entry_idx, min(i, ctx.exit_idx)))
    x = np.arange(len(ctx.close))
    fig = Figure(figsize=figsize, facecolor=BG)
    ax = fig.subplots()
    fig.subplots_adjust(left=0.075, right=0.912, top=0.80, bottom=0.115)
    ax.set_facecolor(BG)
    axr = ax.twinx()                                                   # secondary: P&L
    axr.set_facecolor("none")

    cur_pnl = ctx.pnl[i]
    cur_col = C_PROFIT if cur_pnl >= 0 else C_LOSS
    cur_px = ctx.close[i]
    rmax, rmin = ctx.run_max[i], ctx.run_min[i]

    # ---------- banner ----------
    fig.text(0.075, 0.955, ctx.label, fontsize=13, fontweight="bold", color=INK_HI)
    fig.text(0.075, 0.912, f"{ctx.day}   ·   entry {_hhmm(ctx.stats['entry_time'])} "
                           f"@ {ctx.entry_price:.2f}", fontsize=9.5, color=C_MUTED)
    fig.text(0.912, 0.955, _hhmm(ctx.ts[i]), fontsize=15.5, fontweight="bold",
             color=INK_HI, ha="right")
    fig.text(0.912, 0.905, f"{_fmt_rupees(cur_pnl)}   ({ctx.sign*(cur_px-ctx.entry_price):+.2f} pts)",
             fontsize=15.5, fontweight="bold", color=cur_col, ha="right")
    spot_txt = f"{ctx.idx_name} {ctx.idx_c[i]:,.0f}     " if ctx.idx_c is not None else ""
    fig.text(0.60, 0.905, f"{spot_txt}LTP {cur_px:.2f}", fontsize=10.5, color=INK, ha="right")

    # ---------- primary axis: NIFTY 50 candlesticks ----------
    if ctx.idx_c is not None:
        o, h, lo_, c = ctx.idx_o, ctx.idx_h, ctx.idx_l, ctx.idx_c

        def _segs(xarr, y0, y1):                                       # vectorized
            s = np.empty((len(xarr), 2, 2))
            s[:, 0, 0] = xarr; s[:, 0, 1] = y0
            s[:, 1, 0] = xarr; s[:, 1, 1] = y1
            return s

        def _candles(sl, colors, zorder, wlw=0.8, blw=2.4):           # 2 artists total
            xs = x[sl]
            if len(xs) == 0:
                return
            ax.add_collection(LineCollection(_segs(xs, lo_[sl], h[sl]), colors=colors,
                                             linewidths=wlw, zorder=zorder))        # wicks
            ax.add_collection(LineCollection(_segs(xs, o[sl], c[sl]), colors=colors,
                                             linewidths=blw, zorder=zorder,
                                             capstyle="round"))                     # bodies

        _candles(slice(i + 1, len(x)), C_FUT, 1)                      # future = dim
        up = c[: i + 1] >= o[: i + 1]
        _candles(slice(0, i + 1), np.where(up, C_UP, C_DOWN), 3)     # travelled
        ax.set_ylim(*ctx.idx_ylim)
        ax.set_ylabel(ctx.idx_name, color=INK, fontsize=10)
        candle_lbl = f"{ctx.idx_name} · 1-min"
    else:                                                             # fallback: option line
        ax.plot(x, ctx.close, color=C_FUT, lw=1.0, zorder=1)
        ax.plot(x[ctx.entry_idx:i + 1], ctx.close[ctx.entry_idx:i + 1],
                color=C_PRICE, lw=2.0, zorder=3)
        ax.set_ylim(*ctx.price_ylim)
        ax.set_ylabel("Premium", color=INK, fontsize=10)
        candle_lbl = "Option premium"
    ax.axvline(ctx.entry_idx, color=C_ENTRY, ls=(0, (4, 2)), lw=1.3, alpha=0.85, zorder=4)
    ax.set_xlim(-1, len(x))
    ax.grid(True, color=GRID, alpha=1.0, lw=0.6, zorder=0)

    # ---------- secondary axis: cumulative P&L (dotted line + glow) ----------
    xh = x[ctx.entry_idx:i + 1]
    ph = ctx.pnl[ctx.entry_idx:i + 1]
    axr.axhline(0, color=C_ZERO, ls="--", lw=1.0, zorder=2)                        # 0-P&L
    axr.axhline(rmax, color=C_PROFIT, ls=":", lw=0.9, alpha=0.45, zorder=2)        # peak
    axr.axhline(rmin, color=C_LOSS, ls=":", lw=0.9, alpha=0.45, zorder=2)          # trough
    axr.fill_between(xh, ph, 0, color=C_PNL, alpha=0.08, zorder=3)                 # faint depth
    axr.plot(xh, ph, color=C_PNL, lw=5.0, alpha=0.16, zorder=4,
             solid_capstyle="round")                                              # glow
    axr.plot(xh, ph, color=C_PNL, lw=2.2, zorder=5, solid_capstyle="round")       # solid P&L
    # mark ENTRY at the 0-P&L point
    axr.scatter([ctx.entry_idx], [0], color=C_ENTRY, s=60, zorder=7,
                edgecolor=BG, lw=1.4)
    axr.annotate("entry · 0 P&L", (ctx.entry_idx, 0), textcoords="offset points",
                 xytext=(7, 8), fontsize=8, color=C_ENTRY, fontweight="bold")
    # current point → small 🚀 rocket with the live P&L pinned above it (the eye
    # tracks the rocket, so the number rides along). Falls back to a dot.
    rocket = _rocket_rgba()
    if rocket is not None:
        ab = AnnotationBbox(OffsetImage(rocket, zoom=0.155), (i, cur_pnl),
                            frameon=False, zorder=9, box_alignment=(0.5, 0.5),
                            annotation_clip=True)
        axr.add_artist(ab)
    else:
        axr.scatter([i], [cur_pnl], color=cur_col, s=70, zorder=8, edgecolor=BG, lw=1.3)
    axr.annotate(_fmt_rupees(cur_pnl), (i, cur_pnl), textcoords="offset points",
                 xytext=(0, 17), ha="center", va="bottom", fontsize=10.5,
                 fontweight="bold", color=cur_col, zorder=11, annotation_clip=False,
                 bbox=dict(boxstyle="round,pad=0.24", fc="#0e1320", ec=cur_col,
                           lw=0.9, alpha=0.94))
    axr.set_ylim(*ctx.pnl_ylim)
    axr.set_ylabel("P&L (₹)", color=C_PNL, fontsize=10, fontweight="bold")
    axr.tick_params(axis="y", colors=C_PNL, labelsize=8)

    # legend chips (top corners, on-surface)
    ax.text(0.010, 0.955, candle_lbl, transform=ax.transAxes, fontsize=8.5, color=INK,
            fontweight="bold", va="top",
            bbox=dict(boxstyle="round,pad=0.28", fc="#1c2230", ec=SPINE, lw=0.6, alpha=0.9))
    axr.text(0.988, 0.955, f"━ P&L   peak {_fmt_rupees(rmax)}  ·  trough {_fmt_rupees(rmin)}",
             transform=axr.transAxes, fontsize=8.5, color=C_PNL, fontweight="bold",
             va="top", ha="right",
             bbox=dict(boxstyle="round,pad=0.28", fc="#1c2230", ec=SPINE, lw=0.6, alpha=0.9))

    # spines / ticks styling
    for spine in ("top",):
        ax.spines[spine].set_visible(False); axr.spines[spine].set_visible(False)
    for a in (ax, axr):
        for s in a.spines.values():
            s.set_color(SPINE); s.set_linewidth(0.8)
    ax.tick_params(colors=INK, labelsize=8, length=0)

    # x tick labels HH:MM
    n = len(x)
    ticks = list(range(0, n, max(1, n // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([_hhmm(ctx.ts[t]) for t in ticks], fontsize=8, color=INK)
    return fig


def _frame_rgb(ctx: ReplayContext, i: int, dpi: int = 100):
    """Render frame `i` to a contiguous (H, W, 3) uint8 RGB array via the Agg
    canvas. Dimensions are forced even so H.264/yuv420p accepts them."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = render_frame(ctx, i)
    fig.set_dpi(dpi)
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    w, h = canvas.get_width_height()
    buf = np.asarray(canvas.buffer_rgba())[:, :, :3]     # drop alpha
    h -= h % 2                                            # even height for yuv420p
    w -= w % 2                                            # even width
    buf = np.ascontiguousarray(buf[:h, :w])
    fig.clear()
    return buf, w, h


def render_mp4(ctx: ReplayContext, duration_s: float = 12.0, dpi: int = 100,
               target_fps: int = 24, max_frames: int = 360) -> bytes:
    """Render the whole replay (entry→exit) to an H.264 MP4 lasting ~`duration_s`
    seconds, and return the bytes.

    Uses the imageio-ffmpeg bundled libx264 encoder (no system ffmpeg needed) and
    the OO Agg canvas (no pyplot) so it is safe to call from Streamlit's thread.
    """
    import os
    import tempfile

    import imageio_ffmpeg

    span = ctx.exit_idx - ctx.entry_idx + 1
    frames_wanted = min(max_frames, max(24, round(duration_s * target_fps)))
    step = max(1, -(-span // frames_wanted))             # ceil(span / frames_wanted)
    idxs = list(range(ctx.entry_idx, ctx.exit_idx + 1, step))
    if idxs[-1] != ctx.exit_idx:
        idxs.append(ctx.exit_idx)
    # pick fps so the moving part lasts ~duration_s (clamped to sane playback)
    fps = int(min(60, max(6, round(len(idxs) / max(0.5, duration_s)))))
    idxs += [ctx.exit_idx] * max(1, round(fps * 0.7))    # ~0.7s hold on the close

    first, w, h = _frame_rgb(ctx, idxs[0], dpi)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    try:
        writer = imageio_ffmpeg.write_frames(
            tmp.name, size=(w, h), fps=fps, codec="libx264",
            pix_fmt_in="rgb24", pix_fmt_out="yuv420p", macro_block_size=1,
            output_params=["-movflags", "+faststart"])
        writer.send(None)                                # seed the generator
        writer.send(first.tobytes())
        for i in idxs[1:]:
            frame, _, _ = _frame_rgb(ctx, i, dpi)
            writer.send(frame.tobytes())
        writer.close()
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def render_gif(ctx: ReplayContext, duration_s: float = 12.0, dpi: int = 80,
               target_fps: int = 14, max_frames: int = 140) -> bytes:
    """Render the replay to an animated GIF and return the bytes.

    GIFs are 256-colour and heavier than MP4, so we use a lower dpi / fps / frame
    count and a single shared palette (from the fullest frame) to keep size and
    flicker down. Reuses _frame_rgb (OO Agg, no pyplot) — thread-safe.
    """
    import io as _io

    from PIL import Image

    span = ctx.exit_idx - ctx.entry_idx + 1
    frames_wanted = min(max_frames, max(16, round(duration_s * target_fps)))
    step = max(1, -(-span // frames_wanted))
    idxs = list(range(ctx.entry_idx, ctx.exit_idx + 1, step))
    if idxs[-1] != ctx.exit_idx:
        idxs.append(ctx.exit_idx)
    fps = min(30, max(4, round(len(idxs) / max(0.5, duration_s))))
    hold = max(1, round(fps * 0.7))
    idxs += [ctx.exit_idx] * hold

    imgs = [Image.fromarray(_frame_rgb(ctx, i, dpi)[0]) for i in idxs]
    # build one palette from the last (fullest) frame → consistent colours, smaller file
    pal = imgs[-1].convert("P", palette=Image.ADAPTIVE, colors=128)
    frames_p = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]

    bio = _io.BytesIO()
    frames_p[0].save(bio, format="GIF", save_all=True, append_images=frames_p[1:],
                     duration=round(1000 / fps), loop=0, optimize=True, disposal=2)
    return bio.getvalue()


if __name__ == "__main__":
    import upstox_data as ud
    exp = ud.expiries("NIFTY")[0]
    ks = ud.strikes("NIFTY", exp)
    atm = min(ks, key=lambda k: abs(k - 24350))
    info = ud.resolve("NIFTY", exp, atm, "CE")
    days = ud.available_days(info["instrument_key"])
    df = ud.fetch_day_1m(info["instrument_key"], days[1])
    ctx = compute(df, entry_idx=0, entry_price=float(df["close"].iloc[0]),
                  sign=1, qty=65)
    ctx.label = f"LONG 1 lot NIFTY {int(atm)} CE"
    ctx.day = days[1]
    print("stats:", {k: (str(v) if hasattr(v, "strftime") else v) for k, v in ctx.stats.items()})
    fig = render_frame(ctx, len(df) - 1)
    out = "/tmp/replay_test.png"
    fig.savefig(out, dpi=110)
    print("saved", out)
