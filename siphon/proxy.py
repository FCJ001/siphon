"""/v1/chat/completions —— OpenAI 兼容入口, SSE 流式透传 + 记账 + 优化器。

重试原则(design.md §5): 只有"尚未向客户端发出任何字节"的失败才换账号重试;
流式一旦开始产出, 失败原样上抛(盲目重放会重复计费)。
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import accounts, accounting, config, db, events, optimizers, router, upstream

api = APIRouter()

_NO_RETRY_STATUSES = {400, 401, 403, 404, 422}   # 请求本身的问题, 换账号也没用


def _auth(authorization: str | None) -> bool:
    return bool(authorization) and authorization.strip() == f"Bearer {config.MASTER_KEY}"


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"code": status, "message": message, "data": None}, status_code=status)


def _affinity_key(request: Request) -> str:
    """会话亲和键: 客户端自带会话标识(x-siphon-session / x-opencode-session / x-request-id)
    就直接复用; 都没有则共享 default 桶(缓存视角仍优于随机漂移)。"""
    for h in ("x-siphon-session", "x-opencode-session", "x-request-id"):
        v = request.headers.get(h)
        if v:
            return v
    return "default"


def parse_sse_usage(buf: bytes) -> dict:
    """从上游 SSE 原始字节里取最后一个 usage 块。"""
    usage: dict = {}
    for line in buf.decode("utf-8", "ignore").splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        u = obj.get("usage")
        if isinstance(u, dict) and u.get("prompt_tokens") is not None:
            usage = u
    return usage


def _tokens(usage: dict) -> tuple[int, int, int]:
    details = usage.get("prompt_tokens_details") or {}
    return (int(usage.get("prompt_tokens") or 0),
            int(details.get("cached_tokens") or 0),
            int(usage.get("completion_tokens") or 0))


def _cap_guard(account_id: int, model: str) -> None:
    """模型级月度上限: 本地账本过安全线即熔断降权, 路由自然绕开(撞 429 由熔断兜底)。"""
    base = str(model).split("@", 1)[0]
    cap = config.MODEL_CAPS_USD.get(base)
    if not cap:
        return
    row = db.query_one(
        "SELECT cost_usd FROM model_usage WHERE account_id=? AND model=? AND month=?",
        (account_id, base, time.strftime("%Y-%m")))
    if row and row["cost_usd"] >= cap * config.CAP_SAFETY:
        accounts.mark_model_down(account_id, base)


def _finish(meta: dict, acct: accounts.Account | None, usage: dict,
            status: str, latency_ms: int, error: str = "") -> None:
    """统一记账收口: 记账 → 模型上限守卫 → 粘性绑定 → 事件。"""
    prompt, cached, completion = _tokens(usage)
    info = accounting.record(
        ts=meta["started"], account_id=acct.id if acct else None,
        affinity_key=meta["affinity"], requested_model=meta["requested_model"],
        model=meta["requested_model"], effort_in=meta["effort_in"],
        effort_out=meta["effort_out"], prompt_tokens=prompt, cached_tokens=cached,
        completion_tokens=completion, saved_tokens=meta["saved_tokens"],
        tool_rounds=meta["rounds"], latency_ms=latency_ms, status=status,
        retried=meta["retried"], replay_hit=meta["replay"], error=error)
    if acct and status == "ok":
        _cap_guard(acct.id, meta["requested_model"])
        router.bind(meta["affinity"], acct.id)
    events.publish_safe("request", {
        "account": (acct.alias or acct.key_tail) if acct else None,
        "account_id": acct.id if acct else None,
        "model": meta["requested_model"],
        "effort": [meta["effort_in"], meta["effort_out"]],
        "prompt_tokens": prompt, "cached_tokens": cached,
        "completion_tokens": completion, "saved_tokens": meta["saved_tokens"],
        "tool_rounds": meta["rounds"], "latency_ms": latency_ms,
        "status": status, "replay_hit": meta["replay"],
        "cost_actual": info["cost_actual"],
    })


# ---------------- 非流式 ----------------

async def _nonstream_once(body: dict, hdrs: dict):
    """返回 (resp, None) 或 (None, 错误描述)。"""
    try:
        resp = await upstream.client().post(upstream.chat_url(), json=body, headers=hdrs)
    except httpx.HTTPError as e:
        return None, f"连接失败: {str(e)[:120]}"
    return resp, None


# ---------------- 流式 ----------------

async def _open_stream(body: dict, hdrs: dict):
    try:
        req = upstream.client().build_request("POST", upstream.chat_url(),
                                              json=body, headers=hdrs)
        resp = await upstream.client().send(req, stream=True)
    except httpx.HTTPError as e:
        return None, -1, f"连接失败: {str(e)[:120]}"
    return resp, resp.status_code, ""


def _stream_generator(resp: httpx.Response, acct: accounts.Account, meta: dict):
    async def gen():
        buf = bytearray()
        failed = False
        try:
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                yield chunk
        except httpx.HTTPError as e:
            failed = True
            db.log_event("stream.broken", {"account_id": acct.id, "error": str(e)[:150]})
        finally:
            await resp.aclose()
            accounts.inflight_dec(acct.id)
            latency = int((time.time() - meta["started"]) * 1000)
            usage = parse_sse_usage(bytes(buf)) if buf else {}
            if failed or not buf:
                # 断流必须记 error: 客户端拿到的是截断输出, 不能当成功计费
                _finish(meta, acct, usage, "error", latency,
                        "流式传输中断" if failed else "上游无返回")
                return
            _finish(meta, acct, usage, "ok", latency)
            if usage and meta.get("rkey"):
                optimizers.replay_put(meta["rkey"], bytes(buf), "text/event-stream", usage)
    return gen()


# ---------------- 端点 ----------------

@api.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "无效的 API key")

    try:
        body = await request.json()
    except ValueError:
        return _err(400, "请求体不是合法 JSON")
    if not isinstance(body, dict) or not body.get("model"):
        return _err(400, "缺少 model 字段")

    is_stream = bool(body.get("stream"))
    requested_model = str(body["model"])
    affinity = _affinity_key(request)
    rounds = optimizers.tool_rounds(body.get("messages") or [])
    started = time.time()

    # ---- 优化器(改写的是服务端自己的 body 副本, 客户端无感) ----
    # effort 控制质量敏感 → 默认全局关闭, 客户端带 x-siphon-effort-policy: optimize 才按请求启用
    body, effort_in, effort_out = optimizers.rewrite_effort(
        body, enabled=optimizers.effort_opt_in(dict(request.headers)))
    body, saved_chars = optimizers.compact_tool_results(body)
    saved_tokens = optimizers.estimate_tokens_saved(saved_chars)

    meta = {"started": started, "affinity": affinity, "requested_model": requested_model,
            "effort_in": effort_in, "effort_out": effort_out,
            "saved_tokens": saved_tokens, "rounds": rounds,
            "retried": 0, "replay": False}

    # ---- 精确回放: 整笔请求不出站 ----
    rkey = optimizers.replay_key(requested_model, body)
    meta["rkey"] = rkey
    if optimizers.replayable(body):
        hit = optimizers.replay_get(rkey)
        if hit:
            raw, ctype, usage = hit
            meta["replay"] = True
            _finish(meta, None, usage, "ok", int((time.time() - started) * 1000))
            return Response(content=raw, media_type=ctype,
                            headers={"x-siphon-replay": "1"})

    # ---- 选号 + 重试 ----
    exclude: set[int] = set()
    last_error = ""
    for attempt in range(config.UPSTREAM_RETRIES + 1):
        try:
            acct = router.pick(affinity, requested_model, exclude=exclude)
        except router.NoRoute as e:
            return _err(503, str(e))
        accounts.inflight_inc(acct.id)

        upstream_body = dict(body)
        if is_stream:
            so = dict(body.get("stream_options") or {})
            so.setdefault("include_usage", True)      # 记账依赖上游回传 usage
            upstream_body["stream_options"] = so
        hdrs = upstream.headers_for(acct.key, acct.session_id)

        if is_stream:
            resp, status, err = await _open_stream(upstream_body, hdrs)
            if err or status >= 400 or status < 200:
                accounts.inflight_dec(acct.id)
                if resp is not None:
                    await resp.aclose()
                # 请求本身的错误(400/401/422): 换账号结果相同, 直接把上游报错透传,
                # 不做无谓重试, 也不把真实原因藏成 503
                if status in _NO_RETRY_STATUSES:
                    try:
                        payload = await resp.aread() if resp is not None else b"{}"
                        detail = json.loads(payload)
                    except Exception:  # noqa: BLE001
                        detail = {}
                    _finish(meta, acct, detail.get("usage") or {}, "error",
                            int((time.time() - started) * 1000),
                            error=f"HTTP {status}: {str(detail.get('error', detail))[:120]}")
                    return JSONResponse(detail, status_code=status)
                last_error = err or f"上游 HTTP {status}"
                meta["retried"] = attempt
                if status == 429:
                    accounts.mark_model_down(acct.id, requested_model)
                elif status == 401:
                    accounts.mark_account_open(acct.id)
                exclude.add(acct.id)
                continue
            # 成功开流: 在途计数移交给生成器的 finally
            # 头只能 latin-1, 别名含中文会炸(实测) → 只放 id 和 key 尾号
            headers = {"x-siphon-account": f"{acct.id}:{acct.key_tail}"}
            return StreamingResponse(_stream_generator(resp, acct, meta),
                                     status_code=status, media_type="text/event-stream",
                                     headers=headers)

        resp, err = await _nonstream_once(upstream_body, hdrs)
        try:
            if err:
                last_error = err
                meta["retried"] = attempt
                exclude.add(acct.id)
                continue
            if resp.status_code in _NO_RETRY_STATUSES:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {"message": resp.text[:200]}
                _finish(meta, acct, payload.get("usage") or {}, "error",
                        int((time.time() - started) * 1000),
                        error=f"HTTP {resp.status_code}: {str(payload.get('message'))[:120]}")
                return JSONResponse(payload, status_code=resp.status_code)
            if resp.status_code == 429:
                accounts.mark_model_down(acct.id, requested_model)
                last_error = "上游 429(额度/限流)"
                meta["retried"] = attempt
                exclude.add(acct.id)
                continue
            if resp.status_code >= 500:
                last_error = f"上游 HTTP {resp.status_code}"
                meta["retried"] = attempt
                exclude.add(acct.id)
                continue
            # 成功
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            usage = payload.get("usage") or {}
            _finish(meta, acct, usage, "ok", int((time.time() - started) * 1000))
            if usage and optimizers.replayable(body):
                optimizers.replay_put(meta["rkey"],
                                      json.dumps(payload, ensure_ascii=False).encode(),
                                      "application/json", usage)
            return JSONResponse(payload, status_code=resp.status_code, headers={
                "x-siphon-account": f"{acct.id}:{acct.key_tail}"})
        finally:
            if not is_stream:
                accounts.inflight_dec(acct.id)
                if resp is not None:
                    await resp.aclose()

    _finish(meta, None, {}, "error", int((time.time() - started) * 1000),
            error=f"所有候选账号均失败: {last_error}")
    await events.publish("request", {"status": "exhausted", "error": last_error})
    return _err(503, f"所有候选账号均失败: {last_error[:120]}")
