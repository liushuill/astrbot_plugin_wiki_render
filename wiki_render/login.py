"""MediaWiki 登录与 cookie 持久化（对应需求 §10.1）。

登录流程：meta=tokens 取 logintoken -> action=clientlogin。
登录态（cookie jar 序列化）按会话存 plugin_data/login/<session_id>.json（0600）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx

from .api_client import DEFAULT_UA, WikiConnection
from .errors import WikiAPIError, WikiRenderError

LOGIN_DIR_NAME = "login"


def _sanitize(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", session_id or "")
    if safe:
        return safe[:120]
    return hashlib.md5(str(session_id).encode()).hexdigest()[:16]


def _serialize_cookies(client: httpx.AsyncClient) -> list[dict]:
    out = []
    for c in client.cookies.jar:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "expires": c.expires if isinstance(c.expires, (int, float)) else None,
            }
        )
    return out


class LoginManager:
    """登录态存取：按会话（群/私聊 unified_msg_origin）。"""

    def __init__(self, base_dir: Path) -> None:
        self.dir = base_dir / LOGIN_DIR_NAME
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{_sanitize(session_id)}.json"

    def load(self, session_id: str) -> Optional[dict]:
        p = self._path(session_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if data.get("expires_at") and data["expires_at"] < time.time():
            return None
        return data

    def save(self, session_id: str, data: dict) -> None:
        p = self._path(session_id)
        try:
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            p.chmod(0o600)
        except OSError:
            pass

    def remove(self, session_id: str) -> bool:
        p = self._path(session_id)
        if p.exists():
            try:
                p.unlink()
                return True
            except OSError:
                return False
        return False

    def is_logged_in(self, session_id: str) -> bool:
        return self.load(session_id) is not None

    def info(self, session_id: str) -> Optional[dict]:
        data = self.load(session_id)
        if not data:
            return None
        return {
            "username": data.get("username", ""),
            "logged_in_at": data.get("logged_in_at"),
        }

    def cookies_for_playwright(self, session_id: str) -> list[dict]:
        """返回可交给 context.add_cookies() 的 cookie 列表。"""
        data = self.load(session_id)
        if not data:
            return []
        return [c for c in data.get("cookies", []) if c.get("name") and c.get("value")]

    def cookie_header(self, session_id: str) -> Optional[str]:
        data = self.load(session_id)
        if not data:
            return None
        cks = data.get("cookies", [])
        if not cks:
            return None
        return "; ".join(f"{c['name']}={c['value']}" for c in cks if c.get("name"))


async def wiki_login(
    conn: WikiConnection,
    username: str,
    password: str,
    *,
    ua: str = DEFAULT_UA,
    timeout: float = 15.0,
) -> dict:
    """执行 MediaWiki clientlogin，成功返回可持久化 dict，失败抛 WikiAPIError。"""
    if not username or not password:
        raise WikiAPIError("用户名或密码为空")
    headers = {"User-Agent": ua, **(conn.headers or {})}
    async with httpx.AsyncClient(
        verify=conn.verify_tls,
        proxy=conn.proxy,
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        # 1. 登录令牌
        try:
            r = await client.get(
                conn.api_url,
                params={
                    "action": "query",
                    "meta": "tokens",
                    "type": "login",
                    "format": "json",
                    "formatversion": "2",
                },
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise WikiAPIError(f"获取登录令牌失败: {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise WikiAPIError("登录令牌响应不是合法 JSON") from e

        token = ((data.get("query") or {}).get("tokens") or {}).get("logintoken")
        if not token:
            raise WikiAPIError("无法获取登录令牌（该 wiki 可能禁用了 API 登录）")

        # 2. clientlogin
        try:
            r = await client.post(
                conn.api_url,
                data={
                    "action": "clientlogin",
                    "format": "json",
                    "formatversion": "2",
                    "logintoken": token,
                    "username": username,
                    "password": password,
                    "loginreturnurl": conn.server or "https://example.org/",
                },
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise WikiAPIError(f"登录请求失败: {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise WikiAPIError("登录响应不是合法 JSON") from e

        login = data.get("clientlogin") or data.get("login") or {}
        status = login.get("status")
        if status == "PASS":
            return {
                "cookies": _serialize_cookies(client),
                "username": username,
                "logged_in_at": time.time(),
            }

        msg = login.get("message")
        if isinstance(msg, dict):
            msg = msg.get("text") or msg.get("html") or str(msg)
        reason = login.get("reason") or ""
        raise WikiAPIError(f"登录失败（{status}）：{msg or '未知原因'} {reason}".strip())


__all__ = ["LoginManager", "wiki_login"]
