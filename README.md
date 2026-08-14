# Usage Cost Monitor

Streamlit dashboard built on top of `usage_cost.py` — reads DeepSeek usage
exports (either an uploaded **sheet** or `usage_data_*.zip` from `_data/usage/`),
buckets models into `flash` / `ds pro` via an explicit allowlist, and projects
what the same usage would cost across four providers: DeepSeek, OpenAI, Claude,
and OpenCode Go.

## Features

Two views, switched via tabs:

1. **Cost projection** — a 2x2 grid of **chart-only** quadrants (DeepSeek,
   OpenAI, Claude, OpenCode Go). Each quadrant shows:
   - a Plotly bar chart of provider total cost vs cache hit rate over **90–100%**
     (11 bars) with **graded monotonic colors** (`Blues` colorscale — color
     deepens as the rate rises);
   - a **red marker** + **dashed red vline** at the current cache hit rate;
   - an `st.metric` showing the cost at the selected rate.

   All per-bucket price overrides (`hit` / `miss` / `out` $/1M) now live in the
   **collapsible left sidebar** — one `st.expander` per provider (collapsed by
   default) — with invalid → default fallback (one warning per provider). The
   four cost charts are fed by the sidebar-built `provider_prices`.

### Sheet selection

The sidebar has a **Provider** dropdown (only `deepseek` for now — other
providers are forward-looking) and a **Browse sheet** uploader that accepts a
DeepSeek usage **zip** (`cost-*.csv` + `amount-*.csv`) **or a single
amount/cost CSV**, loaded via `usage_cost.load_usage_from_upload`. When nothing
is uploaded, the app falls back to the data directory (`discover_zips`). The
active source is shown as a caption (`Sheet: <name>` or `Directory: <path>`).

2. **Usage analytics** (driven by `usage_cost.daily_summary`) — daily charts
   with total annotations:
   - **Per-model daily cache hit rates** — line plots (`flash` / `ds pro`);
   - **Total daily cache hit rates** — overall daily `hit_rate %`;
   - **Daily actual cost (USD)**;
   - **Daily requests**;
   - sidebar KPIs: total cost, total tokens, total requests, overall cache
     hit rate %, and effective $/1M (`actual cost / total_tokens * 1e6`).

### Cache-hit-rate control

There is **one shared sidebar slider** (`Cache hit rate %`, 90–100) that drives
**all four** cost-projection charts — there are no per-quadrant sliders. It
defaults to the actual cache hit rate from the data (`hit / (hit + miss)`),
clamped into [90, 100].

## Setup & run

From the project root (the folder containing `app.py` and `usage_cost.py`):

```bash
cd <project root>                              # 1. go to the project root
python3.11 -m venv .venv                       # 2. create the venv (one time)
.venv/bin/pip install -r requirements.txt      # 3. install dependencies (one time)
.venv/bin/streamlit run app.py                 # 4. start the dashboard
```

Or just `./run.sh` — it creates the venv + installs deps on first run, then
starts the app. The `.venv` lives in the project root, next to `app.py`.

Dependencies: `streamlit>=1.30`, `plotly>=5.18` (nothing else). Open the printed
local URL (default `http://localhost:8501`).

## Data source

Either upload a DeepSeek sheet in the sidebar (a zip containing `cost-*.csv` +
`amount-*.csv`, or a single amount/cost CSV) or point the app at a directory of
monthly exports `_data/usage/usage_data_*.zip`. If no data is found the app
shows an info message and stops.

## Cost model

See the `usage_cost.py` docstring. The dashboard uses DeepSeek **new off-peak**
pricing for the DeepSeek quadrant, OpenAI / Claude **cache-write** billing, and
OpenCode Go **meter** rates loaded offline (embedded defaults if no cache).

## Tests

Tests live in `tests/` next to the app. From the project root:

```bash
python3.11 -m pytest tests -q
```

Covers bucketing, cost math, price-table parsing, the DeepSeek sheet-format
loader (`load_usage_from_upload`: zip bytes, single amount CSV, single cost
CSV, unknown-model handling), `total_tokens`, `cost_at_hitrate`,
`request_count` capture, and `daily_summary`.
