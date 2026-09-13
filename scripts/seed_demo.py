"""给一次性演示实例灌演示数据(截图自评用, 不碰真实 data/siphon.db)。

用法: SIPHON_DB=/tmp/siphon-demo.db python scripts/seed_demo.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SIPHON_DB", "/tmp/siphon-demo.db")

from siphon import accounts, accounting, config, db, quota  # noqa: E402

random.seed(20260913)


def main() -> None:
    db.connect()
    a1 = accounts.create(key="sk-demo-aaaa-bbbb", alias="主力", primary=True, weight=100)
    a2 = accounts.create(key="sk-demo-cccc-dddd", alias="备用", weight=60)

    # 三窗水位: 主力健康, 备用周窗逼近警戒(演示 warn/danger 配色)
    wins1 = {"rolling": {"percent": 12, "resetsAt": _iso(hours=2)},
             "weekly": {"percent": 34, "resetsAt": _iso(days=2)},
             "monthly": {"percent": 21, "resetsAt": _iso(days=17)}}
    wins2 = {"rolling": {"percent": 46, "resetsAt": _iso(hours=4)},
             "weekly": {"percent": 87, "resetsAt": _iso(days=3)},
             "monthly": {"percent": 62, "resetsAt": _iso(days=6)}}
    for aid, wins in ((a1.id, wins1), (a2.id, wins2)):
        worst = max(w["percent"] for w in wins.values())
        accounts.headroom_set(aid, worst,
                              {k: v["resetsAt"] for k, v in wins.items()}, windows=wins)
        db.execute(
            "INSERT INTO quota_snapshots(ts,account_id,rolling,weekly,monthly,resets) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), aid, wins["rolling"]["percent"], wins["weekly"]["percent"],
             wins["monthly"]["percent"],
             json.dumps({k: v["resetsAt"] for k, v in wins.items()})))

    models = ["deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-v4-flash", "mimo-v2.5"]
    now = time.time()
    for h in range(47, -1, -1):                      # 近 48h
        for _ in range(random.randint(4, 11)):
            ts = now - h * 3600 - random.randint(0, 3500)
            model = random.choice(models)
            prompt = random.randint(3_000, 9_000)
            cached = int(prompt * random.uniform(0.82, 0.96))
            completion = random.randint(180, 1_600)
            saved = random.choice([0, 0, 0, 350, 720])
            replay = random.random() < 0.08
            accounting.record(
                ts=ts, account_id=random.choice([a1.id, a1.id, a2.id]),
                affinity_key="demo", requested_model=model, model=model,
                effort_in="max", effort_out=random.choice(["low", "medium"]),
                prompt_tokens=0 if replay else prompt, cached_tokens=0 if replay else cached,
                completion_tokens=0 if replay else completion, saved_tokens=saved,
                tool_rounds=random.randint(0, 8),
                latency_ms=random.randint(600, 9_000), status="ok",
                retried=random.choice([0, 0, 0, 1]), replay_hit=replay)
    db.log_event("seed.demo", {"rows": db.query_one("SELECT COUNT(*) n FROM requests")["n"]})
    print("演示数据已灌入:", os.environ["SIPHON_DB"])


def _iso(**kw) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() +
                         kw.get("hours", 0) * 3600 + kw.get("days", 0) * 86400))


if __name__ == "__main__":
    main()
