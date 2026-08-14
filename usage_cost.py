#!/usr/bin/env python3
"""usage_cost.py — LLM usage data -> multi-provider cost estimator + charts.

Reads DeepSeek usage exports from _data/usage/ (one zip per month, each
containing `cost-*.csv` + `amount-*.csv`), buckets models into `flash` /
`ds pro` via an explicit allowlist, and projects what the same usage would
cost across providers:

    * DeepSeek  — new pricing, off-peak / peak / blended (--peak-fraction)
    * OpenAI    — Sol + Luna (--cache-billing write|input)
    * Claude    — Opus + Haiku (--cache-billing write|input)
    * OpenCode Go — meter rates (refreshed from opencode.ai/docs/go/)

The core is stdlib-only (zipfile, csv, argparse, json, datetime, urllib, re,
html).  matplotlib is optional: if importable it writes PNG charts, otherwise
the script falls back to pure-stdlib SVG charts.

Usage:
    python .dev/usage_monitor/usage_cost.py --data-dir _data/usage
"""

from __future__ import annotations

import argparse
import collections
import copy
import csv
import datetime as _dt
import glob
import html
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Explicit allowlist: model -> bucket.  NO substring heuristics.
MODEL_BUCKET = {
    "deepseek-v4-flash": "flash",
    "deepseek-v4-pro": "ds pro",
}

BUCKETS = ("flash", "ds pro")

# amount-*.csv `type` values that carry billable tokens (request_count handled
# separately).
TOKEN_TYPES = ("input_cache_hit_tokens", "input_cache_miss_tokens", "output_tokens")
_TYPE_TO_FIELD = {
    "input_cache_hit_tokens": "hit",
    "input_cache_miss_tokens": "miss",
    "output_tokens": "out",
}

# Per-1M-token prices in USD.
PRICING = {
    "deepseek_new_offpeak": {
        "flash": {"hit": 0.007, "miss": 0.22, "out": 0.66},
        "ds pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
    },
    "deepseek_new_peak": {
        "flash": {"hit": 0.014, "miss": 0.44, "out": 1.32},
        "ds pro": {"hit": 0.044, "miss": 1.32, "out": 3.96},
    },
    "openai": {
        "flash": {"hit": 0.02, "miss_write": 0.25, "miss_input": 0.20, "out": 1.20},
        "ds pro": {"hit": 0.50, "miss_write": 6.25, "miss_input": 5.00, "out": 30.00},
    },
    "claude": {
        "flash": {"hit": 0.10, "miss_write": 1.25, "miss_input": 1.00, "out": 5.00},
        "ds pro": {"hit": 0.50, "miss_write": 6.25, "miss_input": 5.00, "out": 25.00},
    },
    "opencode_go": {
        "flash": {"hit": 0.0028, "miss": 0.14, "out": 0.28},
        "ds pro": {"hit": 0.003625, "miss": 0.435, "out": 0.87},
    },
}

# Embedded OpenCode Go meter defaults (researcher-validated).  Usage caps are
# part of the meter: flash $60/mo, pro $15/mo.
DEFAULT_OPENCODE = {
    "flash": {"hit": 0.0028, "miss": 0.14, "out": 0.28, "cached_write": 0.14, "usage_cap": 60.0},
    "ds pro": {"hit": 0.003625, "miss": 0.435, "out": 0.87, "cached_write": 0.435, "usage_cap": 15.0},
}

OPENCODE_URL = "https://opencode.ai/docs/go/"
OPENCODE_CACHE_MAX_AGE = 24 * 3600  # seconds

# (provider_key, display_name) in display order.
PROVIDER_SCENARIOS = [
    ("deepseek_new_offpeak", "DeepSeek (new, off-peak)"),
    ("deepseek_new_peak", "DeepSeek (new, peak)"),
    ("blended", "DeepSeek (new, blended)"),
    ("openai", "OpenAI (Sol + Luna)"),
    ("claude", "Claude (Opus + Haiku)"),
    ("opencode_go", "OpenCode Go"),
]

_SHORT_NAME = {
    "deepseek_new_offpeak": "DeepSeek off-peak",
    "deepseek_new_peak": "DeepSeek peak",
    "blended": "DeepSeek blended",
    "openai": "OpenAI",
    "claude": "Claude",
    "opencode_go": "OpenCode Go",
}

# Project root = parent of .dev/usage_monitor/. The default input data dir and
# chart output dir are anchored here so the script works no matter what the
# current working directory is (e.g. run from the project root or from inside
# .dev/usage_monitor/).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# OpenCode Go meter cache lives next to this script (.dev/usage_monitor/.opencode_prices.json).
OPCODE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".opencode_prices.json")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def discover_zips(data_dir):
    """Return all usage_data_*.zip files under data_dir (sorted)."""
    return sorted(glob.glob(os.path.join(data_dir, "usage_data_*.zip")))


def _parse_date(iso):
    """Day-bucket key from an ISO-8601 start timestamp (e.g. 2026-06-01)."""
    try:
        return _dt.datetime.fromisoformat(iso).date().isoformat()
    except Exception:
        return str(iso)[:10]


def _day_default():
    """Fresh per-day accumulator: billable tokens, requests, and actual cost."""
    return {
        "tokens": {b: {"hit": 0.0, "miss": 0.0, "out": 0.0} for b in BUCKETS},
        "requests": {b: 0.0 for b in BUCKETS},
        "actual": {b: 0.0 for b in BUCKETS},
    }


def _load_amount(fh, months, unknown_models):
    reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
    for row in reader:
        typ = (row.get("type") or "").strip()
        try:
            amount = float(row.get("amount") or 0)
        except ValueError:
            continue
        model = (row.get("model") or "").strip()
        bucket = MODEL_BUCKET.get(model)
        day = _parse_date(row.get("start_time_iso") or "")
        if not day:
            continue
        if bucket is None:
            unk = unknown_models.setdefault(model, {"tokens": collections.defaultdict(float), "cost": 0.0})
            unk["tokens"][typ] += amount
            continue
        month = day[:7]
        m = months.setdefault(month, {"days": {}, "actual_by_model": collections.defaultdict(float)})
        d = m["days"].setdefault(day, _day_default())
        if typ in TOKEN_TYPES:
            d["tokens"][bucket][_TYPE_TO_FIELD[typ]] += amount
        elif typ == "request_count":
            d["requests"][bucket] += amount


def _load_cost(fh, months, unknown_models):
    reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
    for row in reader:
        try:
            cost = float(row.get("cost") or 0)
        except ValueError:
            continue
        model = (row.get("model") or "").strip()
        day = _parse_date(row.get("start_time_iso") or "")
        if not day:
            continue
        month = day[:7]
        m = months.setdefault(month, {"days": {}, "actual_by_model": collections.defaultdict(float)})
        m["actual_by_model"][model] += cost
        bucket = MODEL_BUCKET.get(model)
        if bucket is None:
            unknown_models.setdefault(model, {"tokens": collections.defaultdict(float), "cost": 0.0})["cost"] += cost
            continue
        d = m["days"].setdefault(day, _day_default())
        d["actual"][bucket] += cost


def load_usage(zip_paths):
    """Read cost-*.csv + amount-*.csv from each zip.

    Returns:
        months: {month: {days: {day: {"tokens": {bucket: {hit,miss,out}},
                                      "requests": {bucket: count},
                                      "actual": {bucket: cost}}},
                         "actual_by_model": {model: cost}}}
        unknown_models: {model: {"tokens": {type: amount}, "cost": cost}}
        zips: list of zip paths read
    """
    months = {}
    unknown_models = {}
    for zp in zip_paths:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if base.startswith("amount-") and base.endswith(".csv"):
                    _load_amount(z.open(name), months, unknown_models)
                elif base.startswith("cost-") and base.endswith(".csv"):
                    _load_cost(z.open(name), months, unknown_models)

    for m in months.values():
        m["days"] = dict(sorted(m["days"].items()))
        m["actual_by_model"] = dict(sorted(m["actual_by_model"].items()))
        for d in m["days"].values():
            d["actual"] = {b: d["actual"][b] for b in BUCKETS}
            d["requests"] = {b: d["requests"][b] for b in BUCKETS}
    for unk in unknown_models.values():
        unk["tokens"] = dict(sorted(unk["tokens"].items()))

    return {
        "months": dict(sorted(months.items())),
        "unknown_models": unknown_models,
        "zips": list(zip_paths),
    }


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_day(tokens, price_table):
    """cost = (hit*hit_price + miss*miss_price + out*out_price) / 1_000_000.

    tokens:      {bucket: {hit,miss,out}} (raw token counts)
    price_table: {bucket: {hit,miss,out}} (per-1M-token USD)
    Returns {bucket: cost_usd}.
    """
    out = {}
    for bucket in BUCKETS:
        t = tokens.get(bucket) or {}
        p = price_table.get(bucket) or {}
        out[bucket] = (
            float(t.get("hit", 0)) * float(p.get("hit", 0))
            + float(t.get("miss", 0)) * float(p.get("miss", 0))
            + float(t.get("out", 0)) * float(p.get("out", 0))
        ) / 1_000_000.0
    return out


def _resolve_miss(table, cache_billing):
    """OpenAI/Claude tables expose miss_write/miss_input; pick per cache_billing."""
    resolved = {}
    for bucket, p in table.items():
        miss = p.get("miss_write") if cache_billing == "write" else p.get("miss_input", p.get("miss_write"))
        resolved[bucket] = {"hit": p["hit"], "miss": miss, "out": p["out"]}
    return resolved


def resolve_provider_prices(provider_key, peak_fraction, cache_billing, opencode_prices):
    """Return {bucket: {hit,miss,out}} for one provider scenario."""
    if provider_key == "deepseek_new_offpeak":
        return PRICING["deepseek_new_offpeak"]
    if provider_key == "deepseek_new_peak":
        return PRICING["deepseek_new_peak"]
    if provider_key == "blended":
        off = PRICING["deepseek_new_offpeak"]
        peak = PRICING["deepseek_new_peak"]
        return {b: {k: peak_fraction * peak[b][k] + (1 - peak_fraction) * off[b][k]
                    for k in ("hit", "miss", "out")}
                for b in BUCKETS}
    if provider_key == "openai":
        return _resolve_miss(PRICING["openai"], cache_billing)
    if provider_key == "claude":
        return _resolve_miss(PRICING["claude"], cache_billing)
    if provider_key == "opencode_go":
        p = opencode_prices or {}
        return {b: {"hit": p[b]["hit"], "miss": p[b]["miss"], "out": p[b]["out"]}
                for b in BUCKETS if b in p}
    raise ValueError(f"unknown provider key: {provider_key}")


def build_results(usage, peak_fraction, cache_billing, opencode_prices):
    """Aggregate per-day estimates + month/grand totals for every provider.

    Returns a nested dict:
        estimates:     {provider_key: {month: {day: {bucket: cost}}}}
        month_provider:{provider_key: {month: {bucket: cost, total}}}
        grand_provider:{provider_key: {bucket: cost, total}}
        month_actual / grand_actual / actual_by_model / unknown_models / zips
    """
    months = usage["months"]
    estimates = {}
    for key, _display in PROVIDER_SCENARIOS:
        price_table = resolve_provider_prices(key, peak_fraction, cache_billing, opencode_prices)
        estimates[key] = {}
        for month, mdata in months.items():
            estimates[key][month] = {}
            for day, ddata in mdata["days"].items():
                estimates[key][month][day] = estimate_day(ddata["tokens"], price_table)

    month_provider, grand_provider = {}, {}
    for key, _display in PROVIDER_SCENARIOS:
        month_provider[key] = {}
        g = {b: 0.0 for b in BUCKETS}
        for month, mdata in months.items():
            acc = {b: 0.0 for b in BUCKETS}
            for day, est in estimates[key][month].items():
                for b in BUCKETS:
                    acc[b] += est[b]
                    g[b] += est[b]
            acc["total"] = sum(acc[b] for b in BUCKETS)
            month_provider[key][month] = acc
        g["total"] = sum(g[b] for b in BUCKETS)
        grand_provider[key] = g

    month_actual = {}
    grand_actual = {b: 0.0 for b in BUCKETS}
    actual_by_model = collections.defaultdict(float)
    for month, mdata in months.items():
        acc = {b: 0.0 for b in BUCKETS}
        for day, ddata in mdata["days"].items():
            for b in BUCKETS:
                acc[b] += ddata["actual"][b]
                grand_actual[b] += ddata["actual"][b]
        for model, c in mdata.get("actual_by_model", {}).items():
            actual_by_model[model] += c
        acc["total"] = sum(acc[b] for b in BUCKETS)
        month_actual[month] = acc
    grand_actual["total"] = sum(grand_actual[b] for b in BUCKETS)

    return {
        "months": months,
        "estimates": estimates,
        "month_provider": month_provider,
        "grand_provider": grand_provider,
        "month_actual": month_actual,
        "grand_actual": grand_actual,
        "actual_by_model": dict(actual_by_model),
        "unknown_models": usage["unknown_models"],
        "zips": usage["zips"],
    }


# ---------------------------------------------------------------------------
# UI helpers (pure functions used by the Streamlit dashboard)
# ---------------------------------------------------------------------------

def total_tokens(usage):
    """Sum token volumes across ALL months/days -> {bucket: {hit, miss, out}}.

    request_count is filtered out at load time and never appears in the per-day
    token dicts, so this is exactly the billable token total.  Returns a fresh
    dict (all buckets present, zero-filled for missing buckets).
    """
    out = {b: {"hit": 0.0, "miss": 0.0, "out": 0.0} for b in BUCKETS}
    for month in (usage or {}).get("months", {}).values():
        for day in month.get("days", {}).values():
            for bucket in BUCKETS:
                t = day.get("tokens", {}).get(bucket) or {}
                for field in ("hit", "miss", "out"):
                    out[bucket][field] += float(t.get(field, 0.0))
    return out


def cost_at_hitrate(tokens, prices, hit_rate):
    """Provider total USD when a fraction `hit_rate` (0..1) of input tokens are
    cache hits.

    tokens:   {bucket: {hit, miss, out}} raw token counts.
    prices:   {bucket: {hit, miss, out}} USD per 1M tokens.
    hit_rate: 0..1 — at 0 every input token is a miss, at 1 every input token
              is a hit.  Output tokens are unaffected by the cache hit rate.

    Per bucket: inp = hit + miss; new_hit = inp*rate; new_miss = inp*(1-rate);
    cost += (new_hit*p['hit'] + new_miss*p['miss'] + out*p['out']) / 1e6.
    Returns the provider total in USD.
    """
    rate = float(hit_rate)
    total = 0.0
    for bucket in BUCKETS:
        t = tokens.get(bucket) or {}
        p = prices.get(bucket) or {}
        inp = float(t.get("hit", 0.0)) + float(t.get("miss", 0.0))
        new_hit = inp * rate
        new_miss = inp * (1.0 - rate)
        total += (
            new_hit * float(p.get("hit", 0.0))
            + new_miss * float(p.get("miss", 0.0))
            + float(t.get("out", 0.0)) * float(p.get("out", 0.0))
        ) / 1_000_000.0
    return total


def daily_summary(usage):
    """Flatten usage into a per-day list, sorted by day.

    Each entry:
        {day, cost, requests, hit, miss, out, total, hit_rate,
         buckets: {bucket: {hit, miss, out, requests, cost, hit_rate}}}

    hit_rate = hit / (hit + miss) (0.0 when there are no input tokens).
    Missing keys ('requests'/'actual') default to 0.
    """
    rows = []
    for month in (usage or {}).get("months", {}).values():
        for day, ddata in month.get("days", {}).items():
            tokens = ddata.get("tokens", {})
            actual = ddata.get("actual", {})
            requests = ddata.get("requests", {})
            agg = {"hit": 0.0, "miss": 0.0, "out": 0.0, "cost": 0.0, "requests": 0.0}
            buckets = {}
            for bucket in BUCKETS:
                t = tokens.get(bucket) or {}
                hit = float(t.get("hit", 0.0))
                miss = float(t.get("miss", 0.0))
                out = float(t.get("out", 0.0))
                inp = hit + miss
                buckets[bucket] = {
                    "hit": hit,
                    "miss": miss,
                    "out": out,
                    "cost": float(actual.get(bucket, 0.0)),
                    "requests": float(requests.get(bucket, 0.0)),
                    "hit_rate": hit / inp if inp else 0.0,
                }
                agg["hit"] += hit
                agg["miss"] += miss
                agg["out"] += out
                agg["cost"] += float(actual.get(bucket, 0.0))
                agg["requests"] += float(requests.get(bucket, 0.0))
            total_inp = agg["hit"] + agg["miss"]
            rows.append({
                "day": day,
                "cost": agg["cost"],
                "requests": agg["requests"],
                "hit": agg["hit"],
                "miss": agg["miss"],
                "out": agg["out"],
                "total": agg["hit"] + agg["miss"] + agg["out"],
                "hit_rate": agg["hit"] / total_inp if total_inp else 0.0,
                "buckets": buckets,
            })
    return sorted(rows, key=lambda r: r["day"])


# ---------------------------------------------------------------------------
# OpenCode Go meter refresh (opencode.ai/docs/go/)
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "usage_cost/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _row_cells(tr):
    cells = re.findall(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", tr, flags=re.S | re.I)
    return [_clean(re.sub(r"<[^>]+>", "", c)) for c in cells]


def _first_number(s):
    m = re.search(r"[\d][\d.,]*", s)
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


# header text -> field name (columns: Model | Input | Output | Cached Read |
# Cached Write | Usage)
_HEADER_ALIASES = {
    "input": "miss",
    "output": "out",
    "cached read": "hit",
    "cached write": "cached_write",
    "usage": "usage",
}


def parse_opencode_prices(html_text):
    """Parse DeepSeek rows from the opencode.ai/docs/go/ pricing table.

    Returns {"flash": {hit,miss,out,cached_write,usage}, "ds pro": {...}} or {}.
    """
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, flags=re.S | re.I)
    header_idx, colmap = None, {}
    for i, tr in enumerate(rows):
        cells = [_clean(c) for c in _row_cells(tr)]
        joined = " ".join(cells).lower()
        if "cached read" in joined and "cached write" in joined and "output" in joined:
            header_idx = i
            for ci, c in enumerate(cells):
                key = c.lower().strip()
                if key in _HEADER_ALIASES and _HEADER_ALIASES[key] not in colmap:
                    colmap[_HEADER_ALIASES[key]] = ci
            break
    if header_idx is None or not colmap:
        return {}

    prices = {}
    for tr in rows[header_idx + 1:]:
        cells = [_clean(c) for c in _row_cells(tr)]
        if not cells:
            continue
        model = cells[0].lower()
        if "deepseek v4 flash" in model:
            key = "flash"
        elif "deepseek v4 pro" in model:
            key = "ds pro"
        else:
            continue
        entry = {}
        for field, ci in colmap.items():
            if ci < len(cells):
                num = _first_number(cells[ci])
                if num is not None:
                    entry[field] = num
        if "hit" in entry and "miss" in entry and "out" in entry:
            prices[key] = entry
    return prices


def _is_stale(cache, max_age_seconds=OPENCODE_CACHE_MAX_AGE):
    try:
        ts = _dt.datetime.fromisoformat(cache.get("fetched_at", ""))
    except Exception:
        return True
    return (_dt.datetime.now(_dt.timezone.utc) - ts) > _dt.timedelta(seconds=max_age_seconds)


def load_opencode_prices(cache_path=OPCODE_CACHE_PATH, refresh=False, offline=False, fetch=fetch_url):
    """Return {"fetched_at", "source", "prices", "note"}.

    Priority: fresh cache -> network -> stale cache -> embedded defaults.
    Never raises; warns to stderr on fallback.
    """
    cached = None
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception as exc:
            print(f"[warn] unreadable opencode price cache {cache_path}: {exc}", file=sys.stderr)
            cached = None

    if (cached and isinstance(cached, dict) and cached.get("prices")
            and not refresh and not offline and not _is_stale(cached)):
        return dict(cached, note="cache (fresh)")

    if not offline:
        try:
            text = fetch(OPENCODE_URL)
            parsed = parse_opencode_prices(text)
            if parsed:
                entry = {
                    "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                    "source": OPENCODE_URL,
                    "prices": parsed,
                }
                if cache_path:
                    try:
                        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump(entry, f, indent=2)
                    except Exception as exc:
                        print(f"[warn] could not write opencode price cache: {exc}", file=sys.stderr)
                return dict(entry, note="fetched")
            print("[warn] opencode.ai/docs/go/ fetched but no DeepSeek rows parsed; using cache/defaults",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[warn] opencode.ai/docs/go/ fetch failed ({exc}); using cache/defaults", file=sys.stderr)

    if cached and isinstance(cached, dict) and cached.get("prices"):
        return dict(cached, note="cache (stale)" if not offline else "cache (offline)")
    return {
        "fetched_at": None,
        "source": OPENCODE_URL,
        "prices": copy.deepcopy(DEFAULT_OPENCODE),
        "note": "embedded defaults",
    }


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def _fmt(v):
    return f"{v:,.2f}"


def render_console(res, args, opencode):
    lines = []
    lines.append("=" * 72)
    lines.append("  LLM USAGE COST ESTIMATOR — multi-provider cost projection")
    lines.append("=" * 72)
    lines.append("Source data (auto-discovered):")
    for zp in res["zips"]:
        lines.append(f"  - {zp}")
    all_days = [day for m in res["months"].values() for day in m["days"]]
    if all_days:
        lines.append(f"Data range: {min(all_days)} .. {max(all_days)}")
    lines.append(f"Peak fraction: {args.peak_fraction:.2f} | Cache billing: {args.cache_billing} "
                 f"| OpenCode prices: {opencode.get('note', '')}")

    lines.append("")
    lines.append("-" * 72)
    lines.append(" ACTUAL DeepSeek cost (baseline from cost-*.csv, USD)")
    lines.append("-" * 72)
    for model in sorted(res["actual_by_model"]):
        lines.append(f"  {model:<22} : ${res['actual_by_model'][model]:,.2f}")
    lines.append(f"  {'TOTAL':<22} : ${res['grand_actual']['total']:,.2f}")

    lines.append("")
    lines.append("-" * 72)
    lines.append(" ESTIMATED COST BY PROVIDER (USD) — per bucket x per month")
    lines.append("-" * 72)
    months = list(res["month_actual"].keys())
    header = ["Provider", "Bucket"] + list(months) + ["Total"]
    str_rows = [header]
    for key, display in PROVIDER_SCENARIOS:
        for b in BUCKETS:
            row = [display, b]
            for m in months:
                row.append(_fmt(res["month_provider"][key][m][b]))
            row.append(_fmt(res["grand_provider"][key][b]))
            str_rows.append(row)
        trow = [display, "TOTAL"]
        for m in months:
            trow.append(_fmt(res["month_provider"][key][m]["total"]))
        trow.append(_fmt(res["grand_provider"][key]["total"]))
        str_rows.append(trow)
    widths = [max(len(str_rows[r][c]) for r in range(len(str_rows))) for c in range(len(header))]

    def fmt_row(row):
        return "  " + "  ".join(
            str(row[c]).ljust(widths[c]) if c < 2 else str(row[c]).rjust(widths[c])
            for c in range(len(row))
        )

    lines.append(fmt_row(header))
    lines.append("  " + "  ".join("-" * w for w in widths))
    for row in str_rows[1:]:
        lines.append(fmt_row(row))

    lines.append("")
    lines.append("-" * 72)
    lines.append(" DEEPSEEK REPROJECTION (actual -> new DeepSeek prices, USD)")
    lines.append("-" * 72)
    actual_total = res["grand_actual"]["total"]

    def mult(v):
        return f"x{v / actual_total:.2f}" if actual_total else "-"

    off = res["grand_provider"]["deepseek_new_offpeak"]
    peak = res["grand_provider"]["deepseek_new_peak"]
    blended = res["grand_provider"]["blended"]
    rep_rows = [
        ("Actual (old pricing)", res["grand_actual"]["flash"], res["grand_actual"]["ds pro"],
         actual_total, "1.00x"),
        ("New, all off-peak", off["flash"], off["ds pro"], off["total"], mult(off["total"])),
        ("New, all peak", peak["flash"], peak["ds pro"], peak["total"], mult(peak["total"])),
        (f"New, blended f={args.peak_fraction:.0%}", blended["flash"], blended["ds pro"],
         blended["total"], mult(blended["total"])),
    ]
    rep_header = ["Scenario", "flash", "ds pro", "Total", "vs actual"]
    rep_str = [(r[0], _fmt(r[1]), _fmt(r[2]), _fmt(r[3]), r[4]) for r in rep_rows]
    rw = [max(len(rep_str[r][c]) for r in range(len(rep_str))) for c in range(4)]
    rw[0] = max(rw[0], len(rep_header[0]))
    lines.append("  " + rep_header[0].ljust(rw[0]) + "  "
                 + "  ".join(h.rjust(w) for h, w in zip(rep_header[1:4], rw[1:]))
                 + "  " + rep_header[4])
    lines.append("  " + "-" * (sum(rw) + 3 * len(rw) + 4))
    for r in rep_str:
        lines.append("  " + r[0].ljust(rw[0]) + "  "
                     + "  ".join(v.rjust(w) for v, w in zip(r[1:4], rw[1:]))
                     + "  " + r[4])

    lines.append("")
    lines.append("-" * 72)
    lines.append(" OPENCODE GO NOTE")
    lines.append("-" * 72)
    oc = opencode.get("prices", {}) or {}
    cap_f = oc.get("flash", {}).get("usage_cap")
    cap_p = oc.get("ds pro", {}).get("usage_cap")
    lines.append("  Subscription: $10/mo. Meter usage caps: "
                 f"flash ${cap_f:,.0f}/mo, ds pro ${cap_p:,.0f}/mo.")
    lines.append("  Meter rates (per 1M tokens):")
    for b in BUCKETS:
        p = oc.get(b, {})
        lines.append(f"    {b:<7}: hit ${p.get('hit', 0):.4f}, miss ${p.get('miss', 0):.3f}, "
                     f"out ${p.get('out', 0):.2f}")
    lines.append(f"  Source: {opencode.get('source', OPENCODE_URL)} "
                 f"({opencode.get('note', '')})")

    lines.append("")
    lines.append("-" * 72)
    lines.append(" UNKNOWN MODELS (excluded from per-provider math)")
    lines.append("-" * 72)
    unk = res["unknown_models"]
    if not unk:
        lines.append("  (none)")
    else:
        for model, info in sorted(unk.items()):
            tok = ", ".join(f"{t}={v:,.0f}" for t, v in sorted(info["tokens"].items()))
            lines.append(f"  {model}: cost=${info['cost']:,.2f}; tokens [{tok}]")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Charts (matplotlib PNG or pure-stdlib SVG)
# ---------------------------------------------------------------------------

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _write_bar_chart_svg(path, labels, values, title):
    n = len(labels)
    W, H = 1000, 430
    pad_l, pad_b, pad_t, pad_r = 70, 90, 40, 20
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    vmax = max(values) if values else 1.0
    if vmax <= 0:
        vmax = 1.0
    gap, bar_w = plot_w / n, plot_w / n * 0.55
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="Arial, Helvetica, sans-serif">']
    parts.append(f'<text x="{W / 2:.0f}" y="26" text-anchor="middle" font-size="17" '
                 f'font-weight="bold">{_esc(title)}</text>')
    for gi in range(5):
        yv = vmax * gi / 4
        y = pad_t + plot_h - plot_h * gi / 4
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="#e5e5e5"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                     f'fill="#666">{yv:,.2f}</text>')
    for i, (lab, val) in enumerate(zip(labels, values)):
        x0 = pad_l + i * gap + (gap - bar_w) / 2
        h = plot_h * val / vmax
        y0 = pad_t + plot_h - h
        parts.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                     f'fill="#4C72B0" stroke="#2a4a7a"/>')
        parts.append(f'<text x="{x0 + bar_w / 2:.1f}" y="{y0 - 6:.1f}" text-anchor="middle" '
                     f'font-size="10" fill="#333">{val:,.2f}</text>')
        parts.append(f'<text x="{x0 + bar_w / 2:.1f}" y="{pad_t + plot_h + 16:.1f}" '
                     f'text-anchor="middle" font-size="10" fill="#444">{_esc(lab)}</text>')
    parts.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _write_stacked_chart_svg(path, day_labels, series, title, series_names):
    n = len(day_labels)
    W, H = 1200, 440
    pad_l, pad_b, pad_t, pad_r = 70, 90, 40, 16
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    totals = [sum(s[i] for s in series) for i in range(n)]
    vmax = max(totals) if totals else 1.0
    if vmax <= 0:
        vmax = 1.0
    gap, bar_w = plot_w / n, max(1.0, plot_w / n * 0.7)
    colors = ["#4C72B0", "#DD8452"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="Arial, Helvetica, sans-serif">']
    parts.append(f'<text x="{W / 2:.0f}" y="26" text-anchor="middle" font-size="17" '
                 f'font-weight="bold">{_esc(title)}</text>')
    for gi in range(5):
        yv = vmax * gi / 4
        y = pad_t + plot_h - plot_h * gi / 4
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" stroke="#e5e5e5"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                     f'fill="#666">{yv:,.2f}</text>')
    for i in range(n):
        x0 = pad_l + i * gap + (gap - bar_w) / 2
        y = pad_t + plot_h
        for s in range(len(series)):
            h = plot_h * series[s][i] / vmax
            y -= h
            parts.append(f'<rect x="{x0:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                         f'fill="{colors[s % len(colors)]}"/>')
    step = max(1, n // 15)
    for i in range(0, n, step):
        cx = pad_l + i * gap + gap / 2
        parts.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 16:.1f}" text-anchor="middle" '
                     f'font-size="9" fill="#444">{_esc(day_labels[i])}</text>')
    lx = pad_l
    for s, name in enumerate(series_names):
        parts.append(f'<rect x="{lx}" y="{H - 26}" width="12" height="12" '
                     f'fill="{colors[s % len(colors)]}"/>')
        parts.append(f'<text x="{lx + 18}" y="{H - 16}" font-size="11" fill="#333">{_esc(name)}</text>')
        lx += 18 + 12 * len(name) + 24
    parts.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _render_png(path1, path2, labels, values, days, flash_vals, pro_vals):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title("Estimated total cost by provider (USD)")
    ax.set_ylabel("USD")
    for i, v in enumerate(values):
        ax.text(i, v, f"{v:,.2f}", ha="center", va="bottom", fontsize=8)
    plt.setp(ax.get_xticklabels(), rotation=25, ha="right", fontsize=9)
    fig.tight_layout()
    fig.savefig(path1, dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.bar(days, flash_vals, label="flash", color="#4C72B0")
    ax.bar(days, pro_vals, bottom=flash_vals, label="ds pro", color="#DD8452")
    ax.set_title("Estimated cost per day by bucket — DeepSeek blended (USD)")
    ax.set_ylabel("USD")
    ax.legend()
    step = max(1, len(days) // 15)
    ticks = range(0, len(days), step)
    ax.set_xticks(list(ticks))
    ax.set_xticklabels([days[i] for i in ticks], rotation=45, ha="right", fontsize=7)
    fig.tight_layout()
    fig.savefig(path2, dpi=150)
    plt.close(fig)


def _matplotlib_available():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def render_charts(res, args):
    """Write both charts to out_dir. Returns list of written paths."""
    os.makedirs(args.out_dir, exist_ok=True)
    labels = [_SHORT_NAME[key] for key, _ in PROVIDER_SCENARIOS]
    values = [res["grand_provider"][key]["total"] for key, _ in PROVIDER_SCENARIOS]
    days, flash_vals, pro_vals = [], [], []
    for month in sorted(res["estimates"]["blended"]):
        for day in sorted(res["estimates"]["blended"][month]):
            days.append(day)
            est = res["estimates"]["blended"][month][day]
            flash_vals.append(est["flash"])
            pro_vals.append(est["ds pro"])
    use_png = _matplotlib_available()
    ext = "png" if use_png else "svg"
    p1 = os.path.join(args.out_dir, f"cost_by_provider.{ext}")
    p2 = os.path.join(args.out_dir, f"per_bucket_stacked.{ext}")
    if use_png:
        _render_png(p1, p2, labels, values, days, flash_vals, pro_vals)
    else:
        _write_bar_chart_svg(p1, labels, values, "Estimated total cost by provider (USD)")
        _write_stacked_chart_svg(p2, days, [flash_vals, pro_vals],
                                 "Estimated cost per day by bucket — DeepSeek blended (USD)",
                                 ["flash", "ds pro"])
    return [p1, p2]


# ---------------------------------------------------------------------------
# JSON export + CLI
# ---------------------------------------------------------------------------

def export_json(res, args, opencode, path):
    payload = {
        "meta": {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "peak_fraction": args.peak_fraction,
            "cache_billing": args.cache_billing,
            "zips": res["zips"],
            "opencode": opencode,
        },
        "actual": {
            "month": res["month_actual"],
            "grand": res["grand_actual"],
            "by_model": res["actual_by_model"],
        },
        "estimated": {
            "month": {key: res["month_provider"][key] for key, _ in PROVIDER_SCENARIOS},
            "grand": {key: res["grand_provider"][key] for key, _ in PROVIDER_SCENARIOS},
        },
        "per_day": res["estimates"],
        "unknown_models": res["unknown_models"],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="usage_cost",
        description="LLM usage data -> multi-provider cost estimator + charts.",
    )
    p.add_argument("--data-dir", default=os.path.join(PROJECT_ROOT, "_data", "usage"),
                   help=f"directory containing usage_data_*.zip (default: {os.path.join(PROJECT_ROOT, '_data', 'usage')})")
    p.add_argument("--peak-fraction", type=float, default=0.0,
                   help="fraction of usage billed at peak DeepSeek prices (default: 0.0 = all off-peak)")
    p.add_argument("--cache-billing", choices=("write", "input"), default="write",
                   help="how OpenAI/Claude bill cache misses: 'write' (default) or 'input'")
    p.add_argument("--out-dir", default=os.path.join(PROJECT_ROOT, ".dev", "out"),
                   help=f"directory for charts (default: {os.path.join(PROJECT_ROOT, '.dev', 'out')})")
    p.add_argument("--json", dest="json_path", metavar="PATH", default=None,
                   help="optional full-results JSON export path")
    p.add_argument("--refresh-prices", action="store_true",
                   help="force re-fetch of OpenCode Go meter prices")
    p.add_argument("--offline", action="store_true",
                   help="skip network; use OpenCode price cache if present, else defaults")
    p.add_argument("--no-charts", action="store_true", help="skip chart generation")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    zips = discover_zips(args.data_dir)
    if not zips:
        print(f"[error] no usage_data_*.zip found in {args.data_dir}", file=sys.stderr)
        return 1
    usage = load_usage(zips)
    for model in usage["unknown_models"]:
        print(f"[warn] unknown model '{model}' excluded from per-provider math", file=sys.stderr)
    opencode = load_opencode_prices(OPCODE_CACHE_PATH,
                                    refresh=args.refresh_prices, offline=args.offline)
    res = build_results(usage, args.peak_fraction, args.cache_billing, opencode["prices"])
    render_console(res, args, opencode)

    if not args.no_charts:
        for cf in render_charts(res, args):
            print(f"Chart written: {cf}")
    if args.json_path:
        export_json(res, args, opencode, args.json_path)
        print(f"JSON export: {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
