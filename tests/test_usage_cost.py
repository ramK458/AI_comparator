"""Tests for .dev/usage_monitor/usage_cost.py — bucketing, cost math, price-table parsing.

DeepSeek-sheet-format focused: load_usage / load_usage_from_upload use the real
DeepSeek CSV headers (amount: user_id,start_time_iso,end_time_iso,model,
api_key_name,api_key,type,price,amount; cost: user_id,start_time_iso,
end_time_iso,model,wallet_type,cost,currency).
"""
import os
import pathlib
import sys
import zipfile

import pytest

# .dev/usage_monitor (parent of this tests/ dir) must be importable so
# `import usage_cost` resolves regardless of the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage_cost as uc  # noqa: E402

# AI_assistant repo root is parents[3] from .dev/usage_monitor/tests/.
USAGE_DATA_DIR = pathlib.Path(__file__).resolve().parents[3] / "_data" / "usage"

AMOUNT_HEADER = "user_id,start_time_iso,end_time_iso,model,api_key_name,api_key,type,price,amount"
COST_HEADER = "user_id,start_time_iso,end_time_iso,model,wallet_type,cost,currency"
UTC_AMOUNT_HEADER = "user_id,utc_date,model,api_key_name,api_key,type,price,amount"
UTC_COST_HEADER = "user_id,utc_date,model,wallet_type,cost,currency"


def _make_zip(tmp_path, name, cost_rows=(), amount_rows=()):
    p = os.path.join(str(tmp_path), name)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("cost-" + name + ".csv",
                   COST_HEADER + "\n" + "\n".join(cost_rows))
        z.writestr("amount-" + name + ".csv",
                   AMOUNT_HEADER + "\n" + "\n".join(amount_rows))
    return p


# ---------------------------------------------------------------------------
# discover_zips / load_usage
# ---------------------------------------------------------------------------

def test_discover_zips_matches_only_zips(tmp_path):
    d = str(tmp_path)
    for n in ("usage_data_2026-06-01_2026-06-30.zip", "usage_data_2026-07-01_2026-07-31.zip"):
        open(os.path.join(d, n), "w").close()
    open(os.path.join(d, "not-a-usage-file.zip"), "w").close()
    open(os.path.join(d, "notes.txt"), "w").close()
    os.makedirs(os.path.join(d, "usage_data_2026-06-01_2026-06-30"), exist_ok=True)
    got = uc.discover_zips(d)
    assert len(got) == 2
    assert all(p.endswith(".zip") for p in got)


def test_load_usage_buckets_and_actual(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        cost_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,Paid,10.0,USD",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,Paid,4.0,USD",
            "u,2026-06-02T00:00:00+02:00,2026-06-03T00:00:00+02:00,deepseek-v4-flash,Paid,1.5,USD",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,some-other-model,Paid,99.0,USD",
        ],
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,1000000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_miss_tokens,0.00000014,100000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "output_tokens,0.00000028,50000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "request_count,0.0,65",
            "u,2026-06-02T00:00:00+02:00,2026-06-03T00:00:00+02:00,deepseek-v4-pro,k,n,"
            "input_cache_hit_tokens,0.000000003625,2000000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,mystery-model,k,n,"
            "output_tokens,0.0000001,777",
        ],
    )
    usage = uc.load_usage([zp])

    assert set(usage["months"]) == {"2026-06"}
    m = usage["months"]["2026-06"]
    assert set(m["days"]) == {"2026-06-01", "2026-06-02"}

    d1 = m["days"]["2026-06-01"]
    # explicit allowlist bucketing
    assert d1["tokens"]["flash"]["hit"] == pytest.approx(1_000_000)
    assert d1["tokens"]["flash"]["miss"] == pytest.approx(100_000)
    assert d1["tokens"]["flash"]["out"] == pytest.approx(50_000)
    # request_count ignored
    assert set(d1["tokens"]["flash"]) == {"hit", "miss", "out"}
    # actual costs from cost-*.csv
    assert d1["actual"]["flash"] == pytest.approx(10.0)
    assert d1["actual"]["ds pro"] == pytest.approx(4.0)
    assert m["days"]["2026-06-02"]["tokens"]["ds pro"]["hit"] == pytest.approx(2_000_000)
    assert m["actual_by_model"]["deepseek-v4-flash"] == pytest.approx(11.5)

    # unknown models excluded from per-provider math, reported separately
    assert "mystery-model" not in m["days"]["2026-06-01"]["tokens"]
    unk = usage["unknown_models"]
    assert unk["some-other-model"]["cost"] == pytest.approx(99.0)
    assert unk["mystery-model"]["tokens"]["output_tokens"] == pytest.approx(777)


# ---------------------------------------------------------------------------
# load_usage_from_upload (DeepSeek sheet format: zip bytes or single CSV)
# ---------------------------------------------------------------------------

def test_upload_zip_bytes_equals_load_usage(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        cost_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,Paid,10.0,USD",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,Paid,4.0,USD",
        ],
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,1000000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,k,n,"
            "output_tokens,0.000000001,500000",
        ],
    )
    with open(zp, "rb") as f:
        data = f.read()
    up = uc.load_usage_from_upload(os.path.basename(zp), data)
    lu = uc.load_usage([zp])
    # months + unknown_models must be identical (the `zips` label differs by design)
    assert up["months"] == lu["months"]
    assert up["unknown_models"] == lu["unknown_models"]


def test_upload_amount_csv_tokens_requests_buckets():
    csv_text = (
        AMOUNT_HEADER + "\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
        "input_cache_hit_tokens,0.0000000028,1000000\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
        "input_cache_miss_tokens,0.00000014,100000\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
        "request_count,0.0,12\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,k,n,"
        "output_tokens,0.000000001,500000\n"
    ).encode("utf-8")
    usage = uc.load_usage_from_upload("amount-2026-06.csv", csv_text)
    d = usage["months"]["2026-06"]["days"]["2026-06-01"]
    assert d["tokens"]["flash"]["hit"] == pytest.approx(1_000_000)
    assert d["tokens"]["flash"]["miss"] == pytest.approx(100_000)
    assert d["tokens"]["flash"]["out"] == pytest.approx(0.0)
    assert set(d["tokens"]["flash"]) == {"hit", "miss", "out"}
    assert d["requests"]["flash"] == pytest.approx(12.0)
    assert d["tokens"]["ds pro"]["out"] == pytest.approx(500_000)


def test_upload_cost_csv_actual_cost():
    csv_text = (
        COST_HEADER + "\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,Paid,10.0,USD\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,Paid,4.0,USD\n"
        "u,2026-06-02T00:00:00+02:00,2026-06-03T00:00:00+02:00,deepseek-v4-flash,Paid,1.5,USD\n"
    ).encode("utf-8")
    usage = uc.load_usage_from_upload("cost-2026-06.csv", csv_text)
    m = usage["months"]["2026-06"]
    assert m["days"]["2026-06-01"]["actual"]["flash"] == pytest.approx(10.0)
    assert m["days"]["2026-06-01"]["actual"]["ds pro"] == pytest.approx(4.0)
    assert m["days"]["2026-06-02"]["actual"]["flash"] == pytest.approx(1.5)
    assert m["actual_by_model"]["deepseek-v4-flash"] == pytest.approx(11.5)


def test_upload_unknown_model_in_deepseek_sheet():
    csv_text = (
        AMOUNT_HEADER + "\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
        "input_cache_hit_tokens,0.0000000028,1000000\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,mystery-model,k,n,"
        "output_tokens,0.0000001,777\n"
    ).encode("utf-8")
    usage = uc.load_usage_from_upload("amount-2026-06.csv", csv_text)
    unk = usage["unknown_models"]
    assert "mystery-model" in unk
    assert unk["mystery-model"]["tokens"]["output_tokens"] == pytest.approx(777)
    # unknown model is NOT bucketed into per-provider math
    assert "mystery-model" not in usage["months"]["2026-06"]["days"]["2026-06-01"]["tokens"]


# ---------------------------------------------------------------------------
# members key + empty-daily diagnostic (v4)
# ---------------------------------------------------------------------------

def test_members_key_present_for_zip(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        cost_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,Paid,10.0,USD",
        ],
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,1000000",
        ],
    )
    usage = uc.load_usage([zp])
    assert set(usage["members"]) == {
        "cost-usage_data_2026-06-01_2026-06-30.zip.csv",
        "amount-usage_data_2026-06-01_2026-06-30.zip.csv",
    }


def test_members_key_present_for_single_csv_upload():
    csv_text = (
        AMOUNT_HEADER + "\n"
        "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
        "input_cache_hit_tokens,0.0000000028,1000000\n"
    ).encode("utf-8")
    usage = uc.load_usage_from_upload("amount-2026-06.csv", csv_text)
    assert usage["members"] == ["amount-2026-06.csv"]


def test_unknown_model_zip_yields_empty_daily_and_members(tmp_path):
    # A zip whose only model is NOT in the allowlist -> no daily rows, but the
    # unknown model + zip members are surfaced for the diagnostic.
    p = os.path.join(str(tmp_path), "usage_data_2026-06-01_2026-06-30.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("cost-usage.csv", COST_HEADER + "\n"
                   "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-chat,Paid,10.0,USD\n")
        z.writestr("amount-usage.csv", AMOUNT_HEADER + "\n"
                   "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-chat,k,n,"
                   "input_cache_hit_tokens,0.0000000028,1000000\n")
    usage = uc.load_usage([p])
    assert uc.daily_summary(usage) == []
    assert "deepseek-chat" in usage["unknown_models"]
    assert usage["unknown_models"]["deepseek-chat"]["cost"] == pytest.approx(10.0)
    assert set(usage["members"]) == {"cost-usage.csv", "amount-usage.csv"}


def test_wrong_member_names_zip_shows_actual_members(tmp_path):
    # A zip whose members are NOT named cost-*.csv / amount-*.csv -> nothing is
    # loaded, but members surfaces the real names for the diagnostic.
    p = os.path.join(str(tmp_path), "usage_data_2026-06-01_2026-06-30.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("usage.csv", "name,value\nignore,1\n")
    usage = uc.load_usage([p])
    assert uc.daily_summary(usage) == []
    assert usage["unknown_models"] == {}
    assert usage["members"] == ["usage.csv"]


def test_alternate_csv_member_names_are_classified_by_headers(tmp_path):
    p = os.path.join(str(tmp_path), "usage_data_2026-06-01_2026-06-30.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("export_amounts.csv", AMOUNT_HEADER + "\n"
                   "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,"
                   "deepseek-v4-flash,k,n,input_cache_hit_tokens,0.0,1000000\n")
        z.writestr("export-costs.csv", COST_HEADER + "\n"
                   "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,"
                   "deepseek-v4-flash,Paid,10.0,USD\n")
        z.writestr("notes.csv", "name,value\nignore,1\n")

    usage = uc.load_usage([p])
    day = usage["months"]["2026-06"]["days"]["2026-06-01"]
    assert day["tokens"]["flash"]["hit"] == pytest.approx(1_000_000)
    assert day["actual"]["flash"] == pytest.approx(10.0)
    assert usage["members"] == ["export_amounts.csv", "export-costs.csv", "notes.csv"]


def test_utc_date_zip_buckets_compact_dates(tmp_path):
    p = os.path.join(str(tmp_path), "usage_data_2026-06-01_2026-06-30.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("export.csv", UTC_AMOUNT_HEADER + "\n"
                   "u,20260629,deepseek-v4-flash,k,n,input_cache_hit_tokens,0.0,1000000\n"
                   "u,20260701,deepseek-v4-pro,k,n,output_tokens,0.0,500000\n")
        z.writestr("billing.csv", UTC_COST_HEADER + "\n"
                   "u,20260629,deepseek-v4-flash,Paid,10.0,USD\n"
                   "u,20260701,deepseek-v4-pro,Paid,4.0,USD\n")

    usage = uc.load_usage([p])
    assert set(usage["months"]) == {"2026-06", "2026-07"}
    assert usage["months"]["2026-06"]["days"]["2026-06-29"]["tokens"]["flash"]["hit"] == pytest.approx(1_000_000)
    assert usage["months"]["2026-06"]["days"]["2026-06-29"]["actual"]["flash"] == pytest.approx(10.0)
    assert usage["months"]["2026-07"]["days"]["2026-07-01"]["tokens"]["ds pro"]["out"] == pytest.approx(500_000)
    assert usage["months"]["2026-07"]["days"]["2026-07-01"]["actual"]["ds pro"] == pytest.approx(4.0)


def test_utc_date_single_csv_upload():
    csv_text = (UTC_AMOUNT_HEADER + "\n"
                "u,20260630,deepseek-v4-flash,k,n,request_count,0.0,12\n").encode("utf-8")
    usage = uc.load_usage_from_upload("export.csv", csv_text)
    day = usage["months"]["2026-06"]["days"]["2026-06-30"]
    assert day["requests"]["flash"] == pytest.approx(12.0)


def test_unrelated_csv_with_utc_date_is_ignored(tmp_path):
    p = os.path.join(str(tmp_path), "usage_data_2026-06-01_2026-06-30.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("notes.csv", "user_id,utc_date,model,value\n"
                   "u,20260629,deepseek-v4-flash,1\n")
    usage = uc.load_usage([p])
    assert usage["months"] == {}
    assert usage["unknown_models"] == {}


# ---------------------------------------------------------------------------
# cost math
# ---------------------------------------------------------------------------

def test_estimate_day_cost_formula():
    tokens = {
        "flash": {"hit": 1_000_000, "miss": 100_000, "out": 50_000},
        "ds pro": {"hit": 2_000_000, "miss": 0, "out": 0},
    }
    price = {
        "flash": {"hit": 0.007, "miss": 0.22, "out": 0.66},
        "ds pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
    }
    est = uc.estimate_day(tokens, price)
    # (1e6*0.007 + 1e5*0.22 + 5e4*0.66)/1e6 = (7000+22000+33000)/1e6 = 0.062
    assert est["flash"] == pytest.approx(0.062, abs=1e-9)
    # (2e6*0.022)/1e6 = 0.044
    assert est["ds pro"] == pytest.approx(0.044, abs=1e-9)


def test_estimate_day_missing_bucket_safe():
    est = uc.estimate_day({"flash": {"hit": 1_000_000, "miss": 0, "out": 0}},
                          {"flash": {"hit": 0.007, "miss": 0.22, "out": 0.66}})
    assert est["flash"] == pytest.approx(0.007, abs=1e-9)
    assert est["ds pro"] == pytest.approx(0.0, abs=1e-9)


def test_cache_billing_write_vs_input():
    tokens = {"flash": {"hit": 0, "miss": 1_000_000, "out": 0}, "ds pro": {"hit": 0, "miss": 0, "out": 0}}
    write = uc.resolve_provider_prices("openai", 0.0, "write", None)
    inp = uc.resolve_provider_prices("openai", 0.0, "input", None)
    assert uc.estimate_day(tokens, write)["flash"] == pytest.approx(0.25, abs=1e-9)
    assert uc.estimate_day(tokens, inp)["flash"] == pytest.approx(0.20, abs=1e-9)

    cw = uc.resolve_provider_prices("claude", 0.0, "write", None)
    ci = uc.resolve_provider_prices("claude", 0.0, "input", None)
    assert uc.estimate_day(tokens, cw)["flash"] == pytest.approx(1.25, abs=1e-9)
    assert uc.estimate_day(tokens, ci)["flash"] == pytest.approx(1.00, abs=1e-9)


def test_blended_prices_midpoint():
    off = uc.PRICING["deepseek_new_offpeak"]["flash"]
    peak = uc.PRICING["deepseek_new_peak"]["flash"]
    blend = uc.resolve_provider_prices("blended", 0.5, "write", None)["flash"]
    assert blend["hit"] == pytest.approx((off["hit"] + peak["hit"]) / 2)
    assert blend["miss"] == pytest.approx((off["miss"] + peak["miss"]) / 2)
    assert blend["out"] == pytest.approx((off["out"] + peak["out"]) / 2)
    # fraction 0 == off-peak exactly
    assert uc.resolve_provider_prices("blended", 0.0, "write", None)["flash"] == \
        uc.PRICING["deepseek_new_offpeak"]["flash"]
    # fraction 1 == peak exactly
    assert uc.resolve_provider_prices("blended", 1.0, "write", None)["flash"] == \
        uc.PRICING["deepseek_new_peak"]["flash"]


def test_opencode_go_uses_provided_prices():
    tokens = {"flash": {"hit": 1_000_000, "miss": 0, "out": 0}, "ds pro": {"hit": 0, "miss": 0, "out": 0}}
    prices = uc.DEFAULT_OPENCODE
    est = uc.estimate_day(tokens, uc.resolve_provider_prices("opencode_go", 0.0, "write", prices))
    assert est["flash"] == pytest.approx(0.0028, abs=1e-9)


# ---------------------------------------------------------------------------
# opencode price-table parse
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<table>
<thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th>
<th>Cached Write</th><th>Usage</th></tr></thead>
<tbody>
<tr><td><a href="/models/deepseek">DeepSeek V4 Flash</a></td><td>$0.14/M</td>
<td>$0.28/M</td><td>$0.0028/M</td><td>$0.14/M</td><td>$60/mo</td></tr>
<tr><td>DeepSeek V4 Pro</td><td>$0.435/M</td><td>$0.87/M</td><td>$0.003625/M</td>
<td>$0.435/M</td><td>$15/mo</td></tr>
<tr><td>Some Other Model</td><td>$1</td><td>$2</td><td>$0.1</td><td>$1</td><td>-</td></tr>
</tbody>
</table>
"""


def test_parse_opencode_prices():
    prices = uc.parse_opencode_prices(SAMPLE_HTML)
    assert "flash" in prices and "ds pro" in prices
    f = prices["flash"]
    assert f["hit"] == pytest.approx(0.0028)
    assert f["miss"] == pytest.approx(0.14)
    assert f["out"] == pytest.approx(0.28)
    assert f["usage"] == pytest.approx(60.0)
    p = prices["ds pro"]
    assert p["hit"] == pytest.approx(0.003625)
    assert p["miss"] == pytest.approx(0.435)
    assert p["out"] == pytest.approx(0.87)
    assert p["usage"] == pytest.approx(15.0)
    assert len(prices) == 2  # unrelated rows ignored


def test_parse_opencode_prices_no_table():
    assert uc.parse_opencode_prices("<html><body>no table here</body></html>") == {}


# ---------------------------------------------------------------------------
# end-to-end against the real usage data (skipped if absent)
# ---------------------------------------------------------------------------

def test_reprojection_matches_research():
    zips = uc.discover_zips(str(USAGE_DATA_DIR))
    if not zips:
        pytest.skip("real usage zips not present")
    usage = uc.load_usage(zips)
    res = uc.build_results(usage, 0.0, "write", uc.DEFAULT_OPENCODE)
    # actual DeepSeek baseline across Jun+Jul
    assert res["grand_actual"]["total"] == pytest.approx(68.53, abs=0.1)
    assert res["grand_actual"]["flash"] == pytest.approx(51.5969, abs=0.1)
    assert res["grand_actual"]["ds pro"] == pytest.approx(16.9353, abs=0.1)
    # reprojection bounds from research
    assert res["grand_provider"]["deepseek_new_offpeak"]["total"] == pytest.approx(140.15, abs=1.0)
    assert res["grand_provider"]["deepseek_new_peak"]["total"] == pytest.approx(280.30, abs=1.0)


# ---------------------------------------------------------------------------
# total_tokens / cost_at_hitrate (UI helpers)
# ---------------------------------------------------------------------------

def _usage_from_days(days_tokens):
    """Build a minimal usage dict shaped like load_usage() output.

    days_tokens: {month: {day: {bucket: {hit, miss, out}}}}.
    """
    months = {}
    for month, days in days_tokens.items():
        m = {"days": {}, "actual_by_model": {}}
        for day, buckets in days.items():
            d = {"tokens": {b: {"hit": 0.0, "miss": 0.0, "out": 0.0} for b in uc.BUCKETS},
                 "actual": {b: 0.0 for b in uc.BUCKETS}}
            for bucket, fields in buckets.items():
                d["tokens"][bucket].update(fields)
            m["days"][day] = d
        months[month] = m
    return {"months": months, "unknown_models": {}, "zips": []}


def test_total_tokens_sums_across_months():
    usage = _usage_from_days({
        "2026-06": {
            "2026-06-01": {"flash": {"hit": 100.0, "miss": 10.0, "out": 5.0}},
            "2026-06-02": {"flash": {"hit": 200.0, "miss": 20.0, "out": 10.0}},
        },
        "2026-07": {
            "2026-07-01": {"ds pro": {"hit": 1000.0, "miss": 100.0, "out": 50.0}},
        },
    })
    t = uc.total_tokens(usage)
    assert t["flash"] == {"hit": 300.0, "miss": 30.0, "out": 15.0}
    assert t["ds pro"] == {"hit": 1000.0, "miss": 100.0, "out": 50.0}


def test_total_tokens_from_loaded_usage(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,1000000",
            "u,2026-06-02T00:00:00+02:00,2026-06-03T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_miss_tokens,0.00000014,200000",
            "u,2026-06-03T00:00:00+02:00,2026-06-04T00:00:00+02:00,deepseek-v4-pro,k,n,"
            "output_tokens,0.000000001,300000",
        ],
    )
    usage = uc.load_usage([zp])
    t = uc.total_tokens(usage)
    assert t["flash"] == {"hit": 1_000_000.0, "miss": 200_000.0, "out": 0.0}
    assert t["ds pro"] == {"hit": 0.0, "miss": 0.0, "out": 300_000.0}


def test_total_tokens_zero_and_missing_buckets_safe():
    usage = _usage_from_days({
        "2026-06": {"2026-06-01": {"flash": {"hit": 0.0, "miss": 0.0, "out": 0.0}}},
    })
    t = uc.total_tokens(usage)
    assert t["flash"] == {"hit": 0.0, "miss": 0.0, "out": 0.0}
    assert t["ds pro"] == {"hit": 0.0, "miss": 0.0, "out": 0.0}
    assert uc.total_tokens(None) == {"flash": {"hit": 0.0, "miss": 0.0, "out": 0.0},
                                    "ds pro": {"hit": 0.0, "miss": 0.0, "out": 0.0}}


TOKENS_2M = {
    "flash": {"hit": 1_000_000, "miss": 1_000_000, "out": 500_000},
    "ds pro": {"hit": 0, "miss": 0, "out": 0},
}
PRICES_DS = {
    "flash": {"hit": 0.007, "miss": 0.22, "out": 0.66},
    "ds pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}


def test_cost_at_hitrate_rate_0_all_miss():
    # rate 0: all input becomes miss -> (2e6*0.22 + 5e5*0.66)/1e6 = 0.44 + 0.33
    assert uc.cost_at_hitrate(TOKENS_2M, PRICES_DS, 0.0) == pytest.approx(0.77, abs=1e-9)


def test_cost_at_hitrate_rate_1_all_hit():
    # rate 1: all input becomes hit -> (2e6*0.007 + 5e5*0.66)/1e6 = 0.014 + 0.33
    assert uc.cost_at_hitrate(TOKENS_2M, PRICES_DS, 1.0) == pytest.approx(0.344, abs=1e-9)


def test_cost_at_hitrate_midpoint():
    # rate 0.5: inp=2e6 -> new_hit=1e6, new_miss=1e6
    # (1e6*0.007 + 1e6*0.22 + 5e5*0.66)/1e6 = 0.007 + 0.22 + 0.33 = 0.557
    mid = uc.cost_at_hitrate(TOKENS_2M, PRICES_DS, 0.5)
    assert mid == pytest.approx(0.557, abs=1e-9)
    # miss prices are higher than hit prices, so cost decreases with hit rate
    assert uc.cost_at_hitrate(TOKENS_2M, PRICES_DS, 1.0) < mid < \
        uc.cost_at_hitrate(TOKENS_2M, PRICES_DS, 0.0)


def test_cost_at_hitrate_zero_tokens_safe():
    zero = {"flash": {"hit": 0, "miss": 0, "out": 0},
            "ds pro": {"hit": 0, "miss": 0, "out": 0}}
    assert uc.cost_at_hitrate(zero, PRICES_DS, 0.3) == 0.0
    assert uc.cost_at_hitrate({}, {}, 0.3) == 0.0


# ---------------------------------------------------------------------------
# request_count + daily_summary (v2 UI analytics helpers)
# ---------------------------------------------------------------------------

def test_load_usage_captures_request_count_per_day(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "request_count,0.0,10",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "request_count,0.0,5",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,1000000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-pro,k,n,"
            "request_count,0.0,3",
            "u,2026-06-02T00:00:00+02:00,2026-06-03T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "request_count,0.0,7",
        ],
    )
    usage = uc.load_usage([zp])
    d1 = usage["months"]["2026-06"]["days"]["2026-06-01"]
    # additive per day/bucket; token dicts unaffected
    assert d1["requests"]["flash"] == pytest.approx(15.0)
    assert d1["requests"]["ds pro"] == pytest.approx(3.0)
    assert set(d1["tokens"]["flash"]) == {"hit", "miss", "out"}
    d2 = usage["months"]["2026-06"]["days"]["2026-06-02"]
    assert d2["requests"]["flash"] == pytest.approx(7.0)


def test_daily_summary_sums_and_sorts():
    usage = _usage_from_days({
        "2026-07": {
            "2026-07-02": {"flash": {"hit": 100.0, "miss": 100.0, "out": 50.0},
                           "ds pro": {"hit": 0.0, "miss": 0.0, "out": 0.0}},
            "2026-07-01": {"flash": {"hit": 0.0, "miss": 0.0, "out": 0.0}},
        },
        "2026-06": {
            "2026-06-15": {"flash": {"hit": 200.0, "miss": 50.0, "out": 25.0}},
        },
    })
    # inject actual cost + request counts for one day
    d = usage["months"]["2026-07"]["days"]["2026-07-02"]
    d["actual"]["flash"] = 1.25
    d["actual"]["ds pro"] = 0.50
    d["requests"] = {"flash": 12.0, "ds pro": 3.0}

    rows = uc.daily_summary(usage)
    # sorted by day across months
    assert [r["day"] for r in rows] == ["2026-06-15", "2026-07-01", "2026-07-02"]

    r0 = rows[0]  # 2026-06-15
    assert r0["hit"] == pytest.approx(200.0)
    assert r0["miss"] == pytest.approx(50.0)
    assert r0["out"] == pytest.approx(25.0)
    assert r0["total"] == pytest.approx(275.0)
    assert r0["hit_rate"] == pytest.approx(200.0 / 250.0)
    assert r0["cost"] == pytest.approx(0.0)  # no actual injected
    assert r0["requests"] == pytest.approx(0.0)  # no requests key -> 0
    assert r0["buckets"]["flash"]["hit_rate"] == pytest.approx(200.0 / 250.0)
    assert r0["buckets"]["flash"]["requests"] == pytest.approx(0.0)

    r1 = rows[1]  # 2026-07-01: no input tokens
    assert r1["hit_rate"] == 0.0
    assert r1["buckets"]["flash"]["hit_rate"] == 0.0
    assert r1["total"] == pytest.approx(0.0)

    r2 = rows[2]  # 2026-07-02
    assert r2["cost"] == pytest.approx(1.75)
    assert r2["requests"] == pytest.approx(15.0)
    assert r2["hit_rate"] == pytest.approx(100.0 / 200.0)
    assert r2["buckets"]["flash"]["requests"] == pytest.approx(12.0)
    assert r2["buckets"]["flash"]["cost"] == pytest.approx(1.25)
    assert r2["buckets"]["ds pro"]["cost"] == pytest.approx(0.50)
    assert r2["buckets"]["ds pro"]["requests"] == pytest.approx(3.0)


def test_daily_summary_missing_keys_graceful():
    usage = {
        "months": {"2026-06": {"days": {"2026-06-01": {"tokens": {}}}}},
        "unknown_models": {},
        "zips": [],
    }
    rows = uc.daily_summary(usage)
    assert len(rows) == 1
    r = rows[0]
    assert r["day"] == "2026-06-01"
    assert r["cost"] == 0.0
    assert r["requests"] == 0.0
    assert r["total"] == 0.0
    assert r["hit_rate"] == 0.0
    assert set(r["buckets"]) == {"flash", "ds pro"}
    for b in uc.BUCKETS:
        assert r["buckets"][b]["hit_rate"] == 0.0
        assert r["buckets"][b]["requests"] == 0.0
        assert r["buckets"][b]["cost"] == 0.0
    assert uc.daily_summary(None) == []
    assert uc.daily_summary({"months": {}}) == []


def test_daily_summary_from_loaded_usage(tmp_path):
    zp = _make_zip(
        tmp_path, "usage_data_2026-06-01_2026-06-30.zip",
        cost_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,Paid,2.0,USD",
        ],
        amount_rows=[
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_hit_tokens,0.0000000028,900000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "input_cache_miss_tokens,0.00000014,100000",
            "u,2026-06-01T00:00:00+02:00,2026-06-02T00:00:00+02:00,deepseek-v4-flash,k,n,"
            "request_count,0.0,42",
        ],
    )
    usage = uc.load_usage([zp])
    rows = uc.daily_summary(usage)
    assert len(rows) == 1
    r = rows[0]
    assert r["requests"] == pytest.approx(42.0)
    assert r["buckets"]["flash"]["requests"] == pytest.approx(42.0)
    assert r["cost"] == pytest.approx(2.0)
    assert r["hit_rate"] == pytest.approx(0.9)
    assert r["total"] == pytest.approx(1_000_000.0)
