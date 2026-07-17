# 📈 NIFTY Intraday Option P&L Replay & Simulator

A Streamlit app for **NIFTY / BankNifty option traders** that does two things:

1. **🎬 Intraday Replay** — pick a contract + your entry, and watch how your P&L
   would have moved **minute-by-minute through a real trading day**, rendered as a
   video/GIF: NIFTY 50 1-minute candles with your P&L riding on top. 🚀
2. **📈 Live P&L Simulator** — a live Black-Scholes payoff + time-decay grid for an
   option position, using the current NSE snapshot.

**No broker login, no API key** — all data comes from public endpoints.

![demo](docs/demo.gif)

---

## Why it's useful

Brokers show you the *final* P&L of a trade. They don't show you the **journey** —
how close you were to the peak, how deep the drawdown got, how long you sat in
profit. This replays the whole day on **real 1-minute traded prices** so you can
review your entry/exit like game tape.

The P&L matches your broker exactly: `(LTP − entry) × qty`, gross — e.g. long 1 lot
NIFTY 24300 CE @ 72.55 closing at 141.95 → **+₹4,511 (+95.66%)**.

---

## Features

**🎬 Intraday Replay (default tab)**
- Real **1-minute OHLC** for any listed strike (CE/PE), NIFTY / BANKNIFTY / FINNIFTY.
- **NIFTY 50 candlesticks** on the primary axis; **P&L (₹)** on a secondary axis with
  your **entry marked at 0 P&L** and a 🚀 tracking the live P&L.
- Pick **entry time & price**, **exit time**, **lots**, and **video length** (5–35 s).
- Export the replay as an **MP4** (H.264) or an **animated GIF**.
- Running **max profit / max drawdown / % time in profit**, plus **Return %** (broker "Chg.").
- Fetched days are **cached to disk** (`min_cache/`) — replays are instant on re-open.

**📈 Live P&L Simulator**
- Live NSE spot + option LTP (public option-chain), Black-Scholes reprice.
- Payoff chart (now vs at-expiry) + a colored **spot × time-decay** P&L matrix.
- Manual override if NSE blocks the host.

![Live P&L Simulator](docs/live_tab.png)

---

## Data sources (all public, no login)

| What | Source |
|------|--------|
| Historical **1-minute** option & index candles | Upstox public `historical-candle` API + instrument master |
| Live option-chain snapshot (Live tab) | NSE public option-chain API |

> **Coverage note:** the replay works for contracts **still listed** (expiry ≥ today),
> over their full life — i.e. the last few weeks of the current weekly/monthly. Already
> **expired** weekly contracts drop out of the free master and aren't retrievable here.
> Great for reviewing **recent** trades.

---

## Quick start

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501.

**Command-line** version of the live simulator:
```bash
python3 simulate.py --strike 24300 --type CE --entry 72.55 --lots 1
```

---

## Project structure

```
app.py            Streamlit app — both tabs (entry point)
upstox_data.py    Free 1-min candle fetch (Upstox) + instrument master + disk cache
replay_engine.py  P&L computation + dark-theme frame renderer → MP4 / GIF
bs.py             Black-Scholes pricing (no scipy)
nse_data.py       Live NSE option-chain / spot (for the Live tab)
simulate.py       CLI for the live simulator
requirements.txt  Dependencies
docs/             Demo media
```

Charts render as matplotlib PNGs and tables as HTML — the app is deliberately
**pyarrow-free** to avoid a Streamlit/pyarrow thread-crash on Python 3.14+
(see the comment block at the top of `app.py`).

---

## Requirements

Python 3.10+, and the packages in `requirements.txt` (Streamlit, pandas, matplotlib,
requests, imageio-ffmpeg, Pillow). The MP4 encoder ships with `imageio-ffmpeg` — no
system `ffmpeg` needed.

> The 🚀 marker uses the system color-emoji font (Apple Color Emoji on macOS); if it's
> unavailable the app falls back to a colored dot automatically.

---

## Disclaimer

For **education and personal trade review only**. Not investment advice. Data may be
delayed or incomplete; P&L figures are **gross** (no brokerage/STT/taxes). Verify
anything important against your broker. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE). (Replace `<YOUR NAME>` in the license file.)
