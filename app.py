#!/usr/bin/env python3
"""app.py — Streamlit dashboard (v3) for the usage cost estimator.

Two views (tabs):

  1. Cost projection — a 2x2 grid of CHART-ONLY quadrants (DeepSeek, OpenAI,
     Claude, OpenCode Go).  ONE shared sidebar slider (Cache hit rate %,
     90..100) drives all four charts.  Each quadrant shows a Plotly bar chart
     of provider total cost vs cache hit rate with GRADED MONOTONIC colors, a
     red marker + dashed red vline at the current shared rate, and a cost
     metric at that rate.  Per-bucket price overrides (hit/miss/out $/1M) live
     in the collapsible left sidebar — one expander per provider.

  2. Usage analytics — per-model daily cache hit rates, total daily cache hit
     rates, daily actual cost, and daily requests, each with total
     annotations and sidebar KPIs.

Data source: the sidebar has a "Browse sheet" uploader (Provider dropdown —
only 'deepseek' for now) that accepts a DeepSeek usage zip (cost-*.csv +
amount-*.csv) or a single amount/cost CSV via usage_cost.load_usage_from_upload.
When nothing is uploaded it falls back to the data directory (discover_zips).

Run:
    streamlit run .dev/usage_monitor/app.py
"""
from __future__ import annotations

import os
import sys

import plotly.graph_objects as go

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st  # noqa: E402

import usage_cost as uc  # noqa: E402

st.set_page_config(page_title="Usage Cost Monitor", layout="wide")

DEFAULT_DATA_DIR = os.path.join(uc.PROJECT_ROOT, "_data", "usage")
MIN_RATE, MAX_RATE = 90, 100


@st.cache_data(show_spinner=False)
def _load_usage(data_dir):
    zips = uc.discover_zips(data_dir)
    if not zips:
        return None
    return uc.load_usage(zips)


@st.cache_data(show_spinner=False)
def _load_usage_from_upload(name, data):
    """Load a DeepSeek sheet from an uploaded file (zip or single CSV)."""
    return uc.load_usage_from_upload(name, data)


@st.cache_data(show_spinner=False)
def _load_opencode_prices():
    return uc.load_opencode_prices(offline=True)


def _clamp_rate(pct):
    return int(min(MAX_RATE, max(MIN_RATE, round(pct))))


opencode = _load_opencode_prices()

# ---- Provider defaults (sidebar price-override prefill) -------------------

PROVIDERS = [
    ("deepseek", "DeepSeek (new, off-peak)",
     uc.resolve_provider_prices("deepseek_new_offpeak", 0.0, "write", None)),
    ("openai", "OpenAI (Sol + Luna, cache-write)",
     uc.resolve_provider_prices("openai", 0.0, "write", None)),
    ("claude", "Claude (Haiku + Opus, cache-write)",
     uc.resolve_provider_prices("claude", 0.0, "write", None)),
    ("opencode", "OpenCode Go (meter)",
     opencode.get("prices") or uc.DEFAULT_OPENCODE),
]


# ---- Sidebar: sheet selection + data source --------------------------------

with st.sidebar:
    st.subheader("Sheet selection")
    st.selectbox(
        "Provider",
        ["deepseek"],
        help="Only DeepSeek sheets are supported for now.",
        key="sheet_provider",
    )
    uploaded = st.file_uploader(
        "Browse sheet",
        type=["csv", "zip"],
        help="Upload a DeepSeek usage export — a zip (cost-*.csv + amount-*.csv) "
             "or a single amount/cost CSV.",
        key="sheet_upload",
    )

    if uploaded is not None:
        usage = _load_usage_from_upload(uploaded.name, uploaded.getvalue())
        st.caption(f"Sheet: {uploaded.name}")
    else:
        data_dir = st.text_input(
            "Data directory",
            value=DEFAULT_DATA_DIR,
            help="Directory containing usage_data_*.zip monthly exports.",
            key="data_dir",
        )
        st.caption(f"Directory: {data_dir}")
        usage = _load_usage(data_dir)

    if usage is None:
        st.info("No usage data — upload a DeepSeek sheet (zip or CSV) or add monthly "
                "zips to the data directory.")
        st.stop()

    daily = uc.daily_summary(usage)
    if not daily:
        st.warning("No usage rows found in the selected source.")
        unknown = usage.get("unknown_models") or {}
        if unknown:
            st.error("The sheet contains model names that are not in the supported "
                     "allowlist. Supported models: `deepseek-v4-flash`, `deepseek-v4-pro`.")
            for model, info in sorted(unknown.items()):
                tok = ", ".join(f"{t}={v:,.0f}" for t, v in sorted(info.get("tokens", {}).items()))
                st.write(f"- **{model}**: cost=${info.get('cost', 0.0):,.2f}; tokens [{tok}]")
        else:
            members = usage.get("members") or []
            st.error("No recognized usage rows found. Expected zip members named "
                     "`cost-*.csv` / `amount-*.csv` with headers `model` and `start_time_iso`.")
            st.write(f"Zip members found: {', '.join(members) if members else '(none)'}")
        st.stop()

    tokens = uc.total_tokens(usage)
    total_cost = sum(r["cost"] for r in daily)
    total_tokens = sum(sum(b.values()) for b in tokens.values())
    total_requests = sum(r["requests"] for r in daily)
    total_hit = sum(r["hit"] for r in daily)
    total_miss = sum(r["miss"] for r in daily)
    overall_hit_pct = (total_hit / (total_hit + total_miss) * 100.0) if (total_hit + total_miss) else 0.0
    effective_per_1m = (total_cost / total_tokens * 1e6) if total_tokens else 0.0

    # ---- Shared cache-hit-rate control + analytics KPIs -------------------
    st.divider()
    st.metric("Actual DeepSeek cost", f"${total_cost:,.2f}")
    hitrate = st.slider(
        "Cache hit rate %",
        MIN_RATE, MAX_RATE,
        value=_clamp_rate(overall_hit_pct),
        help="Applied to all four cost-projection charts (90–100%).",
        key="shared_hitrate",
    )
    st.divider()
    st.subheader("Usage analytics KPIs")
    st.metric("Total cost (USD)", f"${total_cost:,.2f}")
    st.metric("Total tokens", f"{total_tokens:,.0f}")
    st.metric("Total requests", f"{total_requests:,.0f}")
    st.metric("Overall cache hit rate", f"{overall_hit_pct:.1f}%")
    st.metric("Effective $/1M", f"${effective_per_1m:,.2f}")

    # ---- Price overrides: collapsible per-provider expanders --------------
    st.divider()
    st.subheader("Price overrides ($/1M)")
    provider_prices = {}
    for key, display, default_prices in PROVIDERS:
        with st.expander(display, expanded=False):
            prices = {}
            invalid = False
            for bucket in uc.BUCKETS:
                cols = st.columns(3)
                for i, field in enumerate(("hit", "miss", "out")):
                    dv = float(default_prices.get(bucket, {}).get(field, 0.0))
                    wid_key = f"side_{key}_{bucket.replace(' ', '_')}_{field}"
                    raw = cols[i].text_input(f"{bucket} {field} $/1M", value=f"{dv:g}", key=wid_key)
                    try:
                        prices.setdefault(bucket, {})[field] = float(raw)
                    except ValueError:
                        prices.setdefault(bucket, {})[field] = dv
                        invalid = True
            if invalid:
                st.warning("Some price fields were invalid — using defaults for those fields.")
            provider_prices[key] = prices


# ---- Plotly helpers --------------------------------------------------------

def _base_layout(fig, title, y_title, nticks=15):
    fig.update_layout(
        height=300,
        title=title,
        margin=dict(l=10, r=10, t=40, b=10),
        yaxis_title=y_title,
        bargap=0.2,
        showlegend=False,
    )
    fig.update_xaxes(tickangle=-45, nticks=nticks)
    return fig


def _top_right_annotation(fig, text):
    fig.add_annotation(
        x=1, y=1, xref="paper", yref="paper",
        text=text, showarrow=False,
        xanchor="right", yanchor="top",
        font=dict(size=12),
    )
    return fig


def _norm(values, max_value):
    return [v / max_value if max_value else 0.0 for v in values]


# ---- Cost view -------------------------------------------------------------

def _cost_chart(prices, hitrate):
    rates = list(range(MIN_RATE, MAX_RATE + 1))
    costs = [uc.cost_at_hitrate(tokens, prices, r / 100.0) for r in rates]
    sel = uc.cost_at_hitrate(tokens, prices, hitrate / 100.0)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=rates,
        y=costs,
        marker=dict(color=[(r - MIN_RATE) / (MAX_RATE - MIN_RATE) for r in rates], colorscale="Blues"),
        name="Total cost",
    ))
    fig.add_trace(go.Scatter(
        x=[hitrate],
        y=[sel],
        mode="markers",
        marker=dict(color="red", size=12, line=dict(color="white", width=1)),
        name=f"@{hitrate}%",
    ))
    fig.add_vline(x=hitrate, line_dash="dash", line_color="red", opacity=0.6)
    _base_layout(fig, "Provider total cost vs cache hit rate", "Cost (USD)", nticks=11)
    fig.update_xaxes(tickangle=0, tickmode="linear", dtick=1)
    st.plotly_chart(fig, width="stretch")
    st.metric(f"Cost at {hitrate}% hit rate", f"${sel:,.2f}")


def _cost_quadrant(col, display, prices, hitrate):
    """Chart-only quadrant: provider title + graded cost chart + metric."""
    with col:
        st.subheader(display)
        _cost_chart(prices, hitrate)


# ---- Analytics view --------------------------------------------------------

def _analytics_view(overall_hit_pct):
    days = [r["day"] for r in daily]

    # a. Per-model daily cache hit rates
    flash = [r["buckets"]["flash"]["hit_rate"] * 100 for r in daily]
    pro = [r["buckets"]["ds pro"]["hit_rate"] * 100 for r in daily]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=days, y=flash, mode="lines+markers", name="flash",
                             line=dict(color="#1f77b4")))
    fig.add_trace(go.Scatter(x=days, y=pro, mode="lines+markers", name="ds pro",
                             line=dict(color="#ff7f0e")))
    _top_right_annotation(fig, f"Overall cache hit rate: {overall_hit_pct:.1f}%")
    _base_layout(fig, "Per-model daily cache hit rates", "Hit rate %")
    fig.update_layout(showlegend=True)
    st.plotly_chart(fig, width="stretch")

    # b. Total daily cache hit rates
    overall = [r["hit_rate"] * 100 for r in daily]
    fig = go.Figure(go.Bar(
        x=days, y=overall,
        marker=dict(color=_norm(overall, 100.0), colorscale="Blues"),
    ))
    _top_right_annotation(fig, f"Overall cache hit rate: {overall_hit_pct:.1f}%")
    _base_layout(fig, "Total daily cache hit rates", "Hit rate %")
    st.plotly_chart(fig, width="stretch")

    # c. Daily actual cost (USD)
    costs = [r["cost"] for r in daily]
    total_cost_v = sum(costs)
    fig = go.Figure(go.Bar(
        x=days, y=costs,
        marker=dict(color=_norm(costs, total_cost_v), colorscale="Greens"),
    ))
    _top_right_annotation(fig, f"Total: ${total_cost_v:,.2f}")
    _base_layout(fig, f"Daily actual cost (USD) — Total: ${total_cost_v:,.2f}", "USD")
    st.plotly_chart(fig, width="stretch")

    # d. Daily requests
    reqs = [r["requests"] for r in daily]
    total_req = sum(reqs)
    fig = go.Figure(go.Bar(
        x=days, y=reqs,
        marker=dict(color=_norm(reqs, total_req), colorscale="Purples"),
    ))
    _top_right_annotation(fig, f"Total: {total_req:,.0f} requests")
    _base_layout(fig, f"Daily requests — Total: {total_req:,.0f}", "Requests")
    st.plotly_chart(fig, width="stretch")


# ---- Body ------------------------------------------------------------------

tab_cost, tab_analytics = st.tabs(["Cost projection", "Usage analytics"])

with tab_cost:
    row1 = st.columns(2)
    row2 = st.columns(2)
    cells = [row1[0], row1[1], row2[0], row2[1]]
    for col, (key, display, _default_prices) in zip(cells, PROVIDERS):
        _cost_quadrant(col, display, provider_prices[key], hitrate)

with tab_analytics:
    _analytics_view(overall_hit_pct)
