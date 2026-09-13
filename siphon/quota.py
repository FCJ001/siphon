""" /usage 轮询: 账号级三窗口百分比 → headroom + 快照 + SSE 事件。"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from . import accounts, config, db, events

_last_notify: float = 0.0
_NOTIFY_COOLDOWN = 1800


async def fetch_usage(key: str) -> tuple[dict | None, str]:
    """GET {base}/usage → ({"rolling":{percent,resetsAt}, ...}, 原因)。不抛异常。"""
    url = f"{config.OPENCODE_BASE_URL.rstrip('/')}/usage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": config.USER_AGENT,
            })
        if resp.status_code != 200:
            return None, f"usage 端点返回 {resp.status_code}"
        data = resp.json()
    except Exception as e:  # noqa: BLE001 巡检失败不影响主链路
        return None, f"usage 查询失败: {str(e)[:120]}"

    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, "usage 响应结构不识别"

    windows: dict[str, dict] = {}
    for name, info in usage.items():
        if isinstance(info, dict) and "percent" in info:
            windows[name] = {"percent": float(info.get("percent") or 0),
                             "resetsAt": info.get("resetsAt")}
    return (windows or None), ""


async def poll_once() -> dict:
    """轮询全部启用账号一次, 回填 headroom, 落快照, 推事件。"""
    result = {"checked": 0, "failed": 0, "accounts": []}
    for acct in accounts.all_accounts():
        if not acct.enabled:
            continue
        result["checked"] += 1
        windows, reason = await fetch_usage(acct.key)
        if windows is None:
            result["failed"] += 1
            if reason.startswith("usage 端点返回 401"):
                # key 鉴权失败: 账号级熔断 24h(传 TTL 秒数, 不是墙钟时间戳)
                accounts.mark_account_open(acct.id, ttl=86400)
                await _notify(f"账号 {acct.alias or acct.key_tail} 的 key 鉴权失败(401), 已停用 24h")
            db.log_event("quota.error", {"account_id": acct.id, "reason": reason})
            continue

        worst = max((w["percent"] for w in windows.values()), default=0.0)
        resets = {k: v.get("resetsAt") for k, v in windows.items()}
        accounts.headroom_set(acct.id, worst, resets, windows=windows)
        db.execute(
            "INSERT INTO quota_snapshots(ts,account_id,rolling,weekly,monthly,resets) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), acct.id,
             windows.get("rolling", {}).get("percent"),
             windows.get("weekly", {}).get("percent"),
             windows.get("monthly", {}).get("percent"),
             json.dumps(resets)))  # JSON 而非 str(): 启动时要从这里恢复水位
        # 月/周窗口接近耗尽时, 若上游还没开始 429, 提前把该账号降权由路由自然完成
        if worst >= 85:
            db.log_event("quota.warning", {"account_id": acct.id, "worst_pct": worst})
        result["accounts"].append({"id": acct.id, "alias": acct.alias,
                                   "windows": windows, "worst": worst})
    await events.publish("quota", result)
    return result


async def _notify(text: str) -> None:
    global _last_notify
    now = time.time()
    if now - _last_notify < _NOTIFY_COOLDOWN:
        return
    _last_notify = now
    db.log_event("alert", {"text": text})
    # TODO(P3): 接飞书/webhook


async def poll_loop() -> None:
    await asyncio.sleep(1)
    while True:
        try:
            await poll_once()
        except Exception as e:  # noqa: BLE001 轮询永不退出
            db.log_event("quota.loop_error", {"error": str(e)[:200]})
        await asyncio.sleep(config.POLL_INTERVAL)
