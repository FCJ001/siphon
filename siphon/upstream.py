"""上游 HTTP: 每账号独立的必需头(session), 共享连接池。"""
from __future__ import annotations

import httpx

from . import config

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=config.UPSTREAM_TIMEOUT)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def headers_for(key: str, session_id: str) -> dict:
    """Go 网关必需头: Bearer + x-opencode-session(缺失直接 400)。"""
    return {
        "Authorization": f"Bearer {key}",
        "x-opencode-session": session_id,
        "Content-Type": "application/json",
        "User-Agent": config.USER_AGENT,
    }


def chat_url() -> str:
    return f"{config.OPENCODE_BASE_URL.rstrip('/')}/chat/completions"
