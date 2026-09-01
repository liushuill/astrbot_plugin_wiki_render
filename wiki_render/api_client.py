"""MediaWiki Action API 客户端：端点解析、连接适配、常用查询封装。

设计要点（对应需求.md §4.2）：
- 通用所有 MediaWiki：只要是 Action API（api.php）可达的站点即可用。
- 多部署场景连接适配：本地 http / 云端 https / 私域（代理、Basic 认证、自签证书、
  自定义头）/ 老旧版本（formatversion 自动降级）。
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

from .errors import (
    InvalidTitleError,
    PageNotFoundError,
    WikiAPIError,
    WikiUnavailableError,
)

DEFAULT_UA = "astrbot_plugin_wiki_render/0.1 (AstrBot plugin; MediaWiki viewer)"

# 端点探测候选（相对 scriptpath 的常见位置）
_ENDPOINT_CANDIDATES = ("/w/api.php", "/api.php", "/mediawiki/api.php")

# 用正则把版本号从 "MediaWiki 1.46.0" 中取出
_MW_VERSION_RE = re.compile(r"MediaWiki\s+(\d+)\.(\d+)")


@dataclass
class WikiConnection:
    """一个 MediaWiki 站点的连接画像。"""

    api_url: str
    server: str = ""
    article_path: str = "/wiki/$1"
    scriptpath: str = "/w"
    site_name: str = ""
    lang: str = ""
    mw_version: str = ""
    scheme: str = "https"
    verify_tls: bool = True
    proxy: Optional[str] = None
    headers: dict = field(default_factory=dict)
    auth_user: Optional[str] = None
    auth_pass: Optional[str] = None
    timeout: float = 15.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "WikiConnection":
        d = dict(d or {})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def version_tuple(self) -> Optional[tuple[int, int]]:
        m = _MW_VERSION_RE.search(self.mw_version or "")
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None

    def supports_formatversion2(self) -> bool:
        """formatversion=2 自 MediaWiki 1.25 起支持。"""
        v = self.version_tuple
        if v is None:
            return True  # 未知版本先尝试 2，失败再降级（见 WikiClient）
        return v >= (1, 25)

    def article_url(self, title: str) -> str:
        """根据 article_path 生成页面绝对 URL。

        编码遵循 MediaWiki Help:URL 规范：
        - 非 ASCII（含中文）先转 UTF-8 再百分号编码；
        - 空格用下划线表示；
        - 保留 URL 允许字符 : _ / ~ % - + ! ( ) @ 与命名空间冒号/括号/百分号，
          转义 & # ? 等改变 URL 语义的字符。
        """
        quoted = urllib.parse.quote(title.replace(" ", "_"), safe=":()%~!@+")
        return self.server + self.article_path.replace("$1", quoted)

    def history_url(self, title: str) -> str:
        """页面更改记录（历史）URL：<scriptpath>/index.php?title=X&action=history。"""
        quoted = urllib.parse.quote(title.replace(" ", "_"), safe=":()%~!@+")
        return f"{self.server}{self.scriptpath}/index.php?title={quoted}&action=history"

    def mask_sensitive(self) -> dict:
        d = self.to_dict()
        if d.get("auth_pass"):
            d["auth_pass"] = "******"
        return d


@dataclass
class PageState:
    """标题查询（query&titles）后的页面状态。"""

    title: str
    before_title: str = ""
    pageid: Optional[int] = None
    missing: bool = False
    invalid: bool = False
    redirected: bool = False
    redirect_target: str = ""
    normalized: bool = False
    normalized_from: str = ""


@dataclass
class ParsedPage:
    """parse API 的返回结果。"""

    title: str
    pageid: Optional[int]
    html: str
    sections: list[dict] = field(default_factory=list)
    wikitext: str = ""
    langlinks: list = field(default_factory=list)
    is_disambig: bool = False
    redirect_from: str = ""


def _is_http_error_related_to_tls(exc: Exception) -> bool:
    """判断异常是否与 TLS/证书/连接有关，用于 https->http 回退。"""
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "ssl",
            "certificate",
            "cert",
            "tls",
            "handshake",
            "connection reset",
            "connect error",
            "connection refused",
            "connect timeout",
        )
    )


async def _probe_siteinfo(
    api_url: str,
    *,
    verify_tls: bool,
    proxy: Optional[str],
    ua: str,
    timeout: float,
    headers: Optional[dict] = None,
) -> dict:
    """探测一个 api.php 端点，成功返回 siteinfo 的 general 字段。"""
    params = {
        "action": "query",
        "meta": "siteinfo",
        "siprop": "general",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(
            verify=verify_tls,
            proxy=proxy,
            timeout=timeout,
            headers={"User-Agent": ua, **(headers or {})},
            follow_redirects=True,
        ) as client:
            r = await client.get(api_url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise WikiUnavailableError(f"{api_url} 请求失败: {type(e).__name__}: {e}") from e
    except ValueError as e:
        raise WikiUnavailableError(f"{api_url} 响应不是合法 JSON（可能不是 MediaWiki）") from e

    general = (data.get("query") or {}).get("general")
    if not general:
        err = data.get("error")
        if err:
            raise WikiUnavailableError(
                f"{api_url} 返回错误: {err.get('code', err)} {err.get('info', '')}".strip()
            )
        raise WikiUnavailableError(f"{api_url} 不是 MediaWiki 站点")
    return general


async def resolve_endpoint(
    input_url: str,
    *,
    ua: str = DEFAULT_UA,
    verify_tls: bool = True,
    proxy: Optional[str] = None,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
    deadline: float = 45.0,
) -> WikiConnection:
    """把用户输入的任意形式地址解析为一个可用的 WikiConnection。

    接受：裸主机名 / 首页 / 任意页面 URL / api.php / index.php 地址。
    自动尝试 https -> http 回退、自签证书（verify_tls=False）回退。
    """
    raw = (input_url or "").strip()
    if not raw:
        raise WikiUnavailableError("地址为空")

    # 1. 规范化 scheme
    given_scheme = None
    if "://" in raw:
        given_scheme = raw.split("://", 1)[0].lower()
    else:
        raw = "https://" + raw

    parsed = urllib.parse.urlsplit(raw)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    # 2. 生成候选端点
    candidates: list[str] = []
    if "api.php" in path:
        candidates.append(base + path.split("api.php")[0] + "api.php")
    elif "index.php" in path:
        script_dir = path.rsplit("/index.php", 1)[0]
        candidates.append(f"{base}{script_dir}/api.php")
    else:
        for suffix in _ENDPOINT_CANDIDATES:
            candidates.append(f"{base}{path}{suffix}")
    # 去重保序
    seen: set[str] = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    # 3. 依次探测（含 https->http、证书校验回退）
    #    仅在出现 TLS/连接类错误时才追加对应回退轮次，避免无谓超时；
    #    并用总体时限防止多候选逐项超时累积成分钟级卡顿。
    errors: list[str] = []
    last_general: Optional[dict] = None
    last_api: Optional[str] = None
    last_scheme = parsed.scheme
    last_verify = verify_tls
    last_proxy = proxy

    def _try_schemes(scheme: str, v_tls: bool) -> list[tuple[str, bool]]:
        out = []
        for cand in candidates:
            c = cand.replace(f"{base}", f"{scheme}://{parsed.netloc}")
            out.append((c, v_tls))
        return out

    seen_attempts: set[tuple[str, bool]] = set()

    def _merge(rounds: list[list[tuple[str, bool]]]) -> list[tuple[str, bool]]:
        out = []
        for rnd in rounds:
            for item in rnd:
                if item not in seen_attempts:
                    seen_attempts.add(item)
                    out.append(item)
        return out

    attempt_order = _merge(
        [
            _try_schemes(parsed.scheme, verify_tls),
            _try_schemes(parsed.scheme, False),
            _try_schemes("http", verify_tls) if parsed.scheme == "https" else [],
            _try_schemes("http", False) if parsed.scheme == "https" else [],
        ]
    )

    deadline = time.monotonic() + max(deadline, 30.0)
    saw_tls_error = False
    saw_connect_error = False

    for cand, v_tls in attempt_order:
        if time.monotonic() > deadline:
            errors.append("（超过总体探测时限，已停止尝试）")
            break
        try:
            general = await _probe_siteinfo(
                cand,
                verify_tls=v_tls,
                proxy=proxy,
                ua=ua,
                timeout=timeout,
                headers=headers,
            )
            last_general = general
            last_api = cand
            last_scheme = cand.split("://", 1)[0]
            last_verify = v_tls
            break
        except WikiUnavailableError as e:
            if _is_http_error_related_to_tls(e):
                saw_tls_error = True
            if "connect" in str(e).lower() or "refused" in str(e).lower():
                saw_connect_error = True
            errors.append(str(e))

    # 回退轮次仅在首轮出现对应错误时补跑（按 30s 时限约束）
    if not last_general and saw_tls_error:
        for cand, v_tls in _merge([_try_schemes(parsed.scheme, False)]):
            if time.monotonic() > deadline:
                break
            try:
                general = await _probe_siteinfo(
                    cand, verify_tls=False, proxy=proxy, ua=ua, timeout=timeout, headers=headers
                )
                last_general, last_api, last_scheme, last_verify = general, cand, cand.split("://", 1)[0], False
                break
            except WikiUnavailableError as e:
                errors.append(str(e))
    if not last_general and saw_connect_error and parsed.scheme == "https":
        for cand, v_tls in _merge([_try_schemes("http", verify_tls)]):
            if time.monotonic() > deadline:
                break
            try:
                general = await _probe_siteinfo(
                    cand, verify_tls=verify_tls, proxy=proxy, ua=ua, timeout=timeout, headers=headers
                )
                last_general, last_api, last_scheme, last_verify = general, cand, "http", verify_tls
                break
            except WikiUnavailableError as e:
                errors.append(str(e))

    if not last_general:
        detail = "\n".join(errors[:5])
        raise WikiUnavailableError(f"无法解析为可用的 MediaWiki 站点：\n{detail}")

    server = last_general.get("server") or f"{last_scheme}://{parsed.netloc}"
    article_path = last_general.get("articlepath") or "/wiki/$1"
    scriptpath = last_general.get("scriptpath") or "/w"
    generator = last_general.get("generator") or ""
    conn = WikiConnection(
        api_url=last_api or "",
        server=server,
        article_path=article_path,
        scriptpath=scriptpath,
        site_name=last_general.get("sitename") or "",
        lang=last_general.get("lang") or "",
        mw_version=generator,
        scheme=last_scheme,
        verify_tls=last_verify,
        proxy=proxy,
        headers=dict(headers or {}),
        timeout=timeout,
    )
    return conn


class WikiClient:
    """针对单个 WikiConnection 的 API 客户端。"""

    def __init__(
        self,
        conn: WikiConnection,
        ua: str = DEFAULT_UA,
        extra_headers: Optional[dict] = None,
        cookies: Optional[list[dict]] = None,
    ) -> None:
        self.conn = conn
        self._ua = ua
        headers = {"User-Agent": ua}
        headers.update(conn.headers)
        headers.update(extra_headers or {})
        self._http = httpx.AsyncClient(
            verify=conn.verify_tls,
            proxy=conn.proxy,
            timeout=conn.timeout,
            headers=headers,
            auth=(conn.auth_user, conn.auth_pass) if conn.auth_user else None,
            follow_redirects=True,
        )
        # 注入登录 cookie（可选）
        for c in cookies or []:
            if c.get("name") and c.get("value"):
                try:
                    self._http.cookies.set(
                        c["name"],
                        c["value"],
                        domain=c.get("domain") or "",
                        path=c.get("path") or "/",
                    )
                except Exception:
                    continue
        self._fv2: Optional[bool] = None
        self._ns_cache: Optional[dict] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WikiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    def _params(self, params: dict) -> dict:
        p = dict(params)
        p.setdefault("format", "json")
        if p.get("formatversion") is None and self._use_fv2():
            p["formatversion"] = "2"
        # 公网 wiki 礼仪：maxlag
        if p.get("maxlag") is None and p.get("action") != "parse":
            p["maxlag"] = "5"
        return p

    def _use_fv2(self) -> bool:
        if self._fv2 is None:
            self._fv2 = self.conn.supports_formatversion2()
        return self._fv2

    async def _get(self, params: dict) -> dict:
        try:
            r = await self._http.get(self.conn.api_url, params=self._params(params))
            r.raise_for_status()
            data = r.json()
        except httpx.TimeoutException as e:
            raise WikiAPIError("请求超时") from e
        except httpx.HTTPError as e:
            raise WikiAPIError(f"请求失败: {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise WikiAPIError("响应不是合法 JSON") from e

        err = data.get("error") if isinstance(data, dict) else None
        if err:
            code = err.get("code", "") if isinstance(err, dict) else ""
            info = err.get("info", str(err)) if isinstance(err, dict) else str(err)
            # 老旧 MediaWiki 不认识 formatversion=2 → 降级重试一次
            if code in ("badvalue", "unrecognizedvalue") and "formatversion" in str(info):
                self._fv2 = False
                return await self._get(params)
            if code == "missingtitle":
                raise PageNotFoundError(info or "页面不存在")
            if code in ("invalidtitle", "badtitle"):
                raise InvalidTitleError(info or "标题非法")
            raise WikiAPIError(f"{code}: {info}")
        return data

    # ------------------------------------------------------------------ #
    # 查询封装
    # ------------------------------------------------------------------ #
    async def siteinfo(self) -> dict:
        data = await self._get({"action": "query", "meta": "siteinfo", "siprop": "general"})
        return (data.get("query") or {}).get("general") or {}

    async def query_titles(self, titles: list[str]) -> tuple[list[dict], list[dict]]:
        """查询标题状态。返回 (pages, redirects)；pages 已归一化为列表。"""
        data = await self._get(
            {
                "action": "query",
                "titles": "|".join(titles),
                "prop": "info",
                "redirects": "1",
            }
        )
        query = data.get("query") or {}
        pages = query.get("pages")
        redirects = query.get("redirects") or []
        if isinstance(pages, dict):  # formatversion=1：按 pageid 键控
            pages = list(pages.values())
        return list(pages or []), list(redirects)

    async def page_state(self, title: str) -> PageState:
        pages, redirects = await self.query_titles([title])
        page = pages[0] if pages else {}
        state = PageState(
            title=page.get("title") or title,
            before_title=title,
            pageid=page.get("pageid"),
            missing=bool(page.get("missing")),
            invalid=bool(page.get("invalid")),
            redirected=bool(redirects),
        )
        if redirects:
            state.redirect_target = state.title
            state.title = redirects[0].get("to") or state.title
        return state

    async def parse_page(
        self,
        title: Optional[str] = None,
        pageid: Optional[int] = None,
        section: Optional[int] = None,
        prop: tuple[str, ...] = ("text", "sections"),
    ) -> ParsedPage:
        params: dict[str, Any] = {
            "action": "parse",
            "prop": "|".join(prop),
            "redirects": "1",
            "disablepp": "1",
            "disablelimitreport": "1",
        }
        if title is not None:
            params["page"] = title
        if pageid is not None:
            params["pageid"] = pageid
        if section is not None:
            params["section"] = section

        data = await self._get(params)
        parse = data.get("parse") or {}
        text = parse.get("text")
        if isinstance(text, dict):  # formatversion=1
            text = text.get("*", "")
        sections = parse.get("sections") or []
        redirects = parse.get("redirects") or []
        redirect_from = redirects[0].get("from", "") if redirects else ""
        return ParsedPage(
            title=parse.get("title") or title or "",
            pageid=parse.get("pageid") or pageid,
            html=text or "",
            sections=list(sections),
            wikitext=parse.get("wikitext") or "",
            langlinks=parse.get("langlinks") or [],
            is_disambig="mw-disambig" in (text or ""),
            redirect_from=redirect_from,
        )

    async def opensearch(self, query: str, limit: int = 5) -> list[str]:
        data = await self._get(
            {
                "action": "opensearch",
                "search": query,
                "limit": limit,
                "namespace": "0",
            }
        )
        if isinstance(data, list) and len(data) >= 2:
            return list(data[1] or [])
        return []

    async def content_namespace_ids(self) -> list[int]:
        """内容命名空间 ID 列表：主空间(0) + id 为偶数的内容命名空间（排除讨论版）。"""
        mapping = await self.siteinfo_namespaces()
        ids = {v for v in mapping.values() if v >= 0 and v % 2 == 0}
        ids.add(0)  # 主空间总是可搜索
        return sorted(ids)

    async def search(
        self,
        query: str,
        limit: int = 8,
        srwhat: Optional[str] = None,
        srnamespace=None,
    ) -> list[dict]:
        """搜索。srnamespace 可为 int / list[int] / None。

        不传 srnamespace 时自动覆盖全部内容命名空间（避免服务器默认搜索范围
        不含自定义内容命名空间导致的漏搜，例如 Bleap 的「曲目」）。
        """
        params: dict[str, Any] = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "srprop": "snippet",
        }
        if srwhat:
            params["srwhat"] = srwhat
        ns = srnamespace
        if ns is None:
            ns = await self.content_namespace_ids()
        if ns is not None:
            if isinstance(ns, (list, tuple, set)):
                ns = "|".join(str(x) for x in ns)
            params["srnamespace"] = ns
        data = await self._get(params)
        return list(((data.get("query") or {}).get("search")) or [])

    async def siteinfo_namespaces(self) -> dict:
        """返回 {小写命名空间名: id}（含别名），用于识别输入前缀与搜索限定。"""
        if self._ns_cache is not None:
            return self._ns_cache
        data = await self._get(
            {
                "action": "query",
                "meta": "siteinfo",
                "siprop": "namespaces|namespacealiases",
            }
        )
        query = data.get("query") or {}
        namespaces = query.get("namespaces") or {}
        aliases = query.get("namespacealiases") or []
        mapping: dict[str, int] = {}
        for ns in namespaces.values():
            if not isinstance(ns, dict):
                continue
            for name in (ns.get("*"), ns.get("canonical")):
                if name:
                    mapping[name.lower()] = ns.get("id")
        # 主命名空间（id=0）的名字为空串，需显式补上，否则搜索/随机会漏掉主空间
        if 0 not in mapping.values():
            mapping[""] = 0
        for a in aliases:
            if isinstance(a, dict) and a.get("*"):
                mapping[a["*"].lower()] = a.get("id")
        self._ns_cache = mapping
        return mapping

    async def namespace_id_for(self, prefix: str) -> Optional[int]:
        """输入前缀（如 曲目）若为本站命名空间/别名，返回其 id；否则 None。"""
        mapping = await self.siteinfo_namespaces()
        return mapping.get(prefix.strip().lower())

    async def fuzzy_candidates(
        self,
        title: str,
        limit: int = 3,
        namespace=None,
    ) -> list[str]:
        """三路搜索（text/title/nearmatch）合并去重得到候选。

        namespace 可为 int（限定单个命名空间，如输入带前缀时）/ list[int] / None
        （None 时由 search 自动覆盖全部内容命名空间）。
        """
        results: list[str] = []
        for srwhat in ("text", "title", "nearmatch"):
            try:
                found = await self.search(title, limit=1, srwhat=srwhat, srnamespace=namespace)
            except WikiAPIError:
                continue
            for item in found:
                t = item.get("title", "")
                if t and t not in results:
                    results.append(t)
        return results[: max(limit, 1)]

    async def random_titles(self, limit: int = 1, namespaces=None) -> list[str]:
        """随机页面。namespaces 可为 int / list[int] / None（默认主空间 0）。"""
        params: dict[str, Any] = {"action": "query", "list": "random", "rnlimit": limit}
        if namespaces is not None:
            if isinstance(namespaces, (list, tuple, set)):
                params["rnnamespace"] = "|".join(str(x) for x in namespaces)
            else:
                params["rnnamespace"] = namespaces
        data = await self._get(params)
        return [item.get("title", "") for item in (data.get("query") or {}).get("random", [])]

    async def recent_changes(self, limit: int = 20) -> list[dict]:
        data = await self._get(
            {
                "action": "query",
                "list": "recentchanges",
                "rclimit": limit,
                "rcprop": "title|timestamp|user|comment|ids",
            }
        )
        return list(((data.get("query") or {}).get("recentchanges")) or [])

    async def search_suggestions(self, title: str) -> list[str]:
        """页面不存在时给出近似标题建议。"""
        try:
            return await self.opensearch(title, limit=5)
        except WikiAPIError:
            try:
                results = await self.search(title, limit=5)
                return [r.get("title", "") for r in results]
            except WikiAPIError:
                return []

    def find_section_index(self, sections: list[dict], anchor: str) -> Optional[int]:
        """按锚点（章节名）查找章节 index。"""
        target = anchor.strip().lower()
        for s in sections:
            if (s.get("anchor") or "").strip().lower() == target:
                try:
                    return int(s.get("index"))
                except (TypeError, ValueError):
                    return None
            if (s.get("line") or "").strip().lower() == target:
                try:
                    return int(s.get("index"))
                except (TypeError, ValueError):
                    return None
        return None
