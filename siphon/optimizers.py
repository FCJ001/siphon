"""优化器: 分轮次 effort 控制 / 工具结果规范化 / 精确响应回放。

规则(siphon design.md §5):
  - effort 只降不升; 出结论的尾声轮不降
  - 结果规范化只处理"新生成内容"(全价), 不做历史折叠(缓存价, 负收益)
  - 回放只对低温度请求启用
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict

from . import config

# ---------------- 分轮次 effort ----------------

def tool_rounds(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")


def rewrite_effort(body: dict) -> tuple[dict, str | None, str | None]:
    """返回 (改写后body, 改写前effort, 改写后effort)。未改写时前后相同。"""
    if not config.OPT_EFFORT or not body.get("tools"):
        return body, body.get("reasoning_effort"), body.get("reasoning_effort")

    rounds = tool_rounds(body.get("messages") or [])
    if rounds == 0:
        want = config.OPT_EFFORT_FIRST
    elif rounds < config.OPT_EFFORT_MID_UNTIL:
        want = config.OPT_EFFORT_MID
    else:
        return body, body.get("reasoning_effort"), body.get("reasoning_effort")

    cur = body.get("reasoning_effort")
    if cur and config.effort_rank(want) >= config.effort_rank(cur):
        return body, cur, cur                      # 只降不升
    out = dict(body)
    out["reasoning_effort"] = want
    return out, cur, want


# ---------------- 工具结果规范化(日K降采样) ----------------

_KLINE_KEYS = ("date", "open", "high", "low", "close", "volume")


def downsample_tool_results(body: dict) -> tuple[dict, int]:
    """把 tool 消息里的日K数组压到最近 N 根。返回 (body, 省下的字符数)。"""
    if not config.OPT_KLINE:
        return body, 0
    saved = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, 0
    for i, m in enumerate(messages):
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue
        if not (isinstance(data, list) and data
                and isinstance(data[0], dict) and "close" in data[0]):
            continue
        if len(data) <= config.OPT_KLINE_KEEP:
            continue
        trimmed = json.dumps(
            [{k: r.get(k) for k in _KLINE_KEYS} for r in data[-config.OPT_KLINE_KEEP:]],
            ensure_ascii=False)
        saved += len(content) - len(trimmed)
        messages[i] = {**m, "content": trimmed}
    return body, saved


# ---------------- 精确响应回放 ----------------
# 进程内 LRU; 上量后换 Redis(接口不变)

_cache: OrderedDict[str, tuple[float, bytes, str, dict]] = OrderedDict()
_MAX_ENTRIES = 500


def replay_key(model: str, body: dict) -> str:
    basis = json.dumps({
        "model": model,
        "messages": body.get("messages"),
        "tools": bool(body.get("tools")),
        "t": body.get("temperature"),
        "e": body.get("reasoning_effort"),
    }, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(basis.encode()).hexdigest()


def replayable(body: dict) -> bool:
    temp = body.get("temperature")
    return config.OPT_REPLAY and isinstance(temp, (int, float)) and temp <= config.OPT_REPLAY_MAX_TEMP


def replay_get(key: str) -> tuple[bytes, str, dict] | None:
    """命中 → (响应字节, content-type, 首次的真实 usage)。"""
    item = _cache.get(key)
    if not item:
        return None
    exp, raw, ctype, usage = item
    if exp < time.time():
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return raw, ctype, usage


def replay_put(key: str, raw: bytes, ctype: str, usage: dict) -> None:
    while len(_cache) >= _MAX_ENTRIES:
        _cache.popitem(last=False)
    _cache[key] = (time.time() + config.OPT_REPLAY_TTL, raw, ctype, usage)


# ---------------- 请求指纹(记账/排重辅助) ----------------

def estimate_tokens_saved(chars: int) -> int:
    """字符 → token 的粗估(JSON/中文混合约 3 字符 1 token)。"""
    return max(0, int(chars / 3.0))
