"""优化器: 工具结果压缩 / 精确响应回放 / (可选)分轮次 effort 控制。

质量优先(siphon design.md §5):
  - 工具结果压缩只碰超过阈值的大消息, 常规结果一字不动; 数组只裁头部(时序最近最有用)
  - effort 控制默认关闭, 仅客户端显式按请求开启; 且只降不升
  - 回放只对低温度请求启用
本模块不含任何具体业务/上游语义 —— 对所有 OpenAI 兼容客户端一视同仁。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict

from . import config

# ---------------- (可选)分轮次 effort ----------------

def tool_rounds(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")


def effort_opt_in(request_headers: dict | None = None) -> bool:
    """effort 优化是否对该请求生效: 全局开关 或 客户端显式 opt-in。"""
    if config.OPT_EFFORT:
        return True
    if request_headers:
        return str(request_headers.get("x-siphon-effort-policy", "")).lower() == "optimize"
    return False


def rewrite_effort(body: dict, enabled: bool = True) -> tuple[dict, str | None, str | None]:
    """返回 (改写后body, 改写前effort, 改写后effort)。未改写时前后相同。"""
    if not enabled or not body.get("tools"):
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


# ---------------- 工具结果压缩(通用, 与业务无关) ----------------
_TRUNC_MARK = "\n…[siphon: 超长工具结果已截断]"


def compact_tool_results(body: dict) -> tuple[dict, int]:
    """超大 tool 消息的保守压缩。返回 (body, 省下的字符数)。

    质量优先的边界:
      - 只处理超过 TOOL_TRUNC_CHARS 的消息, 常规结果不动
      - JSON 数组: 只裁头部, 保留最近 TOOL_ARRAY_KEEP 条(时间序列最近的最有用)
      - 非数组长文本: 尾部截断并留标记
      - 不重写字段、不删字段、不做任何"摘要" —— 摘要会改变模型看到的事实
    """
    if not config.OPT_TOOLTRUNC:
        return body, 0
    saved = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, 0
    for i, m in enumerate(messages):
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= config.TOOL_TRUNC_CHARS:
            continue
        try:
            data = json.loads(content)
        except ValueError:
            trimmed = content[:config.TOOL_TRUNC_CHARS] + _TRUNC_MARK
        else:
            if isinstance(data, list) and data and isinstance(data[0], dict) \
                    and len(data) > config.TOOL_ARRAY_KEEP:
                trimmed = json.dumps(data[-config.TOOL_ARRAY_KEEP:], ensure_ascii=False)
            else:
                trimmed = content[:config.TOOL_TRUNC_CHARS] + _TRUNC_MARK
        if len(trimmed) < len(content):
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
