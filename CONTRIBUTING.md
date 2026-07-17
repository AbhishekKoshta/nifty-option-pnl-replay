# Contributing

Thanks for your interest! This is a small, self-contained Streamlit app — easy to
hack on.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

No API keys or login are needed; all data comes from public NSE / Upstox endpoints.

## Project layout

- `app.py` — Streamlit UI (both tabs).
- `upstox_data.py` — 1-minute candle fetch + instrument master + on-disk cache.
- `replay_engine.py` — P&L math + the dark-theme frame renderer (MP4 / GIF).
- `bs.py`, `nse_data.py`, `simulate.py` — Black-Scholes + Live-tab data + CLI.

## Ground rules

- **Keep the app pyarrow-free.** Do **not** reintroduce `st.dataframe`,
  `st.altair_chart`, or the native `st.*_chart` helpers — Streamlit serialises those
  to Arrow, which segfaults in its per-rerun thread on some Python builds. Render
  charts as matplotlib PNGs (`st.image`) and tables as HTML (`st.html`). See the
  comment block at the top of `app.py`.
- **No pyplot in `replay_engine.py`.** Use the object-oriented `Figure` API so
  rendering is thread-safe.
- Keep it **login-free** — no broker credentials or paid data sources.

## Before opening a PR

```bash
ruff check --select E9,F63,F7,F82 .   # what CI runs
python -m compileall -q .
```

Please describe what you changed and include a screenshot/GIF if it's a UI change.
By contributing you agree your work is licensed under the repo's MIT License.
