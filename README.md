# Usage Cost Monitor

Streamlit dashboard (v2) built on top of `usage_cost.py` — reads DeepSeek usage
exports (`usage_data_*.zip`) from `_data/usage/`, buckets models into
`flash` / `ds pro` via an explicit allowlist, and projects what the same usage
would cost across four providers: DeepSeek, OpenAI, Claude, and OpenCode Go.

## Features (v2)

Two views, switched via tabs:

1. **Cost projection** — a 2x2 grid of quadrants (DeepSeek, OpenAI, Claude,
   OpenCode Go). Each quadrant has:
   - per-bucket price overrides (`hit` / `miss` / `out` $/1M) with
     invalid → default fallback;
   - a Plotly bar chart of provider total cost vs cache hit rate over **90–100%**
     (11 bars) with **graded monotonic colors** (`Blues` colorscale — color
     deepens as the rate rises);
   - a **red marker** + **dashed red vline** at the current cache hit rate;
   - an `st.metric` showing the cost at the selected rate.

2. **Usage analytics** (driven by `usage_cost.daily_summary`) — daily charts
   with total annotations:
   - **Daily compute (total tokens)** — `hit + miss + out` per day;
   - **Per-model daily cache hit rates** — grouped bars (`flash` / `ds pro`);
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

> **"Daily compute"** is modeled as **daily total tokens** (`hit + miss + out`)
> because the usage data has no wall-clock compute time.

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Dependencies: `streamlit>=1.30`, `plotly>=5.18` (nothing else).

## Run

From the repository root:

```bash
.venv/bin/streamlit run .dev/usage_monitor/app.py
```

Open the printed local URL (default `http://localhost:8501`).

## Data source

Monthly exports in `_data/usage/usage_data_*.zip`, each containing
`cost-*.csv` and `amount-*.csv`. The data directory can be changed in the
sidebar. If no zips are found the app shows an info message and stops.

## Cost model

See the `usage_cost.py` docstring. The dashboard uses DeepSeek **new off-peak**
pricing for the DeepSeek quadrant, OpenAI / Claude **cache-write** billing, and
OpenCode Go **meter** rates loaded offline (embedded defaults if no cache).

## Tests

```bash
/usr/local/bin/python3.11 -m pytest tests/test_usage_cost.py -q
```

Covers bucketing, cost math, price-table parsing, `total_tokens`,
`cost_at_hitrate`, `request_count` capture, and `daily_summary`.
