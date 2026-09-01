"""会话（群/私聊）级 wiki 绑定存储：基于 AstrBot KV（插件维度）。

数据结构（单个 KV key）：
{
  "bindings": {
    "<session_id>": {
      "conn": { ...WikiConnection 字段... },
      "interwikis": { "<prefix>": "<api_url>" }
    }
  }
}
"""

from __future__ import annotations

from typing import Any, Optional

from .api_client import WikiConnection

KV_KEY = "astrbot_plugin_wiki_render:v1"


class BindingStore:
    def __init__(self, star: Any) -> None:
        self.star = star

    async def _load(self) -> dict:
        data = await self.star.get_kv_data(KV_KEY, {})
        return data if isinstance(data, dict) else {}

    async def _save(self, data: dict) -> None:
        await self.star.put_kv_data(KV_KEY, data)

    # ---- 绑定 ----
    async def get_conn(self, session_id: str) -> Optional[WikiConnection]:
        data = await self._load()
        rec = data.get("bindings", {}).get(session_id) or {}
        if "conn" not in rec:
            return None
        return WikiConnection.from_dict(rec.get("conn"))

    async def set_conn(self, session_id: str, conn: WikiConnection) -> None:
        data = await self._load()
        rec = data.setdefault("bindings", {}).setdefault(session_id, {})
        rec["conn"] = conn.to_dict()
        await self._save(data)

    async def unset(self, session_id: str) -> bool:
        data = await self._load()
        bindings = data.get("bindings", {})
        if session_id in bindings:
            del bindings[session_id]
            await self._save(data)
            return True
        return False

    # ---- interwiki ----
    async def get_interwikis(self, session_id: str) -> dict:
        data = await self._load()
        rec = data.get("bindings", {}).get(session_id) or {}
        return dict(rec.get("interwikis") or {})

    async def set_interwiki(self, session_id: str, prefix: str, api_url: str) -> None:
        data = await self._load()
        rec = data.setdefault("bindings", {}).setdefault(session_id, {})
        iw = rec.setdefault("interwikis", {})
        iw[prefix] = api_url
        await self._save(data)

    async def remove_interwiki(self, session_id: str, prefix: str) -> bool:
        data = await self._load()
        rec = data.get("bindings", {}).get(session_id) or {}
        iw = rec.get("interwikis") or {}
        if prefix in iw:
            del iw[prefix]
            await self._save(data)
            return True
        return False

    # ---- 截图方向（横屏/竖屏） ----
    async def get_screen(self, session_id: str, default: str = "landscape") -> str:
        data = await self._load()
        mode = (data.get("screens") or {}).get(session_id)
        return mode if mode in ("landscape", "portrait") else default

    async def set_screen(self, session_id: str, mode: str) -> None:
        if mode not in ("landscape", "portrait"):
            return
        data = await self._load()
        data.setdefault("screens", {})[session_id] = mode
        await self._save(data)

    # ---- 遍历绑定（供插件页面展示） ----
    async def all_bindings(self) -> dict:
        data = await self._load()
        out = {}
        for sid, rec in (data.get("bindings") or {}).items():
            conn = rec.get("conn") or {}
            out[sid] = {
                "site_name": conn.get("site_name", ""),
                "api_url": conn.get("api_url", ""),
                "screen": (data.get("screens") or {}).get(sid, "landscape"),
            }
        return out

    # ---- 数组类设置（插件页面管理；AstrBot 配置界面无法友好编辑长 JSON） ----
    async def get_array_setting(self, key: str, default=None):
        data = await self._load()
        return (data.get("array_settings") or {}).get(key, default)

    async def set_array_setting(self, key: str, value: list) -> None:
        data = await self._load()
        data.setdefault("array_settings", {})[key] = list(value or [])
        await self._save(data)
