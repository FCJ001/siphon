"""事件总线: 后端各处 publish, SSE 端点 subscribe。"""
from __future__ import annotations

import asyncio
import json
import time

from . import db

_subs: set[asyncio.Queue] = set()


async def publish(topic: str, payload: dict) -> None:
    """广播给所有订阅者(无订阅者时零开销)。"""
    item = {"ts": time.time(), "topic": topic, "payload": payload}
    for q in list(_subs):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass


def register() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subs.add(q)
    return q


def publish_safe(topic: str, payload: dict) -> None:
    """同步上下文可用的发布: 有事件循环就调度, 没有就丢弃(记账已落库, 不丢数据)。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(publish(topic, payload))


def unregister(q: asyncio.Queue) -> None:
    _subs.discard(q)


def sse_format(item: dict) -> str:
    return f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"


def history(limit: int = 100) -> list[dict]:
    rows = db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [{"ts": r["ts"], "topic": r["type"], "payload": r["payload"]} for r in rows]
