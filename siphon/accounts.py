"""账号池: 账号 CRUD、两级熔断、会话粘性、在途计数。

内存态(熔断/在途/粘性/剩余额度)是进程级的 —— 单实例部署够用;
多实例时把这几个 dict 换成 Redis 即可(接口不变)。
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from . import config, db


@dataclass
class Account:
    id: int
    key: str
    key_tail: str
    alias: str
    note: str
    primary: bool
    weight: int
    enabled: bool
    inflight_cap: int
    session_id: str
    allowlist: list = field(default_factory=list)

    def to_public(self, with_key: bool = False) -> dict:
        d = {
            "id": self.id, "key_tail": self.key_tail, "alias": self.alias,
            "note": self.note, "primary": self.primary, "weight": self.weight,
            "enabled": bool(self.enabled), "inflight_cap": self.inflight_cap,
            "session_tail": self.session_id[-4:], "allowlist": self.allowlist,
        }
        if with_key:
            d["key"] = self.key
        return d


# ---------------- 内存态 ----------------
_mu = threading.Lock()
_inflight: dict[int, int] = {}
_headroom: dict[int, float] = {}                 # 账号 → 最紧窗口已用百分比
_resets: dict[int, dict] = {}                    # 账号 → {rolling|weekly|monthly: resetsAt}
_model_down: dict[tuple[int, str], float] = {}   # (账号, 模型) → 解除时刻
_account_open: dict[int, float] = {}             # 账号 → 解除时刻
_sticky: dict[str, tuple[int, float]] = {}       # 亲和键 → (账号, 过期时刻)
_last_quota_error: float = 0.0


def _now() -> float:
    return time.time()


def new_session_id() -> str:
    return secrets.token_hex(16)


# ---------------- CRUD ----------------

def _row_to_account(row: dict) -> Account:
    import json as _json
    return Account(
        id=row["id"], key=row["key"], key_tail=row["key_tail"],
        alias=row["alias"], note=row["note"], primary=bool(row["primary_flag"]),
        weight=row["weight"], enabled=bool(row["enabled"]),
        inflight_cap=row["inflight_cap"], session_id=row["session_id"],
        allowlist=_json.loads(row["allowlist"] or "[]"),
    )


def bootstrap() -> None:
    """账号表为空且配了 OPENCODE_KEYS 时引导建号。"""
    rows = db.query("SELECT id FROM accounts")
    if rows or not config.BOOTSTRAP_KEYS:
        return
    for i, key in enumerate(config.BOOTSTRAP_KEYS):
        create(key=key, alias=f"账号{i + 1}", primary=(i == 0))
    db.log_event("accounts.bootstrap", {"count": len(config.BOOTSTRAP_KEYS)})


def create(key: str, alias: str = "", note: str = "", primary: bool = False,
           weight: int = 100, enabled: bool = True, inflight_cap: int | None = None,
           allowlist: list | None = None, session_id: str | None = None) -> Account:
    import json as _json
    tail = key[-4:] if len(key) >= 4 else key
    aid = db.execute(
        "INSERT INTO accounts(key,key_tail,alias,note,primary_flag,weight,enabled,"
        "inflight_cap,session_id,allowlist,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (key, tail, alias, note, int(primary), weight, int(enabled),
         inflight_cap or config.PRIMARY_INFLIGHT_CAP,
         session_id or new_session_id(), _json.dumps(allowlist or []), 
         time.strftime("%Y-%m-%d %H:%M:%S")))
    if primary:
        demote_others(aid)
    db.log_audit("account.create", target=f"account:{aid}", after={"alias": alias, "key_tail": tail})
    invalidate()
    return get(aid)  # type: ignore[return-value]


def demote_others(keep_id: int) -> None:
    db.execute("UPDATE accounts SET primary_flag=0 WHERE id<>?", (keep_id,))


def update(aid: int, **fields) -> None:
    import json as _json
    before = get(aid)
    if not before:
        return
    col_map = {"alias": "alias", "note": "note", "primary": "primary_flag",
               "weight": "weight", "enabled": "enabled", "inflight_cap": "inflight_cap",
               "allowlist": "allowlist", "session_id": "session_id", "key": "key"}
    sets, params = [], []
    for name, val in fields.items():
        col = col_map.get(name)
        if not col:
            continue
        if col == "allowlist":
            val = _json.dumps(val or [])
        elif col == "primary":
            val = int(bool(val))
        elif col == "enabled":
            val = int(bool(val))
        sets.append(f"{col}=?")
        params.append(val)
    if not sets:
        return
    params.append(aid)
    db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", params)
    if fields.get("primary"):
        demote_others(aid)
    after = get(aid)
    db.log_audit("account.update", target=f"account:{aid}",
                 before=before.to_public(), after=after.to_public() if after else {})
    invalidate()


def delete(aid: int) -> None:
    acct = get(aid)
    db.execute("DELETE FROM accounts WHERE id=?", (aid,))
    db.log_audit("account.delete", target=f"account:{aid}",
                 before=acct.to_public() if acct else {})
    invalidate()


def invalidate() -> None:
    global _cache
    with _mu:
        _cache = None     # 置 None 而非 clear(): 下次读取强制回库, 否则永远拿到失效空表


_cache: list[Account] | None = None


def all_accounts(refresh: bool = False) -> list[Account]:
    global _cache
    if _cache is None or refresh:
        _cache = [_row_to_account(r) for r in db.query("SELECT * FROM accounts ORDER BY id")]
    return _cache


def get(aid: int) -> Account | None:
    for a in all_accounts():
        if a.id == aid:
            return a
    return None


def primary() -> Account | None:
    for a in all_accounts():
        if a.primary and a.enabled:
            return a
    enabled = [a for a in all_accounts() if a.enabled]
    return enabled[0] if enabled else None


# ---------------- 在途 ----------------

def inflight_inc(aid: int) -> None:
    with _mu:
        _inflight[aid] = _inflight.get(aid, 0) + 1


def inflight_dec(aid: int) -> None:
    with _mu:
        _inflight[aid] = max(0, _inflight.get(aid, 1) - 1)


def inflight(aid: int) -> int:
    with _mu:
        return _inflight.get(aid, 0)


# ---------------- 熔断(两级) ----------------

def mark_model_down(aid: int, model: str, ttl: float | None = None) -> None:
    """模型级: 同账号单模型 429(常见为该模型月度上限)。"""
    with _mu:
        already = any(k[0] == aid and k[1] != model and v > _now()
                      for k, v in _model_down.items())
        _model_down[(aid, str(model).split("@", 1)[0])] = _now() + (
            ttl if ttl is not None else config.MODEL_DOWN_TTL)
        # 同账号第二个模型也熔断 → 说明是账号级(5h/周/总额度), 升级
        if already:
            _account_open[aid] = max(_account_open.get(aid, 0), _now() + config.ACCOUNT_DOWN_TTL)
    db.log_event("breaker.model_down", {"account_id": aid, "model": model})


def mark_account_open(aid: int, until: float | None = None) -> None:
    with _mu:
        _account_open[aid] = max(_account_open.get(aid, 0),
                                 until if until is not None else _now() + config.ACCOUNT_DOWN_TTL)
    db.log_event("breaker.account_open", {"account_id": aid,
                                          "until": _account_open[aid]})


def clear_breaker(aid: int) -> None:
    with _mu:
        for k in [k for k in _model_down if k[0] == aid]:
            _model_down.pop(k, None)
        _account_open.pop(aid, None)
    db.log_event("breaker.reset", {"account_id": aid})


def breaker_state(aid: int) -> dict:
    with _mu:
        now = _now()
        wall = time.time()
        models = []
        for (a, m), t in _model_down.items():
            if a == aid and t > now:
                models.append({"model": m, "until": t, "until_iso": _iso(wall + (t - now))})
        acct = _account_open.get(aid, 0)
        return {"account_open_until": acct if acct > now else 0,
                "account_open_until_iso": _iso(wall + (acct - now)) if acct > now else 0,
                "models": models}


def _iso(epoch: float) -> str:
    """熔断内部用 monotonic, 展示给前端时换算成墙钟 ISO(否则倒计时是错的)。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def headroom_set(aid: int, used_pct: float, resets: dict) -> None:
    with _mu:
        _headroom[aid] = float(used_pct)
        _resets[aid] = resets


def headroom(aid: int) -> float:
    with _mu:
        return _headroom.get(aid, 0.0)


def resets(aid: int) -> dict:
    with _mu:
        return _resets.get(aid, {})


def snapshot() -> dict:
    with _mu:
        now = _now()
        return {
            "inflight": dict(_inflight),
            "headroom": dict(_headroom),
            "model_down": {f"{a}:{m}": t for (a, m), t in _model_down.items() if t > now},
            "account_open": {a: t for a, t in _account_open.items() if t > now},
            "sticky": len(_sticky),
        }


# ---------------- 会话粘性 ----------------

def sticky_get(affinity_key: str) -> int | None:
    with _mu:
        item = _sticky.get(affinity_key)
        if not item:
            return None
        aid, exp = item
        if exp < _now():
            _sticky.pop(affinity_key, None)
            return None
        return aid


def sticky_set(affinity_key: str, aid: int) -> None:
    with _mu:
        # 简单防膨胀: 超过 1 万条时把过期的清掉
        if len(_sticky) > 10_000:
            now = _now()
            for k in [k for k, (_, exp) in _sticky.items() if exp < now]:
                _sticky.pop(k, None)
        _sticky[affinity_key] = (aid, _now() + config.AFFINITY_TTL)
