"""Siphon 配置 —— 全部读环境变量，零第三方依赖（不用 pydantic-settings）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


def _env_json(name: str, default):
    raw = _env(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


# ---------------- 服务 ----------------
MASTER_KEY = _env("SIPHON_MASTER_KEY", "sk-siphon-dev")
HOST = _env("SIPHON_HOST", "127.0.0.1")
PORT = _env_int("SIPHON_PORT", 4100)
DB_PATH = Path(_env("SIPHON_DB", str(PROJECT_ROOT / "data" / "siphon.db")))

# ---------------- 上游(OpenCode Go) ----------------
OPENCODE_BASE_URL = _env("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")
# 首次启动且账号表为空时, 用它引导建号(逗号分隔); 之后一律以数据库为准
BOOTSTRAP_KEYS = _env_list("OPENCODE_KEYS")
USER_AGENT = "siphon-relay/0.1"

# ---------------- 配额与调度 ----------------
POLL_INTERVAL = _env_int("SIPHON_POLL_INTERVAL", 300)          # /usage 轮询间隔(秒)
AFFINITY_TTL = _env_int("SIPHON_AFFINITY_TTL", 3600)           # 会话粘账号时长(秒)
PRIMARY_INFLIGHT_CAP = _env_int("SIPHON_PRIMARY_INFLIGHT_CAP", 24)
MODEL_DOWN_TTL = _env_int("SIPHON_MODEL_DOWN_TTL", 900)        # 模型级熔断(秒)
ACCOUNT_DOWN_TTL = _env_int("SIPHON_ACCOUNT_DOWN_TTL", 600)    # 账号级熔断兜底(秒)
UPSTREAM_TIMEOUT = _env_float("SIPHON_UPSTREAM_TIMEOUT", 300.0)
UPSTREAM_RETRIES = _env_int("SIPHON_UPSTREAM_RETRIES", 2)      # 仅限尚未发出字节的失败

# ---------------- 计价(USD / 1M tokens) ----------------
# counterfactual 口径: 瀑布图的"基线"按中档模型全价计算, 保证各段节省可加且能对上总账
COUNTERFACTUAL_MODEL = _env("SIPHON_BASELINE_MODEL", "deepseek-v4-flash")
PRICES = _env_json("SIPHON_PRICES", {
    "deepseek-v4-flash": [0.14, 0.28],
    "deepseek-v4-flash-vision-exp": [0.14, 0.28],
    "deepseek-v4.1-flash": [0.14, 0.28],
    "deepseek-v4-pro": [1.74, 3.48],
    "mimo-v2.5": [0.0, 0.0],
})
DEFAULT_PRICE = tuple(_env_json("SIPHON_DEFAULT_PRICE", [0.5, 1.5]))
CACHED_PRICE_RATIO = _env_float("SIPHON_CACHED_RATIO", 0.1)
MODEL_CAPS_USD = _env_json("SIPHON_MODEL_CAPS_USD", {
    "deepseek-v4.1-flash": 15.0, "deepseek-v4-flash": 30.0, "mimo-v2.5": 30.0,
})
CAP_SAFETY = _env_float("SIPHON_CAP_SAFETY", 0.9)              # 模型级上限用到 90% 即降权

# ---------------- 优化器(质量优先: 默认全部保守, effort 控制默认关闭) ----------------
OPT_EFFORT = _env_bool("SIPHON_OPT_EFFORT", False)   # 默认关: 降 reasoning 深度有质量风险,
                                                     # 客户端可带 x-siphon-effort-policy: optimize 按请求开启
OPT_EFFORT_FIRST = _env("SIPHON_OPT_EFFORT_FIRST", "low")
OPT_EFFORT_MID = _env("SIPHON_OPT_EFFORT_MID", "medium")
OPT_EFFORT_MID_UNTIL = _env_int("SIPHON_OPT_EFFORT_MID_UNTIL", 4)
OPT_TOOLTRUNC = _env_bool("SIPHON_OPT_TOOLTRUNC", True)
TOOL_TRUNC_CHARS = _env_int("SIPHON_TOOL_TRUNC_CHARS", 3000)   # 超过才碰, 常规结果不动
TOOL_ARRAY_KEEP = _env_int("SIPHON_TOOL_ARRAY_KEEP", 30)       # 数组保留最近 N 条(时序: 最近最有用)
OPT_REPLAY = _env_bool("SIPHON_OPT_REPLAY", True)
OPT_REPLAY_TTL = _env_int("SIPHON_OPT_REPLAY_TTL", 180)
OPT_REPLAY_MAX_TEMP = _env_float("SIPHON_OPT_REPLAY_MAX_TEMP", 0.1)

_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")


def effort_rank(effort: str | None) -> int:
    try:
        return _EFFORT_ORDER.index((effort or "").lower())
    except ValueError:
        return -1


def price_for(model: str) -> tuple[float, float]:
    """(输入价, 输出价) USD/1M。带 @effort 后缀先剥掉。"""
    base = str(model).split("@", 1)[0].strip()
    p = PRICES.get(base)
    return tuple(p) if p else tuple(DEFAULT_PRICE)  # type: ignore[return-value]


def counterfactual_price() -> tuple[float, float]:
    return price_for(COUNTERFACTUAL_MODEL)
