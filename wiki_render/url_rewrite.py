"""parse API 输出的文章 HTML 重写：相对链接 -> 绝对链接。

处理对象（对应需求.md §4.4 管线 A 第 2 步）：
- href/src 的相对路径（/wiki/xxx、/w/images/xxx）
- 协议相对 URL（//upload.wikimedia.org/...）
- srcset 中的多候选
- data-src（部分皮肤懒加载占位）
- 移除 <script>（安全）
"""

from __future__ import annotations

import re
from typing import Optional

from .api_client import WikiConnection

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_ATTR_RE = re.compile(
    r'(?i)(\b(?:href|src|data-src)\s*=\s*["\'])([^"\']*)(["\'])'
)
_SRCSET_RE = re.compile(r'(?i)(\bsrcset\s*=\s*["\'])([^"\']*)(["\'])')
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def abs_url(url: str, server: str, scheme: str) -> str:
    """把单个 URL 绝对化；已绝对的/锚点/协议 URL 原样返回。"""
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("//"):
        return f"{scheme}:{url}"
    if url.startswith("/"):
        return f"{server}{url}"
    if url.startswith("#"):
        return url
    if _SCHEME_RE.match(url):  # 已经是绝对 URL（http/https/mailto/data/...）
        return url
    # 罕见的非根相对路径，按 server 根拼
    return f"{server}/{url}"


def _fix_srcset(srcset: str, server: str, scheme: str) -> str:
    out = []
    for item in srcset.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split()
        u = parts[0]
        rest = " " + " ".join(parts[1:]) if len(parts) > 1 else ""
        out.append(abs_url(u, server, scheme) + rest)
    return ", ".join(out)


def rewrite_article_html(
    html: str,
    conn: WikiConnection,
    server: Optional[str] = None,
    scheme: Optional[str] = None,
) -> str:
    """重写 parse 输出的 HTML。server/scheme 缺省取连接画像值。"""
    srv = server or conn.server or ""
    sch = scheme or conn.scheme or "https"

    html = _SCRIPT_RE.sub("", html)

    def _attr_sub(m: re.Match) -> str:
        return m.group(1) + abs_url(m.group(2), srv, sch) + m.group(3)

    def _srcset_sub(m: re.Match) -> str:
        return m.group(1) + _fix_srcset(m.group(2), srv, sch) + m.group(3)

    html = _ATTR_RE.sub(_attr_sub, html)
    html = _SRCSET_RE.sub(_srcset_sub, html)
    return html
