"""Siphon 单元测试: 不发真实网络请求, 用桩覆盖核心口径与调度。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SIPHON_DB"] = tempfile.mktemp(suffix=".db")

from siphon import accounts, accounting, config, optimizers, router  # noqa: E402
from siphon import db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """每个用例独立临时库, 并清空账号内存态。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db._conn = None
    db.connect()
    accounts.invalidate()
    accounts._model_down.clear()
    accounts._account_open.clear()
    accounts._sticky.clear()
    accounts._inflight.clear()
    accounts._headroom.clear()
    yield


def _mkacct(**kw) -> accounts.Account:
    return accounts.create(key=kw.pop("key", "sk-test-abcd"), **kw)


# ---------------- 记账口径 ----------------

def test_accounting_identity():
    """各节省段 + 实际 == 基线(可加性, 瀑布图的硬要求); 价格按 USD/1M。"""
    m_in, m_out = config.price_for("deepseek-v4-flash")       # 0.14 / 0.28
    base_in, base_out = config.counterfactual_price()          # 同模型 → 与上相同
    M = 1_000_000.0
    info = accounting.record(
        ts=time.time(), account_id=None, affinity_key="k",
        requested_model="deepseek-v4-flash", model="deepseek-v4-flash",
        effort_in="max", effort_out="low", prompt_tokens=10_000, cached_tokens=9_000,
        completion_tokens=1_000, saved_tokens=500, tool_rounds=3, latency_ms=800,
        status="ok", retried=0, replay_hit=False)
    baseline = 10_500 / M * base_in + 1_000 / M * base_out
    actual_expected = (1_000 / M * m_in + 9_000 / M * m_in * config.CACHED_PRICE_RATIO
                       + 1_000 / M * m_out)
    assert info["cost_baseline"] == pytest.approx(baseline, rel=1e-6)
    assert info["save_cache"] == pytest.approx(9_000 / M * m_in * 0.9, rel=1e-6)
    assert info["save_transform"] == pytest.approx(500 / M * m_in, rel=1e-6)
    assert (info["save_cache"] + info["save_free"] + info["save_transform"]
            + info["save_replay"]) == pytest.approx(
        info["cost_baseline"] - info["cost_actual"], rel=1e-6)
    assert info["cost_actual"] == pytest.approx(actual_expected, rel=1e-6)


def test_accounting_replay_full_baseline():
    info = accounting.record(
        ts=time.time(), account_id=None, affinity_key="k",
        requested_model="deepseek-v4-flash", model="deepseek-v4-flash",
        effort_in=None, effort_out=None, prompt_tokens=0, cached_tokens=0,
        completion_tokens=0, saved_tokens=0, tool_rounds=0, latency_ms=1,
        status="ok", retried=0, replay_hit=True)
    assert info["save_replay"] == pytest.approx(info["cost_baseline"])
    assert info["cost_actual"] == pytest.approx(0.0)


def test_accounting_free_model_zero_cost():
    info = accounting.record(
        ts=time.time(), account_id=None, affinity_key="k",
        requested_model="mimo-v2.5", model="mimo-v2.5", effort_in=None,
        effort_out=None, prompt_tokens=10_000, cached_tokens=0,
        completion_tokens=2_000, saved_tokens=0, tool_rounds=0, latency_ms=5,
        status="ok", retried=0, replay_hit=False)
    assert info["cost_actual"] == pytest.approx(0.0)
    assert info["save_free"] == pytest.approx(
        info["cost_baseline"], rel=1e-6)   # 免费档: 全部进"免费档路由"段


# ---------------- 优化器 ----------------

def test_effort_first_round_downgraded():
    body = {"model": "m", "reasoning_effort": "max", "tools": [{"t": 1}],
            "messages": [{"role": "user", "content": "hi"}]}
    out, before, after = optimizers.rewrite_effort(body)
    assert (before, after) == ("max", "low")
    assert out["reasoning_effort"] == "low"
    assert body["reasoning_effort"] == "max"          # 不改调用方对象


def test_effort_never_upgrades():
    body = {"model": "m", "reasoning_effort": "low", "tools": [1],
            "messages": [{"role": "user", "content": "x"}]}
    _, _, after = optimizers.rewrite_effort(body)
    assert after == "low"


def test_effort_final_rounds_untouched():
    msgs = [{"role": "user", "content": "x"}]
    msgs += [{"role": "assistant", "content": ""}, {"role": "tool", "content": "{}"}] * 5
    body = {"model": "m", "reasoning_effort": "max", "tools": [1], "messages": msgs}
    _, _, after = optimizers.rewrite_effort(body)
    assert after == "max"                              # 尾声轮(出结论)不动


def test_effort_no_tools_untouched():
    body = {"model": "m", "reasoning_effort": "max", "messages": [{"role": "user", "content": "x"}]}
    _, _, after = optimizers.rewrite_effort(body)
    assert after == "max"


def test_compact_large_array_keeps_tail():
    """通用工具结果压缩: 只碰超阈值消息, 数组只裁头部(保留最近 N 条)。"""
    rows = [{"ts": f"2026-09-{i:02d}T09:30:00", "open": 1, "high": 2,
             "low": 0.5, "close": 1.5, "volume": 12345} for i in range(1, 61)]
    body = {"messages": [{"role": "tool", "content": json.dumps(rows, ensure_ascii=False)}]}
    assert len(body["messages"][0]["content"]) > config.TOOL_TRUNC_CHARS
    out, saved = optimizers.compact_tool_results(body)
    kept = json.loads(out["messages"][0]["content"])
    assert len(kept) == config.TOOL_ARRAY_KEEP
    assert kept[-1]["ts"] == rows[-1]["ts"]          # 保留的是最近的数据
    assert saved > 0


def test_compact_ignores_small_and_non_tool():
    small = json.dumps([{"close": 1}] * 5)
    body = {"messages": [
        {"role": "tool", "content": small},
        {"role": "tool", "content": "not json"},
        {"role": "user", "content": "x" * 9000},
    ]}
    out, saved = optimizers.compact_tool_results(body)
    assert saved == 0                                 # 小消息与非 tool 消息一字不动
    assert out["messages"][0]["content"] == small


def test_compact_long_text_gets_marker():
    body = {"messages": [{"role": "tool", "content": "x" * 5000}]}
    out, saved = optimizers.compact_tool_results(body)
    assert saved > 0
    assert "[siphon:" in out["messages"][0]["content"]


def test_effort_disabled_by_default_and_opt_in():
    """质量优先: effort 控制默认关闭, 显式 opt-in(enabled=True)才生效。"""
    body = {"model": "m", "reasoning_effort": "max", "tools": [{"t": 1}],
            "messages": [{"role": "user", "content": "hi"}]}
    _, _, after = optimizers.rewrite_effort(body, enabled=False)
    assert after == "max"                             # 未开启: 原样
    _, _, after = optimizers.rewrite_effort(body, enabled=True)
    assert after == "low"


def test_replay_roundtrip():
    key = optimizers.replay_key("m", {"messages": [{"role": "user", "content": "x"}]})
    assert optimizers.replay_get(key) is None
    optimizers.replay_put(key, b"raw", "application/json", {"prompt_tokens": 5})
    raw, ctype, usage = optimizers.replay_get(key)
    assert raw == b"raw" and ctype == "application/json" and usage["prompt_tokens"] == 5


# ---------------- 账号池与路由 ----------------

def test_router_sticky_and_failover():
    a = _mkacct(alias="A", primary=True)
    b = _mkacct(key="sk-test-ef01", alias="B")
    router.bind("sess-1", a.id)
    assert router.pick("sess-1", "deepseek-v4-flash").id == a.id

    # 主力熔断 → 粘性失效 → 落到 B
    accounts.mark_account_open(a.id, ttl=600)
    assert router.pick("sess-1", "deepseek-v4-flash").id == b.id


def test_pick_skips_excluded_accounts():
    """B5 回归: 重试时已失败的账号必须跳过, 不能 break 掉仍有额度的账号。"""
    a = _mkacct(alias="A", primary=True)
    b = _mkacct(key="sk-test-ef04", alias="B")
    assert router.pick("", "deepseek-v4-flash", exclude={a.id}).id == b.id
    with pytest.raises(router.NoRoute):
        router.pick("", "deepseek-v4-flash", exclude={a.id, b.id})


def test_mark_account_open_uses_ttl_seconds():
    """B2 回归: 熔断内部一律 monotonic + TTL(秒), 不接受墙钟时间戳。

    quota 轮询曾把 time.time()+3600 传进来 —— 两套时钟混用等于永久熔断。
    用「剩余时间 ≈ ttl」验证契约, 不比较时钟量级(平台差异不可靠)。
    """
    a = _mkacct(alias="A", primary=True)
    _mkacct(key="sk-test-ef05", alias="B")
    accounts.mark_account_open(a.id, ttl=600)
    st = accounts.breaker_state(a.id)
    assert st["account_open_until"] > 0
    remaining = st["account_open_until"] - time.monotonic()
    assert 590 <= remaining <= 610, f"剩余 {remaining:.0f}s 应 ≈ ttl 600s"
    assert st["account_open_until_iso"]                    # 展示用墙钟 ISO 已换算
    assert router.pick("", "deepseek-v4-flash").id != a.id


def test_router_model_down_only_affects_that_model():
    a = _mkacct(alias="A", primary=True)
    b = _mkacct(key="sk-test-ef02", alias="B", allowlist=["deepseek-v4-flash"])
    accounts.mark_model_down(a.id, "deepseek-v4.1-flash")
    assert router.pick("", "deepseek-v4-flash").id == a.id       # 其他模型不受影响
    # A 模型级熔断 + B 白名单不含该模型 → 无路由
    with pytest.raises(router.NoRoute):
        router.pick("", "deepseek-v4.1-flash")
    # B 白名单放开后即可路由
    accounts.update(b.id, allowlist=["deepseek-v4.1-flash"])
    assert router.pick("", "deepseek-v4.1-flash").id == b.id


def test_router_allowlist():
    a = _mkacct(alias="A", primary=True, allowlist=["deepseek-v4-flash"])
    with pytest.raises(router.NoRoute):
        router.pick("", "deepseek-v4.1-flash")


def test_account_breaker_two_models_escalates():
    a = _mkacct(alias="A", primary=True)
    accounts.mark_model_down(a.id, "m1")
    assert accounts.breaker_state(a.id)["account_open_until"] == 0
    accounts.mark_model_down(a.id, "m2")                          # 第二个模型也挂 → 账号级
    assert accounts.breaker_state(a.id)["account_open_until"] > 0


def test_inflight_counter():
    a = _mkacct(alias="A")
    accounts.inflight_inc(a.id)
    accounts.inflight_inc(a.id)
    accounts.inflight_dec(a.id)
    assert accounts.inflight(a.id) == 1


# ---------------- 聚合 ----------------

def test_overview_and_savings_parts_sum():
    for _ in range(3):
        accounting.record(
            ts=time.time(), account_id=None, affinity_key="k",
            requested_model="deepseek-v4-flash", model="deepseek-v4-flash",
            effort_in="max", effort_out="low", prompt_tokens=1_000, cached_tokens=800,
            completion_tokens=200, saved_tokens=50, tool_rounds=1, latency_ms=100,
            status="ok", retried=0, replay_hit=False)
    ov = accounting.overview("today")
    assert ov["totals"]["requests"] == 3
    sa = accounting.savings_attribution("today")
    parts_sum = sum(p["amount"] for p in sa["parts"])
    assert parts_sum + sa["actual"] == pytest.approx(sa["baseline"], rel=1e-4)


def test_logs_filter_by_cached():
    accounting.record(ts=time.time(), account_id=None, affinity_key="k",
                      requested_model="m", model="m", effort_in=None, effort_out=None,
                      prompt_tokens=100, cached_tokens=90, completion_tokens=10,
                      saved_tokens=0, tool_rounds=0, latency_ms=1, status="ok",
                      retried=0, replay_hit=False)
    accounting.record(ts=time.time(), account_id=None, affinity_key="k",
                      requested_model="m", model="m", effort_in=None, effort_out=None,
                      prompt_tokens=100, cached_tokens=0, completion_tokens=10,
                      saved_tokens=0, tool_rounds=0, latency_ms=1, status="ok",
                      retried=0, replay_hit=False)
    assert accounting.logs(cached=True)["total"] == 1
    assert accounting.logs(cached=False)["total"] == 1
    assert accounting.logs()["total"] == 2


def test_model_usage_caps():
    a = _mkacct(alias="A")
    accounting.record(ts=time.time(), account_id=a.id, affinity_key="k",
                      requested_model="deepseek-v4-flash", model="deepseek-v4-flash",
                      effort_in=None, effort_out=None, prompt_tokens=1_000_000,
                      cached_tokens=0, completion_tokens=0, saved_tokens=0,
                      tool_rounds=0, latency_ms=1, status="ok", retried=0, replay_hit=False)
    rows = accounting.model_usage_all()
    assert rows[0]["cap_usd"] == config.MODEL_CAPS_USD["deepseek-v4-flash"]
    assert rows[0]["cap_pct"] == pytest.approx(round(100 * 0.14 / 30.0, 2), abs=1e-9)
