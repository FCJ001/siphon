"""管理后台 API(/api/*)。鉴权与代理同源: Bearer = SIPHON_MASTER_KEY(P0 单租户)。"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from . import accounts, accounting, config, db, events, quota, router as router_mod, upstream

api = APIRouter(prefix="/api")


def _auth(authorization: str | None, token: str | None = None) -> bool:
    supplied = token or ""
    if authorization:
        supplied = authorization.strip().removeprefix("Bearer ").strip()
    return bool(supplied) and supplied == config.MASTER_KEY


def _err(status: int, message: str):
    from fastapi.responses import JSONResponse
    return JSONResponse({"code": status, "message": message, "data": None}, status_code=status)


def _ok(data=None):
    return {"code": 0, "message": "ok", "data": data}


# ---------------- 登录 ----------------

@api.post("/login")
async def login(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _err(400, "bad json")
    if body.get("password") != config.MASTER_KEY:
        return _err(401, "密码错误")
    return _ok({"token": config.MASTER_KEY})


# ---------------- 总览/分析 ----------------

@api.get("/overview")
async def overview(range: str = Query("today"), authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    data = accounting.overview(range or "today")
    data["accounts"] = _accounts_view()
    data["runtime"] = accounts.snapshot()
    return _ok(data)


@api.get("/analytics/timeseries")
async def timeseries(metric: str = Query("tokens"), granularity: str = Query("hour"),
                     group_by: str = Query("none"), hours: int = Query(168),
                     authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    rows = accounting.timeseries(metric=metric, granularity=granularity,
                                 group_by=group_by, since=time.time() - hours * 3600)
    return _ok({"items": rows})


@api.get("/analytics/savings-attribution")
async def savings(range: str = Query("today"), authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok(accounting.savings_attribution(range or "today"))


@api.get("/model-usage")
async def model_usage(authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok(accounting.model_usage_all())


# ---------------- 账号 ----------------

def _accounts_view() -> list[dict]:
    out = []
    for a in accounts.all_accounts():
        d = a.to_public()
        d.update({
            "inflight": accounts.inflight(a.id),
            "used_pct": round(accounts.headroom(a.id), 1),
            "windows": accounts.windows(a.id),
            "resets": accounts.resets(a.id),
            "breaker": accounts.breaker_state(a.id),
        })
        out.append(d)
    return out


@api.get("/accounts")
async def list_accounts(authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok({"items": _accounts_view(), "policy": _policy()})


@api.post("/accounts")
async def create_account(request: Request, authorization: str | None = Header(None),
                         force: bool = Query(False)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    try:
        body = await request.json()
    except ValueError:
        return _err(400, "bad json")
    key = (body.get("key") or "").strip()
    if not key:
        return _err(400, "key 不能为空")

    windows, reason = await quota.fetch_usage(key)
    if windows is None and not force:
        return _err(400, f"key 校验失败: {reason}")

    acct = accounts.create(key=key, alias=body.get("alias") or "",
                           note=body.get("note") or "",
                           primary=bool(body.get("primary")),
                           weight=int(body.get("weight") or 100),
                           enabled=bool(body.get("enabled", True)),
                           inflight_cap=body.get("inflight_cap"),
                           allowlist=body.get("allowlist"))
    if windows is not None:
        worst = max((w["percent"] for w in windows.values()), default=0.0)
        accounts.headroom_set(acct.id, worst,
                              {k: v.get("resetsAt") for k, v in windows.items()})
    return _ok({"account": acct.to_public(), "validated": windows is not None})


@api.patch("/accounts/{aid}")
async def update_account(aid: int, request: Request,
                         authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    body = await request.json()
    fields = {k: v for k, v in body.items()
              if k in ("alias", "note", "primary", "weight", "enabled",
                       "inflight_cap", "allowlist", "session_id", "key")}
    if body.get("key") in ("", None):
        fields.pop("key", None)
    accounts.update(aid, **fields)
    return _ok(accounts.get(aid).to_public() if accounts.get(aid) else None)


@api.delete("/accounts/{aid}")
async def delete_account(aid: int, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    accounts.delete(aid)
    return _ok()


@api.get("/accounts/{aid}/detail")
async def account_detail(aid: int, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    acct = accounts.get(aid)
    if not acct:
        return _err(404, "账号不存在")
    snaps = db.query("SELECT * FROM quota_snapshots WHERE account_id=? "
                     "ORDER BY id DESC LIMIT 24", (aid,))
    model_rows = [r for r in accounting.model_usage_all() if r["account_id"] == aid]
    recent = accounting.logs(account_id=aid, page=1, page_size=20)
    return _ok({"account": {**acct.to_public(), "key_tail": acct.key_tail,
                            "used_pct": round(accounts.headroom(aid), 1),
                            "resets": accounts.resets(aid),
                            "breaker": accounts.breaker_state(aid),
                            "inflight": accounts.inflight(aid)},
                "quota_history": list(reversed(snaps)),
                "model_usage": model_rows,
                "recent_requests": recent["items"]})


@api.post("/accounts/{aid}/reset-breaker")
async def reset_breaker(aid: int, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    accounts.clear_breaker(aid)
    return _ok()


@api.post("/accounts/{aid}/reset-session")
async def reset_session(aid: int, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    accounts.update(aid, session_id=accounts.new_session_id())
    return _ok()


@api.post("/accounts/{aid}/test")
async def test_account(aid: int, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    acct = accounts.get(aid)
    if not acct:
        return _err(404, "账号不存在")
    windows, reason = await quota.fetch_usage(acct.key)
    return _ok({"ok": windows is not None, "windows": windows, "reason": reason})


@api.post("/accounts/bulk")
async def bulk_create(request: Request, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    body = await request.json()
    keys = [k.strip() for k in (body.get("keys") or []) if k.strip()]
    defaults = body.get("defaults") or {}
    created = []
    for i, k in enumerate(keys):
        acct = accounts.create(key=k, alias=defaults.get("alias") or "",
                               primary=False, weight=int(defaults.get("weight") or 100))
        created.append(acct.to_public())
    return _ok({"created": created})


@api.post("/accounts/bulk-action")
async def bulk_action(request: Request, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    body = await request.json()
    action = body.get("action")
    if action not in ("enable", "disable"):
        return _err(400, "action 仅支持 enable/disable")
    for aid in body.get("ids") or []:
        accounts.update(int(aid), enabled=(action == "enable"))
    return _ok()


@api.get("/accounts/policy")
async def get_policy(authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok(_policy())


def _policy() -> dict:
    d = db.kv_get("policy", {})
    return {
        "overflow": d.get("overflow", "strict"),        # strict=严格粘性 | spill=允许溢出
        "affinity_ttl": d.get("affinity_ttl", config.AFFINITY_TTL),
        "primary_inflight_cap": d.get("primary_inflight_cap", config.PRIMARY_INFLIGHT_CAP),
        "free_routing": d.get("free_routing", True),
    }


@api.put("/accounts/policy")
async def put_policy(request: Request, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    body = await request.json()
    old = _policy()
    db.kv_set("policy", {**old, **{k: v for k, v in body.items()
                                   if k in ("overflow", "affinity_ttl",
                                            "primary_inflight_cap", "free_routing")}})
    db.log_audit("policy.update", target="policy", before=old, after=_policy())
    return _ok(_policy())


@api.get("/accounts/export")
async def export_accounts(with_secrets: bool = Query(False),
                          authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    items = []
    for a in accounts.all_accounts():
        d = a.to_public(with_key=with_secrets)
        items.append(d)
    return _ok({"accounts": items, "with_secrets": with_secrets})


@api.post("/accounts/import")
async def import_accounts(request: Request, authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    body = await request.json()
    mode = body.get("mode", "merge")
    n = 0
    for item in body.get("accounts") or []:
        key = item.get("key")
        if not key:
            continue
        accounts.create(key=key, alias=item.get("alias") or "",
                        primary=bool(item.get("primary")),
                        weight=int(item.get("weight") or 100))
        n += 1
    return _ok({"imported": n, "mode": mode})


@api.post("/accounts/preview-routing")
async def preview_routing(request: Request, authorization: str | None = Header(None)):
    """改动权重/白名单后的路由分布试算: 按当前 headroom 模拟软分配。"""
    if not _auth(authorization):
        return _err(401, "unauthorized")
    await request.json()
    accounts_list = _accounts_view()
    active = [a for a in accounts_list if a["enabled"]]
    if not active:
        return _ok({"distribution": [], "note": "无启用账号"})
    scores = []
    for a in active:
        remaining = max(0.0, 100.0 - a["used_pct"])
        scores.append(max(0.01, remaining * (a["weight"] / 100.0)))
    total = sum(scores)
    dist = [{"account": f"{a['alias'] or a['key_tail']}",
             "share_pct": round(s / total * 100, 1)}
            for a, s in zip(active, scores)]
    return _ok({"distribution": dist,
                "note": "按「剩余额度 × 权重」模拟的软分布, 实际受会话粘性影响"})


# ---------------- 日志 ----------------

@api.get("/logs")
async def logs(account_id: int | None = None, model: str | None = None,
               status: str | None = None, replay: bool | None = None,
               cached: bool | None = None, hours: int = Query(168),
               page: int = Query(1), page_size: int = Query(50),
               authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok(accounting.logs(account_id=account_id, model=model, status=status,
                               replay=replay, cached=cached,
                               since=time.time() - hours * 3600,
                               page=page, page_size=page_size))


# ---------------- 配置 ----------------

@api.get("/config")
async def get_config(authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok({
        "optimizers": {
            "effort": {"enabled": config.OPT_EFFORT,
                       "note": "默认关闭(质量优先); 客户端可带 x-siphon-effort-policy: optimize 按请求开启",
                       "first": config.OPT_EFFORT_FIRST,
                       "mid": config.OPT_EFFORT_MID, "mid_until": config.OPT_EFFORT_MID_UNTIL},
            "tooltrunc": {"enabled": config.OPT_TOOLTRUNC,
                          "chars": config.TOOL_TRUNC_CHARS,
                          "array_keep": config.TOOL_ARRAY_KEEP},
            "replay": {"enabled": config.OPT_REPLAY, "ttl": config.OPT_REPLAY_TTL,
                       "max_temp": config.OPT_REPLAY_MAX_TEMP},
        },
        "prices": config.PRICES, "model_caps_usd": config.MODEL_CAPS_USD,
        "note": "P0 只读; 优化器开关的热加载在 P3(kv 覆盖)接入",
    })


# ---------------- 事件 ----------------

@api.get("/events")
async def event_history(limit: int = Query(100), authorization: str | None = Header(None)):
    if not _auth(authorization):
        return _err(401, "unauthorized")
    return _ok({"items": events.history(limit)})


@api.get("/events/stream")
async def event_stream(token: str | None = Query(None)):
    if not _auth(None, token):
        return _err(401, "unauthorized")
    q = events.register()

    async def gen():
        try:
            yield events.sse_format({"topic": "hello", "payload": {"ts": time.time()}})
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield events.sse_format(item)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"     # 心跳防代理断连
        finally:
            events.unregister(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
