"""FastAPI 入口: uvicorn siphon.main:app --host 127.0.0.1 --port 4100"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import accounts, admin, db, proxy, quota, upstream

WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    accounts.bootstrap()
    restored = accounts.restore_from_snapshots()
    if restored:
        print(f"[siphon] 已从快照恢复 {restored} 个账号的配额水位")
    poller = asyncio.create_task(quota.poll_loop())
    yield
    poller.cancel()
    await upstream.aclose()


app = FastAPI(title="Siphon", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None)
app.include_router(proxy.api)
app.include_router(admin.api)


@app.get("/health")
async def health():
    from . import router as router_mod
    return {"ok": True, "accounts": len(accounts.all_accounts(refresh=True)),
            "runtime": accounts.snapshot()}


# ---------------- 前端 ----------------
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/login")
    async def login_page():
        return FileResponse(WEB_DIR / "login.html")

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")
