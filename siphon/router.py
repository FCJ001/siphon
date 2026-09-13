"""路由: 会话粘性优先 → 主力 → 在途最少(并列看剩余额度/权重)。"""
from __future__ import annotations

from . import accounts, config


class NoRoute(Exception):
    """无可用 (账号, 模型) 路由。"""


def model_allowed(acct, model: str) -> bool:
    base = str(model).split("@", 1)[0]
    return not acct.allowlist or base in acct.allowlist


def candidates(model: str) -> list:
    out = []
    for a in accounts.all_accounts():
        if not a.enabled:
            continue
        st = accounts.breaker_state(a.id)
        if st["account_open_until"] > 0:
            continue
        if any(m["model"] == str(model).split("@", 1)[0] for m in st["models"]):
            continue
        if not model_allowed(a, model):
            continue
        out.append(a)
    return out


def pick(affinity_key: str, model: str) -> object:
    """选账号。粘性 > 主力(在途未满) > 在途最少(并列: 剩余额度多者 / 权重高者)。"""
    cands = candidates(model)
    if not cands:
        raise NoRoute(f"模型 {model} 无可用账号路由(全部熔断/禁用/不在白名单)")

    if affinity_key:
        sticky_aid = accounts.sticky_get(affinity_key)
        if sticky_aid:
            for a in cands:
                if a.id == sticky_aid:
                    return a

    prim = accounts.primary()
    if prim and prim in cands and accounts.inflight(prim.id) < prim.inflight_cap:
        return prim

    return min(cands, key=lambda a: (
        accounts.inflight(a.id),
        accounts.headroom(a.id),   # 已用百分比越小越优先
        -a.weight,
        a.id,
    ))


def bind(affinity_key: str, aid: int) -> None:
    """请求成功后绑定会话粘性。"""
    if affinity_key:
        accounts.sticky_set(affinity_key, aid)
