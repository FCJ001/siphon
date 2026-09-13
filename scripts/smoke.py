"""Siphon 端到端冒烟 —— 假上游 + 真中转, 验证代理/记账/回放/故障转移。

用法: python scripts/smoke.py
不碰真实 OpenCode 网关; 假上游返回带 cached_tokens 的 usage 并可模拟 429。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = 4110
MOCK = 4310
DB = tempfile.mktemp(suffix=".db")
os.environ.update(
    SIPHON_MASTER_KEY="sk-smoke",
    SIPHON_PORT=str(PORT),
    SIPHON_DB=DB,
    OPENCODE_BASE_URL=f"http://127.0.0.1:{MOCK}/v1",
    OPENCODE_KEYS="sk-mock-key-1,sk-mock-key-2",   # 引导建两个账号
    SIPHON_POLL_INTERVAL="3600",
    SIPHON_OPT_EFFORT="1",                          # 冒烟要验证 effort 改写(生产默认关)
)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

seen: list[dict] = []
mock_429_keys: set[str] = {"sk-mock-key-1"}      # 这把 key 一律 429(测故障转移)

mock = FastAPI(docs_url=None, redoc_url=None)


def _usage(p: int = 4000, c: int = 3840, o: int = 100) -> dict:
    return {"prompt_tokens": p, "completion_tokens": o, "total_tokens": p + o,
            "prompt_tokens_details": {"cached_tokens": c, "cache_write_tokens": None}}


@mock.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    auth = request.headers.get("authorization", "")
    key = auth.removeprefix("Bearer ").strip()
    seen.append({"model": body.get("model"), "effort": body.get("reasoning_effort"),
                 "stream": body.get("stream"), "key_tail": key[-2:]})
    if key in mock_429_keys:
        return JSONResponse({"error": {"message": "FreeUsageLimitError: usage limit"}},
                            status_code=429)
    usage = _usage()
    if body.get("stream"):
        async def gen():
            for i in range(3):
                yield f'data: {json.dumps({"choices":[{"delta":{"content":f"c{i}"}}]})}\n\n'
            yield f'data: {json.dumps({"choices":[],"usage": usage})}\n\n'
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"id": "cmpl-1", "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": usage}


@mock.get("/v1/usage")
async def usage():
    return {"usage": {"rolling": {"status": "ok", "percent": 10,
                                  "resetsAt": "2026-09-13T12:00:00Z"},
                      "weekly": {"status": "ok", "percent": 20,
                                 "resetsAt": "2026-09-14T00:00:00Z"},
                      "monthly": {"status": "ok", "percent": 30,
                                  "resetsAt": "2026-10-01T00:00:00Z"}}}


def _serve(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()


_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    _serve(mock, MOCK)
    from siphon.main import app as siphon_app
    _serve(siphon_app, PORT)
    time.sleep(1.2)

    base = f"http://127.0.0.1:{PORT}"
    H = {"Authorization": "Bearer sk-smoke"}
    c = httpx.Client(base_url=base, headers=H, timeout=30)

    # 1 健康 + 引导建号
    h = c.get("/health").json()
    check("健康检查 & OPENCODE_KEYS 引导建号", h.get("accounts") == 2,
          f"accounts={h.get('accounts')}")

    # 2 非流式: 应自动绕开 429 的 key1, 落在 key2, 并透传 usage
    #   带 tools(触发分轮 effort 改写) + 低温度(启用精确回放)
    TOOLS = [{"type": "function", "function": {"name": "query_data", "parameters": {}}}]
    body = {"model": "deepseek-v4-flash", "reasoning_effort": "max",
            "temperature": 0.1, "tools": TOOLS,
            "messages": [{"role": "user", "content": "你好"}]}
    r = c.post("/v1/chat/completions", json=body)
    if r.status_code != 200 or "usage" not in r.json():
        print("  [debug] status:", r.status_code, "body:", r.text[:300])
    got = r.json().get("usage", {}).get("prompt_tokens_details", {}) if r.status_code == 200 else {}
    ok = got.get("cached_tokens") == 3840
    check("非流式请求 200 + usage 透传", ok,
          f"status={r.status_code} cached={got.get('cached_tokens')}")
    check("429 故障转移(绕开 key1 落 key2)",
          r.headers.get("x-siphon-account", "").startswith("2:"),
          f"account={r.headers.get('x-siphon-account')}")
    check("分轮 effort 改写(max→low)", seen[-1]["effort"] == "low",
          f"upstream 收到 effort={seen[-1]['effort']}")

    # 3 记账: cached_tokens 落库, 成本按 USD/1M
    import sqlite3
    row = sqlite3.connect(DB).execute(
        "SELECT prompt_tokens,cached_tokens,completion_tokens,cost_actual,"
        "effort_in,effort_out,status FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    expect_actual = 160 / 1e6 * 0.14 + 3840 / 1e6 * 0.14 * 0.1 + 100 / 1e6 * 0.28
    check("记账落库(cached_tokens/effort 改写)", 
          bool(row) and row[0] == 4000 and row[1] == 3840 and row[4] == "max" and row[5] == "low",
          f"row={row}")
    check("成本口径(USD/1M, 缓存 0.1x)", row and abs(row[3] - expect_actual) < 1e-6,
          f"cost={row[3] if row else '?'} ≈ {expect_actual:.9f}(库内 6 位小数)")

    # 4 精确回放: 同参数第二发不出站
    r2 = c.post("/v1/chat/completions", json=body)
    check("精确响应回放(不出站)", r2.status_code == 200 and r2.headers.get("x-siphon-replay") == "1",
          f"replay={r2.headers.get('x-siphon-replay')}")

    # 5 流式透传 + usage 提取
    s = c.post("/v1/chat/completions", json={**body, "stream": True,
                                             "messages": [{"role": "user", "content": "流式"}]})
    text = s.text
    check("SSE 流式透传", s.status_code == 200 and "data: " in text and "[DONE]" in text)
    row2 = sqlite3.connect(DB).execute(
        "SELECT prompt_tokens,cached_tokens FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    check("流式记账(从 SSE 提取 usage)", row2 == (4000, 3840), f"row={row2}")

    # 6 管理台: 总览瀑布可加
    ov = c.get("/api/overview?range=today").json()["data"]
    t = ov["totals"]
    check("总览: 缓存命中率", abs(t["cache_hit_rate"] - 96.0) < 0.1,
          f"{t['cache_hit_rate']}%")
    sa = c.get("/api/analytics/savings-attribution?range=today").json()["data"]
    parts = sum(p["amount"] for p in sa["parts"])
    check("瀑布: 各段 + 实际 == 基线", abs(parts + sa["actual"] - sa["baseline"]) < 1e-9,
          f"baseline={sa['baseline']:.6f} actual={sa['actual']:.6f}")

    # 7 账号视图: /usage 轮询回填「最紧窗口」= 已用最多的那个(30%)
    accs = c.get("/api/accounts").json()["data"]["items"]
    check("配额轮询回填(最紧窗口 30%)",
          all(a["used_pct"] == 30.0 for a in accs),
          f"used_pct={[a['used_pct'] for a in accs]}")

    # 8 鉴权
    bad = httpx.post(f"{base}/v1/chat/completions", json=body,
                     headers={"Authorization": "Bearer wrong"}, timeout=10)
    check("代理鉴权拒绝错误 key", bad.status_code == 401)

    failed = [r for r in _results if not r[1]]
    print(f"\n{'=' * 46}\n冒烟结果: {len(_results) - len(failed)}/{len(_results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
