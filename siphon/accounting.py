"""记账: 每请求的 token(含 cached_tokens) + 节省四段拆分, 保证瀑布图能对上总账。

口径(siphon design.md §2 / frontend.md §2.2):
  基线 b   = counterfactual(中档模型)全价: prompt×base_in + completion×base_out
  缓存节省 c = cached × m_in × (1−ratio)
  免档节省 f = prompt×(base_in−m_in) + completion×(base_out−m_out)
  规范节省 t = saved_tokens × m_in
  回放节省 r = b(整笔请求未发出)
  实际 a   = b − f − c − t − r  ==  上游真实消耗(未命中输入全价 + 命中 0.1x + 输出全价)
"""
from __future__ import annotations

import time

from . import config, db


def _month(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts or time.time()))


def _range_since(range_name: str) -> float:
    now = time.time()
    return {"today": now - _seconds_until_midnight(),
            "24h": now - 86400,
            "7d": now - 7 * 86400,
            "30d": now - 30 * 86400}.get(range_name, now - 86400)


def _seconds_until_midnight() -> int:
    lt = time.localtime()
    return 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def record(*, ts: float, account_id: int | None, affinity_key: str,
           requested_model: str, model: str, effort_in: str | None,
           effort_out: str | None, prompt_tokens: int, cached_tokens: int,
           completion_tokens: int, saved_tokens: int, tool_rounds: int,
           latency_ms: int, status: str, retried: int, replay_hit: bool,
           error: str = "") -> dict:
    base_in, base_out = config.counterfactual_price()
    m_in, m_out = config.price_for(model)
    ratio = config.CACHED_PRICE_RATIO

    # 注意: prompt_tokens 是"规范化之后实际发出的"输入量(上游返回口径),
    # saved_tokens 是优化器少发的部分, 两者相加 = 客户端原本要发的量。
    # 价格为 USD/1M tokens, 统一除以 1e6。
    M = 1_000_000.0
    full_prompt = prompt_tokens + saved_tokens

    baseline = full_prompt / M * base_in + completion_tokens / M * base_out
    if replay_hit:
        # 回放行: 上游零消耗。整笔基线全部归入"回放"段, 其余段必须为 0,
        # 否则与原始请求的缓存/免费段重复计, 瀑布图对不上总账。
        save_free = save_cache = save_transform = 0.0
        save_replay = baseline
        actual = 0.0
    else:
        save_free = (full_prompt / M * (base_in - m_in)
                     + completion_tokens / M * (base_out - m_out))
        save_cache = cached_tokens / M * m_in * (1 - ratio)
        save_transform = saved_tokens / M * m_in
        save_replay = 0.0
        actual = max(0.0, baseline - save_free - save_cache - save_transform - save_replay)

    rid = db.execute(
        "INSERT INTO requests(ts,account_id,affinity_key,requested_model,model,"
        "effort_in,effort_out,prompt_tokens,cached_tokens,completion_tokens,"
        "saved_tokens,tool_rounds,latency_ms,status,retried,replay_hit,"
        "cost_baseline,cost_actual,save_cache,save_free,save_transform,save_replay,error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, account_id, affinity_key, requested_model, model,
         effort_in, effort_out, prompt_tokens, cached_tokens, completion_tokens,
         saved_tokens, tool_rounds, latency_ms, status, retried, int(replay_hit),
         round(baseline, 6), round(actual, 6), round(save_cache, 6),
         round(save_free, 6), round(save_transform, 6), round(save_replay, 6),
         error[:300]))

    if account_id and status == "ok":
        db.execute(
            "INSERT INTO model_usage(account_id,model,month,prompt_tokens,cached_tokens,"
            "completion_tokens,cost_usd) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,model,month) DO UPDATE SET "
            "prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
            "cached_tokens=cached_tokens+excluded.cached_tokens, "
            "completion_tokens=completion_tokens+excluded.completion_tokens, "
            "cost_usd=cost_usd+excluded.cost_usd",
            (account_id, str(model).split("@", 1)[0], _month(ts),
             prompt_tokens, cached_tokens, completion_tokens, round(actual, 6)))
    return {"id": rid, "cost_baseline": baseline, "cost_actual": actual,
            "save_cache": save_cache, "save_free": save_free,
            "save_transform": save_transform, "save_replay": save_replay}


# ---------------- 查询 ----------------

def _sum_row(where: str = "1=1", params: tuple = ()) -> dict:
    # 成本/节省: 含回放行(它们贡献 save_replay 段);
    # token 三口径: 不含回放行(上游零消耗, 计入会虚增缓存命中率)
    cost = db.query_one(
        f"SELECT COUNT(*) AS requests,"
        f" COALESCE(SUM(cost_baseline),0) AS cost_baseline,"
        f" COALESCE(SUM(cost_actual),0) AS cost_actual,"
        f" COALESCE(SUM(save_cache),0) AS save_cache,"
        f" COALESCE(SUM(save_free),0) AS save_free,"
        f" COALESCE(SUM(save_transform),0) AS save_transform,"
        f" COALESCE(SUM(save_replay),0) AS save_replay,"
        f" COALESCE(SUM(replay_hit),0) AS replays,"
        f" COALESCE(SUM(status<>'ok'),0) AS failures"
        f" FROM requests WHERE {where}", params) or {}
    tok = db.query_one(
        f"SELECT COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        f" COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
        f" COALESCE(SUM(completion_tokens),0) AS completion_tokens"
        f" FROM requests WHERE ({where}) AND replay_hit=0", params) or {}
    row = {**cost, **tok}
    prompt = row.get("prompt_tokens") or 0
    cached = row.get("cached_tokens") or 0
    row["cache_hit_rate"] = round(cached / prompt * 100, 2) if prompt else 0.0
    row["failure_rate"] = round((row.get("failures") or 0) /
                                row["requests"] * 100, 2) if row.get("requests") else 0.0
    for k in ("cost_baseline", "cost_actual", "save_cache", "save_free",
              "save_transform", "save_replay"):
        row[k] = round(row.get(k) or 0, 4)
    return row


def overview(range_name: str = "today") -> dict:
    since = _range_since(range_name)
    totals = _sum_row("ts>=?", (since,))
    totals["range"] = range_name
    totals["savings_total"] = round(totals["cost_baseline"] - totals["cost_actual"], 4)
    totals["savings_pct"] = (round(totals["savings_total"] / totals["cost_baseline"] * 100, 1)
                             if totals["cost_baseline"] else 0.0)
    by_model = db.query(
        "SELECT model, COUNT(*) AS requests, SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(cached_tokens) AS cached_tokens, SUM(completion_tokens) AS completion_tokens,"
        " SUM(cost_actual) AS cost_actual"
        " FROM requests WHERE ts>=? GROUP BY model ORDER BY cost_actual DESC",
        (since,))
    return {"totals": totals, "by_model": by_model}


def timeseries(metric: str = "tokens", granularity: str = "hour",
               group_by: str = "none", since: float | None = None) -> list[dict]:
    since = since or _range_since("7d")
    fmt = "%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d"
    if metric == "cost":
        cols = "SUM(cost_actual) AS value"
    elif metric == "requests":
        cols = "COUNT(*) AS value"
    else:  # tokens: 拆三段
        cols = ("SUM(prompt_tokens - cached_tokens) AS input_uncached,"
                " SUM(cached_tokens) AS input_cached,"
                " SUM(completion_tokens) AS output")
    dim = {"model": "model", "account_id": "account_id", "key": "affinity_key"}.get(group_by)
    sel = f"strftime('{fmt}', ts, 'unixepoch', 'localtime') AS bucket"
    sel += f", {dim}" if dim else ""
    group = f"GROUP BY bucket{', ' + dim if dim else ''} ORDER BY bucket"
    return db.query(f"SELECT {sel}, {cols} FROM requests WHERE ts>=? {group}", (since,))


def savings_attribution(range_name: str = "today") -> dict:
    """瀑布图数据: 基线 → 各优化器 → 实际, 各段可加且和为实际。"""
    t = _sum_row("ts>=?", (_range_since(range_name),))
    base = t["cost_baseline"]
    parts = [
        {"key": "cache", "label": "上游前缀缓存", "amount": t["save_cache"]},
        {"key": "free", "label": "免费档路由", "amount": t["save_free"]},
        {"key": "transform", "label": "工具结果规范化", "amount": t["save_transform"]},
        {"key": "replay", "label": "精确响应回放", "amount": t["save_replay"]},
    ]
    return {"baseline": base, "actual": t["cost_actual"], "parts": parts,
            "savings": round(base - t["cost_actual"], 4),
            "savings_pct": round((base - t["cost_actual"]) / base * 100, 1) if base else 0.0,
            "methodology": ("effort 分轮控制的节省依赖反事实(不知满档会输出多少), 暂不计量; "
                            "各段按 design.md §2 口径可加, sum(parts)+actual=baseline")}


def logs(account_id: int | None = None, model: str | None = None,
         status: str | None = None, replay: bool | None = None,
         cached: bool | None = None, since: float | None = None,
         page: int = 1, page_size: int = 50) -> dict:
    where, params = ["1=1"], []
    if account_id:
        where.append("account_id=?"); params.append(account_id)
    if model:
        where.append("model LIKE ?"); params.append(f"%{model}%")
    if status:
        where.append("status=?"); params.append(status)
    if replay is not None:
        where.append("replay_hit=?"); params.append(int(replay))
    if cached is not None:
        where.append("cached_tokens " + ("> 0" if cached else "= 0"))
    if since:
        where.append("ts>=?"); params.append(since)
    cond = " AND ".join(where)
    total = (db.query_one(f"SELECT COUNT(*) AS n FROM requests WHERE {cond}", params)
             or {}).get("n", 0)
    page = max(1, page)
    rows = db.query(
        f"SELECT * FROM requests WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size])
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


def model_usage_all() -> list[dict]:
    month = _month()
    rows = db.query(
        "SELECT u.account_id, u.model, u.prompt_tokens, u.cached_tokens,"
        " u.completion_tokens, u.cost_usd, a.alias, a.key_tail"
        " FROM model_usage u LEFT JOIN accounts a ON a.id=u.account_id"
        " WHERE u.month=? ORDER BY u.cost_usd DESC", (month,))
    cap_default = config.MODEL_CAPS_USD
    for r in rows:
        r["cap_usd"] = cap_default.get(r["model"])
        # 2 位小数: 月初消耗很小, 1 位小数会把 0.47% 抹成 0.5%, 无法对账
        r["cap_pct"] = (round(r["cost_usd"] / r["cap_usd"] * 100, 2)
                        if r.get("cap_usd") else None)
    return rows
