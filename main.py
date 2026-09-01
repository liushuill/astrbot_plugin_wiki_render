"""astrbot_plugin_wiki_render 主入口。

功能：
- ~wiki <页面名> 获取 MediaWiki 页面内容渲染为图片（原生渲染默认，去顶栏，失败回退自建模板）
- 模糊识别：缺失页三路搜索（text/title/nearmatch）+ 确认交互
- ~wiki login/logout（私聊专属，#群号指定群） / ~wiki screen（横竖屏）
- 命令冲突开关（command_suffix_mode，分号结尾）
- 会话绑定/interwiki/随机/最近更改/搜索选号
- 插件页面（pages/，hasattr 版本守护）+ 渲染日志报告 + 站点 CSS 预存
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from astrbot.core.utils.session_waiter import SessionController, SessionFilter, session_waiter

from .wiki_render import (
    DEFAULT_UA,
    NotBoundError,
    PageNotFoundError,
    RenderError,
    SectionNotFoundError,
    WikiAPIError,
    WikiClient,
    WikiConnection,
    WikiRenderError,
    WikiUnavailableError,
    resolve_endpoint,
)
from .wiki_render import url_rewrite
from .wiki_render.login import LoginManager, wiki_login
from .wiki_render.renderer import Renderer
from .wiki_render.report import RenderReport
from .wiki_render.storage import BindingStore

CMD_RE = re.compile(r"^[~～]\s*wiki\b(.*)$", re.IGNORECASE | re.DOTALL)

SUBCOMMANDS = {
    "set",
    "unset",
    "status",
    "iw",
    "search",
    "id",
    "random",
    "rc",
    "recentchanges",
    "help",
    "login",
    "logout",
    "screen",
}

ADMIN_SUBCOMMANDS = {"set", "unset", "status", "iw"}

HELP_TEXT = """📖 wiki-render 使用说明
~wiki <页面名>           查询并渲染页面为图片（原生渲染，去顶栏）
~wiki <页面名>#章节      只渲染指定章节
~wiki <页面名> --refresh 强制刷新（绕过渲染缓存，有冷却）
~wiki 页1 页2 ...        批量查询（实验性，需配置 batch_query 开启）
~wiki 前缀:页面名        跨 wiki 查询（需先 iw add；前缀优先于命名空间，
                        想查本 wiki 命名空间用 ~wiki :前缀:页面名）
~wiki search <关键词>    搜索并选择条目
~wiki id <页面ID>        按页面 ID 查询
~wiki random             随机页面
~wiki rc                 最近更改
~wiki screen [方向]      查看/切换横竖屏（landscape/portrait）
~wiki set <wiki地址>     绑定本会话的 wiki（管理员）
~wiki unset / status     解除绑定 / 查看绑定（管理员）
~wiki iw add/remove/list 跨 wiki 管理（管理员）
~wiki login <用户> <密码> [#群号]   登录 wiki（仅私聊；含空格用下划线 _ 代替）
~wiki logout [#群号]     退出登录（仅私聊）
~wiki help               本帮助
{suffix_hint}"""


class RequesterSessionFilter(SessionFilter):
    """只接收命令发起者的回复（群内其他人回复不进入本会话，避免干扰）。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


class Main(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.store = BindingStore(self)

        # 数据目录：data/plugin_data/astrbot_plugin_wiki_render/
        base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_wiki_render"
        cache_dir = base / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.renderer = Renderer(
            cache_dir,
            width=int(self._cfg("render_width", 860)),
            landscape_width=int(self._cfg("landscape_width", 1280)),
            portrait_width=int(self._cfg("portrait_width", 420)),
            device_scale_factor=int(self._cfg("device_scale_factor", 2)),
            timeout=float(self._cfg("render_timeout", 30)),
            max_height=int(self._cfg("max_render_height", 15000)),
            screenshot_type=str(self._cfg("screenshot_type", "png")),
            resource_wait_ms=int(self._cfg("resource_wait_ms", 5000)),
            content_padding=int(self._cfg("content_padding", 16)),
        )
        self.login_manager = LoginManager(base)
        self.report = RenderReport(base / "render_report.jsonl")
        self.render_cache_dir = base / "render_cache"
        self.css_cache_dir = base / "css_cache"
        try:
            self.render_cache_dir.mkdir(parents=True, exist_ok=True)
            self.css_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # 渲染串行化（Playwright 单例复用）
        self._render_lock = asyncio.Lock()
        # 端点解析结果缓存（按 api_url）
        self._conn_cache: dict[str, WikiConnection] = {}
        self._default_conn: Optional[WikiConnection] = None
        self._default_conn_failed: Optional[str] = None
        # 撤回取消渲染：进行中的命令消息登记与取消标记
        self._pending_tasks: dict[str, float] = {}  # key: session:msg_id -> expire_ts
        self._cancelled_tasks: set[str] = set()
        # 强制刷新冷却（按会话）
        self._refresh_cooldown: dict[str, float] = {}

        # 插件页面（老版本 AstrBot 无 register_web_api 时静默跳过）
        if hasattr(context, "register_web_api"):
            try:
                self._register_web_apis(context)
            except Exception as e:  # pragma: no cover
                logger.warning(f"wiki-render: register web apis failed: {e}")

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _cfg(self, key: str, default):
        """跨顶层分组读取配置（兼容扁平 dict 与 web_analyzer 式分组结构）。"""
        cfg = self.config
        if not isinstance(cfg, dict):
            return default
        if key in cfg:
            return cfg[key]
        for v in cfg.values():
            if isinstance(v, dict) and key in v:
                return v[key]
        return default

    def _resolve_args(self) -> dict:
        to = float(self._cfg("request_timeout", 15))
        return dict(
            ua=str(self._cfg("user_agent", "") or ""),
            verify_tls=bool(self._cfg("verify_tls", True)),
            proxy=str(self._cfg("proxy", "") or "") or None,
            timeout=to,
            deadline=max(to * 4, 30.0),
        )

    async def _clean_cache(self) -> None:
        try:
            max_files = int(self._cfg("cache_max_files", 50))
            max_age = int(self._cfg("cache_max_age", 3600))
            now = time.time()
            for d in (self.renderer.cache_dir, self.render_cache_dir):
                files = sorted(d.glob("wr_*"), key=lambda p: p.stat().st_mtime)
                keep = []
                for f in files:
                    try:
                        if now - f.stat().st_mtime > max_age:
                            f.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    keep.append(f)
                while len(keep) > max_files:
                    keep.pop(0).unlink(missing_ok=True)
        except Exception as e:  # pragma: no cover
            logger.debug(f"clean cache failed: {e}")

    async def _check_allowlist(self, api_url: str) -> bool:
        # 优先读插件页面保存的数组设置，其次配置/默认
        allowed = await self.store.get_array_setting("allowed_wiki_apis")
        if allowed is None:
            raw = str(self._cfg("allowed_wiki_apis", "[]") or "[]")
            try:
                allowed = json.loads(raw)
            except Exception:
                allowed = []
        if not allowed:
            return True
        return api_url in allowed

    async def _resolve_conn(self, api_url: str) -> WikiConnection:
        if not await self._check_allowlist(api_url):
            raise WikiUnavailableError("该 wiki 不在允许绑定的白名单中")
        cached = self._conn_cache.get(api_url)
        if cached:
            return cached
        conn = await resolve_endpoint(api_url, **self._resolve_args())
        self._conn_cache[api_url] = conn
        return conn

    async def _get_default_conn(self) -> WikiConnection:
        if self._default_conn is None:
            if self._default_conn_failed:
                raise NotBoundError(self._default_conn_failed)
            api = str(self._cfg("default_wiki_api", "") or "").strip()
            if not api:
                raise NotBoundError(
                    "尚未绑定 wiki。请使用 `~wiki set <wiki地址>` 绑定，"
                    "或在插件配置中设置默认 wiki。"
                )
            try:
                self._default_conn = await self._resolve_conn(api)
            except WikiRenderError as e:
                self._default_conn_failed = str(e)
                raise NotBoundError(str(e))
        return self._default_conn

    async def _get_conn(self, event: AstrMessageEvent) -> WikiConnection:
        """当前会话连接：会话绑定 > 全局默认。"""
        conn = await self.store.get_conn(event.unified_msg_origin)
        if conn:
            return conn
        return await self._get_default_conn()

    async def _get_conn_by_session(self, session_id: str) -> WikiConnection:
        conn = await self.store.get_conn(session_id)
        if conn:
            return conn
        return await self._get_default_conn()

    def _make_client(self, conn: WikiConnection, session_id: str) -> WikiClient:
        ua = str(self._cfg("user_agent", "") or "") or DEFAULT_UA
        cookies = self.login_manager.cookies_for_playwright(session_id)
        return WikiClient(conn, ua=ua, cookies=cookies)

    def _is_private(self, event: AstrMessageEvent) -> bool:
        try:
            return event.get_message_type() == MessageType.FRIEND_MESSAGE
        except Exception:
            return False

    def _group_session_str(self, event: AstrMessageEvent, group_id: str) -> str:
        """由私聊事件 + 群号构造该群的 unified_msg_origin。"""
        parts = (event.unified_msg_origin or "").split(":")
        platform = parts[0] if parts else "unknown"
        return f"{platform}:{MessageType.GROUP_MESSAGE.value}:{group_id}"

    async def _is_group_admin(self, event: AstrMessageEvent, group_id: str) -> bool:
        """执行者是否为指定群的群主/管理员（且机器人能查到该成员信息）。

        依赖平台 API（aiocqhttp 的 get_group_member_info）：机器人不在该群、
        执行者不在该群、平台不支持时均返回 False（拒绝）。
        严格按群管理身份判定，bot 管理员不豁免群操作。
        """
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
            if platform is None or not hasattr(platform, "get_client"):
                return False
            client = platform.get_client()
            if client is None or not hasattr(client, "api"):
                return False
            resp = await client.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(event.get_sender_id()),
                no_cache=True,
            )
            role = resp.get("role") if isinstance(resp, dict) else None
            return role in ("owner", "admin")
        except Exception as e:
            logger.debug(f"wiki-render: group admin check failed for {group_id}: {e}")
            return False

    def _is_bot_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    async def _check_manage_perm(self, event: AstrMessageEvent) -> bool:
        """管理命令权限。

        group_admin_manage=True（默认）：群聊中的管理命令要求执行者是该群群主/管理员
        （bot 管理员不豁免）；私聊中的管理命令要求 bot 管理员。
        group_admin_manage=False：全部仅 bot 管理员。
        """
        if not self._cfg("group_admin_manage", True):
            return self._is_bot_admin(event)
        gid = event.get_group_id()
        if gid:
            return await self._is_group_admin(event, gid)
        return self._is_bot_admin(event)

    async def _screen_for(self, event: AstrMessageEvent) -> str:
        return await self.store.get_screen(
            event.unified_msg_origin, str(self._cfg("screen_default", "landscape"))
        )

    # ------------------------------------------------------------------ #
    # 监听器与分发
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 1) 平台撤回通知：匹配进行中的命令 → 标记取消
        raw = None
        try:
            raw = getattr(event.message_obj, "raw_message", None)
        except Exception:
            pass
        if isinstance(raw, dict) and raw.get("post_type") == "notice" and raw.get(
            "notice_type"
        ) in ("group_recall", "friend_recall"):
            recalled_id = str(raw.get("message_id", ""))
            key = f"{event.unified_msg_origin}:{recalled_id}"
            if key in self._pending_tasks:
                self._cancelled_tasks.add(key)
                try:
                    await event.send(event.plain_result("⏹️ 检测到命令已撤回，本次 wiki 渲染已取消。"))
                except Exception:
                    pass
            event.stop_event()
            return

        text = (event.message_str or "").strip()
        m = CMD_RE.match(text)
        if not m:
            return
        event.stop_event()
        args = m.group(1).strip()

        # 2) 登记进行中的命令（供撤回匹配）
        task_key = None
        try:
            cmd_msg_id = str(getattr(event.message_obj, "message_id", ""))
            if cmd_msg_id:
                task_key = f"{event.unified_msg_origin}:{cmd_msg_id}"
                self._pending_tasks[task_key] = time.time() + 120
        except Exception:
            pass
        # 清理过期登记
        now = time.time()
        for k in list(self._pending_tasks):
            if self._pending_tasks[k] < now - 60:
                self._pending_tasks.pop(k, None)
                self._cancelled_tasks.discard(k)

        try:
            results = await self._dispatch(event, args)
        except Exception as e:
            logger.error(f"wiki-render error: {e!r}")
            yield event.plain_result(f"⚠️ wiki 插件内部错误：{e}")
            return

        # 3) 发送前检查撤回：命令已撤回则不发送结果
        if task_key and task_key in self._cancelled_tasks:
            self._pending_tasks.pop(task_key, None)
            self._cancelled_tasks.discard(task_key)
            yield event.plain_result("⏹️ 命令已撤回，本次 wiki 渲染结果不发送。")
            return
        if task_key:
            self._pending_tasks.pop(task_key, None)
        for r in results:
            yield r

    async def _dispatch(self, event: AstrMessageEvent, args: str) -> list:
        suffix_mode = bool(self._cfg("command_suffix_mode", False))
        # 命令冲突开关：开启后子命令必须以分号结尾，否则整段按页面名处理
        if suffix_mode:
            if args.endswith(";"):
                args = args[:-1].rstrip()
            else:
                return await self._query_pages(event, args)

        if not args:
            return [self._help_result(event)]
        tokens = args.split(None, 1)
        cmd = tokens[0].lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        if cmd in ADMIN_SUBCOMMANDS and not await self._check_manage_perm(event):
            return [event.plain_result("⚠️ 该指令需要管理员权限（群管理或 bot 管理员）。")]

        if cmd == "set":
            return await self._cmd_set(event, rest)
        if cmd == "unset":
            return await self._cmd_unset(event)
        if cmd == "status":
            return await self._cmd_status(event)
        if cmd == "iw":
            return await self._cmd_iw(event, rest)
        if cmd == "search":
            return await self._cmd_search(event, rest)
        if cmd == "id":
            return await self._cmd_id(event, rest)
        if cmd in ("rc", "recentchanges"):
            return await self._cmd_rc(event)
        if cmd == "random":
            return await self._cmd_random(event)
        if cmd == "login":
            return await self._cmd_login(event, rest)
        if cmd == "logout":
            return await self._cmd_logout(event, rest)
        if cmd == "screen":
            return await self._cmd_screen(event, rest)
        if cmd == "help":
            return [self._help_result(event)]
        # 其余全部按页面名处理
        return await self._query_pages(event, args)

    def _help_result(self, event: AstrMessageEvent):
        suffix_hint = ""
        if self._cfg("command_suffix_mode", False):
            suffix_hint = "\n⚠️ 命令冲突开关已开启：所有子命令必须以半角分号结尾，如 `~wiki set;`"
        return event.plain_result(
            HELP_TEXT.format(max=int(self._cfg("max_query_pages", 5)), suffix_hint=suffix_hint)
        )

    # ------------------------------------------------------------------ #
    # 子命令
    # ------------------------------------------------------------------ #
    async def _cmd_set(self, event: AstrMessageEvent, url: str) -> list:
        if not url:
            return [event.plain_result("用法：~wiki set <wiki地址>\n例如：~wiki set https://zh.wikipedia.org")]
        await event.send(event.plain_result("🔄 正在检测站点并解析 API 端点…"))
        try:
            conn = await self._resolve_conn(url)
        except WikiRenderError as e:
            return [event.plain_result(f"❌ 绑定失败：{e}")]
        await self.store.set_conn(event.unified_msg_origin, conn)
        self._conn_cache[conn.api_url] = conn
        if self._cfg("prefetch_on_set", True):
            asyncio.create_task(self._prefetch_site_css(conn))
        return [
            event.plain_result(
                f"✅ 已绑定 wiki：{conn.site_name or conn.api_url}\n"
                f"API：{conn.api_url}\n版本：{conn.mw_version or '未知'}\n"
                f"连接：{conn.scheme}（TLS校验：{'开' if conn.verify_tls else '关'}）"
            )
        ]

    async def _cmd_unset(self, event: AstrMessageEvent) -> list:
        ok = await self.store.unset(event.unified_msg_origin)
        if ok:
            return [event.plain_result("✅ 已解除绑定，将使用全局默认 wiki。")]
        return [event.plain_result("当前会话没有绑定 wiki。")]

    async def _cmd_status(self, event: AstrMessageEvent) -> list:
        sid = event.unified_msg_origin
        conn = await self.store.get_conn(sid)
        iws = await self.store.get_interwikis(sid)
        screen = await self._screen_for(event)
        lines = []
        if conn:
            lines.append(f"📌 已绑定：{conn.site_name or conn.api_url}")
            lines.append(f"   API：{conn.api_url}")
            lines.append(f"   版本：{conn.mw_version or '未知'}")
            lines.append(f"   连接：{conn.scheme}，TLS校验：{'开' if conn.verify_tls else '关'}")
        else:
            lines.append("📌 当前会话未绑定 wiki（将使用全局默认）。")
            default_api = str(self._cfg("default_wiki_api", "") or "").strip()
            if default_api:
                lines.append(f"   默认：{default_api}")
        login_info = self.login_manager.info(sid)
        if login_info:
            lines.append(f"   🔑 已登录：{login_info['username']}")
        else:
            lines.append("   🔑 未登录（特殊页面可能无法查看）")
        lines.append(f"   📱 截图方向：{'横屏' if screen == 'landscape' else '竖屏'}")
        if iws:
            lines.append("   interwiki：")
            for k, v in iws.items():
                lines.append(f"     {k} -> {v}")
        else:
            lines.append("   interwiki：无")
        return [event.plain_result("\n".join(lines))]

    async def _cmd_iw(self, event: AstrMessageEvent, rest: str) -> list:
        sid = event.unified_msg_origin
        parts = rest.split()
        if not parts:
            return [event.plain_result("用法：~wiki iw add <前缀> <地址> / iw remove <前缀> / iw list")]
        action = parts[0].lower()
        if action == "list":
            iws = await self.store.get_interwikis(sid)
            if not iws:
                return [event.plain_result("还没有配置 interwiki。")]
            return [event.plain_result("\n".join(f"  {k} -> {v}" for k, v in iws.items()))]
        if action == "add":
            if len(parts) < 3:
                return [event.plain_result("用法：~wiki iw add <前缀> <wiki地址>")]
            prefix = parts[1]
            url = parts[2]
            await event.send(event.plain_result("🔄 正在检测该站点…"))
            try:
                conn = await self._resolve_conn(url)
            except WikiRenderError as e:
                return [event.plain_result(f"❌ 添加失败：{e}")]
            await self.store.set_interwiki(sid, prefix, conn.api_url)
            self._conn_cache[conn.api_url] = conn
            return [event.plain_result(f"✅ 已添加 interwiki：{prefix} -> {conn.site_name or conn.api_url}")]
        if action == "remove":
            if len(parts) < 2:
                return [event.plain_result("用法：~wiki iw remove <前缀>")]
            ok = await self.store.remove_interwiki(sid, parts[1])
            return [event.plain_result("✅ 已移除。" if ok else "❌ 未找到该前缀。")]
        return [event.plain_result("未知的 iw 子命令。")]

    async def _cmd_screen(self, event: AstrMessageEvent, rest: str) -> list:
        sid = event.unified_msg_origin
        cur = await self.store.get_screen(sid, str(self._cfg("screen_default", "landscape")))
        arg = rest.strip().lower()
        if not arg:
            mode_name = "横屏（PC 观感）" if cur == "landscape" else "竖屏（手机长截图）"
            return [
                event.plain_result(
                    f"📱 当前截图方向：{mode_name}\n"
                    "用法：~wiki screen landscape（横屏）/ ~wiki screen portrait（竖屏）"
                )
            ]
        if arg in ("landscape", "横屏", "横"):
            await self.store.set_screen(sid, "landscape")
            return [event.plain_result("✅ 已切换为横屏（PC 观感）。")]
        if arg in ("portrait", "竖屏", "竖"):
            await self.store.set_screen(sid, "portrait")
            return [event.plain_result("✅ 已切换为竖屏（手机长截图观感）。")]
        return [event.plain_result("参数无效：landscape / portrait")]

    async def _cmd_login(self, event: AstrMessageEvent, rest: str) -> list:
        if not self._is_private(event):
            return [event.plain_result("🔒 登录功能仅限私聊使用（避免密码在群聊中泄露）。")]
        parts = rest.split()
        if len(parts) < 2:
            return [event.plain_result("用法：~wiki login <用户名> <密码> [#群号]（仅私聊）")]
        username = parts[0]
        group_arg = None
        if parts[-1].startswith("#"):
            group_arg = parts[-1][1:]
            password = " ".join(parts[1:-1])
        else:
            password = " ".join(parts[1:])
        if not password:
            return [event.plain_result("密码为空。")]
        # 给指定群设置登录：要求执行者是该群群主/管理员（且机器人在该群）；
        # 关闭群管理鉴权时回退为 bot 管理员
        if group_arg:
            if self._cfg("group_admin_manage", True):
                allowed = await self._is_group_admin(event, group_arg)
            else:
                allowed = self._is_bot_admin(event)
            if not allowed:
                return [
                    event.plain_result(
                        f"🔒 你不是群 {group_arg} 的群主/管理员，或机器人不在该群，无法为其设置登录。"
                    )
                ]
        target_sid = (
            self._group_session_str(event, group_arg) if group_arg else event.unified_msg_origin
        )
        try:
            conn = await self._get_conn_by_session(target_sid)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        await event.send(event.plain_result("🔄 正在登录…"))
        try:
            data = await wiki_login(
                conn,
                username,
                password,
                ua=str(self._cfg("user_agent", "") or "") or DEFAULT_UA,
                timeout=float(self._cfg("request_timeout", 15)),
            )
        except WikiRenderError as e:
            hint = ""
            if "_" in username or "_" in password:
                hint = "\n💡 若用户名/密码实际包含空格，请用下划线 _ 代替空格（MediaWiki 会自动转换）。"
            self.report.record(
                kind="audit", action="login", operator=event.get_sender_id(),
                target=target_sid, wiki=conn.api_url, success=False, error=str(e)[:200],
            )
            return [event.plain_result(f"❌ 登录失败：{e}{hint}")]
        self.login_manager.save(target_sid, data)
        where = f"群 {group_arg}" if group_arg else "当前私聊会话"
        self.report.record(
            kind="audit", action="login", operator=event.get_sender_id(),
            target=target_sid, wiki=conn.api_url, success=True, username=username,
        )
        hint = ""
        if "_" in username or "_" in password:
            hint = "\n💡 若用户名/密码实际包含空格，请用下划线 _ 代替空格（MediaWiki 会自动转换）。"
        return [
            event.plain_result(
                f"✅ 已登录 {conn.site_name or conn.api_url}（{where}）。\n"
                "💡 建议使用 BotPassword（受限机器人密码）登录，降低泄露风险。"
                f"{hint}"
            )
        ]

    async def _cmd_logout(self, event: AstrMessageEvent, rest: str) -> list:
        if not self._is_private(event):
            return [event.plain_result("🔒 登录功能仅限私聊使用。")]
        arg = rest.strip()
        group_arg = None
        if arg.startswith("#"):
            group_arg = arg[1:]
        if group_arg:
            if self._cfg("group_admin_manage", True):
                allowed = await self._is_group_admin(event, group_arg)
            else:
                allowed = self._is_bot_admin(event)
            if not allowed:
                return [
                    event.plain_result(
                        f"🔒 你不是群 {group_arg} 的群主/管理员，或机器人不在该群，无法为其退出登录。"
                    )
                ]
        target_sid = (
            self._group_session_str(event, group_arg) if group_arg else event.unified_msg_origin
        )
        ok = self.login_manager.remove(target_sid)
        self.report.record(
            kind="audit", action="logout", operator=event.get_sender_id(),
            target=target_sid, success=ok,
        )
        return [event.plain_result("✅ 已退出登录。" if ok else "当前没有登录态。")]

    async def _cmd_search(self, event: AstrMessageEvent, kw: str) -> list:
        if not kw:
            return [event.plain_result("用法：~wiki search <关键词>")]
        try:
            conn = await self._get_conn(event)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        async with self._make_client(conn, event.unified_msg_origin) as client:
            try:
                results = await client.search(kw, limit=8)
            except WikiRenderError as e:
                return [event.plain_result(f"❌ 搜索失败：{e}")]
        if not results:
            return [event.plain_result(f"未找到与「{kw}」相关的条目。")]
        titles = [r.get("title", "") for r in results]
        prompt_lines = [f"🔎 「{kw}」的搜索结果："]
        prompt_lines += [f"{i + 1}. {t}" for i, t in enumerate(titles)]
        prompt_lines.append("回复序号查看对应条目，发送「取消」退出：")
        await event.send(event.plain_result("\n".join(prompt_lines)))
        await self._wait_pick_and_query(event, conn, titles)
        return []

    async def _cmd_id(self, event: AstrMessageEvent, rest: str) -> list:
        if not rest:
            return [event.plain_result("用法：~wiki id <页面ID>")]
        try:
            conn = await self._get_conn(event)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        pageid = rest.strip()
        if ":" in pageid and not pageid.lstrip("-").isdigit():
            prefix, _, pid = pageid.partition(":")
            conn = await self._conn_for_iw(event, prefix, conn)
            pageid = pid.strip()
        if not pageid.isdigit():
            return [event.plain_result("页面 ID 必须是数字。")]
        return await self._query_one(event, conn, pageid=int(pageid))

    # 默认随机排除：MediaWiki 内建 1-15（2-14 偶数内容空间 + 全部讨论版奇数；0 主空间保留）
    BUILTIN_RANDOM_EXCLUDES = set(range(1, 16))

    async def _parse_namespace_excludes(self) -> set[int]:
        """随机页面排除的命名空间 ID 集合（优先插件页面保存的数组设置）。

        格式 ID:<数字>（如 ID:6）；为空时使用内建默认排除（1-15）；显式 [] 表示不排除。
        """
        arr = await self.store.get_array_setting("random_namespace_excludes")
        if arr is None:
            raw = str(self._cfg("random_namespace_excludes", "") or "").strip()
            if not raw:
                return set(self.BUILTIN_RANDOM_EXCLUDES)
            parts: list[str] = []
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    parts = [str(x) for x in parsed]
            except (ValueError, TypeError):
                parts = [p for p in raw.replace("，", ",").split(",") if p.strip()]
        else:
            parts = [str(x) for x in arr]
        ids: set[int] = set()
        for part in parts:
            part = part.strip()
            m = re.match(r"(?:ID[:：])?\s*(\d+)", part, re.I)
            if m:
                ids.add(int(m.group(1)))
        return ids

    async def _cmd_random(self, event: AstrMessageEvent) -> list:
        try:
            conn = await self._get_conn(event)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        excludes = self._parse_namespace_excludes()
        async with self._make_client(conn, event.unified_msg_origin) as client:
            try:
                if excludes:
                    mapping = await client.siteinfo_namespaces()
                    all_ids = sorted({v for v in mapping.values() if v >= 0} | {0})
                    allowed = [ns for ns in all_ids if ns not in excludes] or [0]
                else:
                    allowed = [0]
                titles = await client.random_titles(1, namespaces=allowed)
            except WikiRenderError as e:
                return [event.plain_result(f"❌ 获取随机页面失败：{e}")]
        if not titles:
            return [event.plain_result("❌ 未获取到随机页面。")]
        # 整标题直查（避免含空格标题被 _query_pages 按空格拆分）
        return await self._query_one(event, conn, title=titles[0])

    async def _cmd_rc(self, event: AstrMessageEvent) -> list:
        try:
            conn = await self._get_conn(event)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        limit = int(self._cfg("rc_limit", 15))
        async with self._make_client(conn, event.unified_msg_origin) as client:
            try:
                rcs = await client.recent_changes(limit)
            except WikiRenderError as e:
                return [event.plain_result(f"❌ 获取最近更改失败：{e}")]
        if not rcs:
            return [event.plain_result("❌ 该 wiki 没有最近更改记录。")]

        nodes = []
        nodes.append(
            Comp.Node(
                content=[Comp.Plain(f"🕒 {conn.site_name or conn.api_url} · 最近更改")],
                name="wiki-render",
                uin="0",
            )
        )
        for r in rcs[:limit]:
            title = r.get("title", "")
            user = r.get("user", "")
            ts = str(r.get("timestamp", ""))[:16].replace("T", " ")
            comment = r.get("comment", "")
            lines = [f"📄 {title}", f"👤 {user} | 🕒 {ts}"]
            if comment:
                lines.append(f"💬 {comment}")
            lines.append(f"🔗 页面：{conn.article_url(title)}")
            lines.append(f"📜 更改记录：{conn.history_url(title)}")
            nodes.append(
                Comp.Node(
                    content=[Comp.Plain("\n".join(lines))],
                    name=user or "wiki",
                    uin="0",
                )
            )

        # 合并转发仅 OneBot v11 支持；其它平台降级为文本列表
        if event.get_platform_name() == "aiocqhttp":
            return [event.chain_result([Comp.Nodes(nodes=nodes)])]

        lines = [f"🕒 {conn.site_name or conn.api_url} · 最近更改："]
        for i, r in enumerate(rcs[:limit], 1):
            title = r.get("title", "")
            lines.append(f"{i}. {title} — {r.get('user','')}")
            lines.append(f"   更改记录：{conn.history_url(title)}")
        return [event.plain_result("\n".join(lines))]

    # ------------------------------------------------------------------ #
    # 页面查询
    # ------------------------------------------------------------------ #
    async def _conn_for_iw(self, event: AstrMessageEvent, prefix: str, current: WikiConnection) -> WikiConnection:
        iws = await self.store.get_interwikis(event.unified_msg_origin)
        api = iws.get(prefix)
        if not api:
            raise WikiUnavailableError(f"未配置 interwiki 前缀「{prefix}」，可用 ~wiki iw add <前缀> <地址> 添加")
        return await self._resolve_conn(api)

    async def _query_pages(self, event: AstrMessageEvent, raw: str, allow_batch: bool = True) -> list:
        try:
            conn = await self._get_conn(event)
        except WikiRenderError as e:
            return [event.plain_result(f"⚠️ {e}")]
        # 多页批量查询（空格拆分）默认关闭：整段输入当作一个标题（含空格），
        # 避免含空格标题被拆分触发误匹配。开启 batch_query 后按空格拆批量（实验性）。
        batch_query = bool(self._cfg("batch_query", False))
        if batch_query:
            parts = raw.split()
        else:
            parts = [raw] if raw.strip() else []
        if not parts:
            return [self._help_result(event)]
        max_pages = int(self._cfg("max_query_pages", 5))
        if len(parts) > max_pages:
            parts = parts[:max_pages]
        results: list = []
        for part in parts:
            if not part or part.startswith("#"):
                continue
            target_conn = conn
            title = part
            if title.startswith(":"):
                # 强制当前 wiki（跳过 interwiki 匹配），例如 :曲目:xxx
                title = title[1:]
            else:
                # interwiki 优先于命名空间：前缀若在会话配置的 interwiki 中则跨 wiki
                m = re.match(r"^([^:]+):(.*)$", title)
                if m:
                    iws = await self.store.get_interwikis(event.unified_msg_origin)
                    if m.group(1) in iws:
                        try:
                            target_conn = await self._conn_for_iw(event, m.group(1), conn)
                            title = m.group(2)
                        except WikiRenderError as e:
                            results.append(event.plain_result(f"⚠️ {e}"))
                            continue
            results.extend(await self._query_one(event, target_conn, title=title))
        return results

    async def _query_one(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        title: Optional[str] = None,
        pageid: Optional[int] = None,
        force_refresh: bool = False,
    ) -> list:
        sid = event.unified_msg_origin
        # 强制刷新标识：~wiki 页面名 --refresh（末尾 token）
        if title and title.endswith(" --refresh"):
            title = title[: -len(" --refresh")].rstrip()
            force_refresh = True
        if force_refresh:
            now = time.time()
            cd = float(self._cfg("refresh_cooldown", 10))
            last = self._refresh_cooldown.get(sid, 0.0)
            if now - last < cd:
                return [
                    event.plain_result(
                        f"⏳ 强制刷新过于频繁，请 {max(1, int(cd - (now - last)))} 秒后再试。"
                    )
                ]
            self._refresh_cooldown[sid] = now

        section = None
        if title and "#" in title:
            title, _, anchor = title.partition("#")
            section = anchor.strip() or None

        # 特殊页面直接走原生 URL 渲染（parse 对 Special 无效）
        if title and re.match(r"^[Ss]pecial:", title):
            try:
                img_path = await self._render_native(event, conn, title, title, force=force_refresh)
            except RenderError as e:
                if self._cfg("fallback_to_text", True):
                    return [event.plain_result(f"📄 {title}\n（渲染失败：{e}）\n🔗 {conn.article_url(title)}")]
                return [event.plain_result(f"❌ 渲染失败：{e}")]
            chain = [Comp.Image.fromFileSystem(str(img_path))]
            if self._cfg("send_text_info", True):
                chain.insert(0, Comp.Plain(f"📄 {title}\n🔗 {conn.article_url(title)}"))
            return [event.chain_result(chain)]

        sid = event.unified_msg_origin
        async with self._make_client(conn, sid) as client:
            try:
                state = await client.page_state(title) if title else None
            except WikiRenderError as e:
                return [event.plain_result(f"⚠️ {e}")]

            if state and state.invalid:
                return [event.plain_result(f"❌ 标题「{state.before_title}」非法。")]
            if state and state.missing:
                return await self._handle_missing(event, conn, client, title or "")

            try:
                if section is not None:
                    parsed = await client.parse_page(title=title, pageid=pageid, prop=("text", "sections"))
                    idx = client.find_section_index(parsed.sections, section)
                    if idx is None:
                        lines = [f"❌ 章节「{section}」不存在。可用章节："]
                        lines += [f"  {i + 1}. {s.get('line','')}" for i, s in enumerate(parsed.sections[:20])]
                        return [event.plain_result("\n".join(lines))]
                    parsed = await client.parse_page(title=title, pageid=pageid, section=idx, prop=("text",))
                    display_title = f"{parsed.title} § {section}"
                else:
                    parsed = await client.parse_page(title=title, pageid=pageid, prop=("text", "sections"))
                    display_title = parsed.title
            except PageNotFoundError:
                return await self._handle_missing(event, conn, client, title or "")
            except WikiRenderError as e:
                return [event.plain_result(f"⚠️ {e}")]

        if not parsed.html.strip():
            return [event.plain_result("❌ 该页面没有可渲染的内容。")]

        article_html = url_rewrite.rewrite_article_html(parsed.html, conn)
        notes = []
        if parsed.redirect_from and parsed.redirect_from != parsed.title:
            notes.append(f"↪️ 重定向自「{parsed.redirect_from}」→「{parsed.title}」")
        if parsed.is_disambig:
            notes.append("ℹ️ 这是一个消歧义页，可回复序号查看：")
            links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', article_html)
            seen = set()
            cnt = 0
            for href, label in links:
                label = re.sub(r"<[^>]+>", "", label).strip()
                if not label or label in seen:
                    continue
                seen.add(label)
                notes.append(f"  {cnt + 1}. {label}")
                cnt += 1
                if cnt >= 5:
                    break
        link = conn.article_url(parsed.title)

        try:
            if section is not None:
                # 章节查询走自建模板（原生定位复杂，兜底稳定）
                img_path = await self._render_article_ctx(
                    event, conn, article_html, display_title, screen=None, force=force_refresh
                )
                mode = "self"
            else:
                img_path, mode = await self._render_page(
                    event, conn, parsed, display_title, None, force=force_refresh
                )
        except RenderError as e:
            if self._cfg("fallback_to_text", True):
                lines = [f"📄 {display_title}"]
                lines += notes
                lines.append(f"（渲染失败：{e}）")
                lines.append(f"🔗 {link}")
                return [event.plain_result("\n".join(lines))]
            return [event.plain_result(f"❌ 渲染失败：{e}")]

        chain = [Comp.Image.fromFileSystem(str(img_path))]
        if self._cfg("send_text_info", True):
            info = f"📄 {display_title}"
            if notes:
                info += "\n" + "\n".join(notes)
            info += f"\n🔗 {link}"
            chain.insert(0, Comp.Plain(info))
        return [event.chain_result(chain)]

    async def _handle_missing(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        client: WikiClient,
        title: str,
    ) -> list:
        """缺失页：三路搜索 + 确认交互；无结果明确返回。"""
        # 命名空间推断（输入带前缀且为本站命名空间时限定搜索范围）
        ns_id = None
        if ":" in title and not title.startswith(":"):
            prefix = title.split(":", 1)[0]
            try:
                ns_id = await client.namespace_id_for(prefix)
            except WikiRenderError:
                ns_id = None
        limit = max(1, int(self._cfg("fuzzy_search_limit", 3)))
        try:
            candidates = await client.fuzzy_candidates(title, limit=limit, namespace=ns_id)
        except WikiRenderError:
            candidates = []
        if not candidates:
            return [
                event.plain_result(
                    f"❌ 未找到页面「{title}」，也没有相关搜索结果。"
                )
            ]
        if len(candidates) == 1 and self._cfg("auto_fuzzy_jump", False):
            await event.send(event.plain_result(f"↪️ 页面「{title}」不存在，已自动跳转到最相近页面「{candidates[0]}」。"))
            return await self._query_one(event, conn, title=candidates[0])
        prompt = [f"❌ 页面「{title}」不存在，你是不是想找："]
        prompt += [f"{i + 1}. {c}" for i, c in enumerate(candidates)]
        prompt.append("回复序号查看对应条目，发送「取消」退出：")
        await event.send(event.plain_result("\n".join(prompt)))
        await self._wait_pick_and_query(event, conn, candidates)
        return []

    # ------------------------------------------------------------------ #
    # 交互选择（session_waiter）
    # ------------------------------------------------------------------ #
    async def _wait_pick_and_query(
        self, event: AstrMessageEvent, conn: WikiConnection, titles: list[str]
    ) -> None:
        async def act(ev: AstrMessageEvent, conn: WikiConnection, title: str) -> list:
            # 直接查询单个标题：避免 _query_pages 按空格拆分（标题可含空格，
            # 如「曲目:无意义都市 feat.电鸟」），否则会触发二次模糊搜索
            return await self._query_one(ev, conn, title=title)

        await self._wait_pick(event, conn, titles, act)

    async def _wait_pick(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        titles: list[str],
        act: Callable[[AstrMessageEvent, WikiConnection, str], object],
    ) -> None:
        @session_waiter(timeout=45, record_history_chains=False)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            choice = (ev.message_str or "").strip()
            if choice in ("取消", "退出"):
                await ev.send(ev.plain_result("已取消。"))
                controller.stop()
                return
            if not choice.isdigit():
                await ev.send(ev.plain_result("请回复数字序号（如 1），或发送「取消」退出。"))
                return
            idx = int(choice) - 1
            if idx < 0 or idx >= len(titles):
                await ev.send(ev.plain_result(f"序号无效，请输入 1-{len(titles)}。"))
                return
            controller.stop()
            try:
                results = await act(ev, conn, titles[idx])
            except WikiRenderError as e:
                await ev.send(ev.plain_result(f"⚠️ {e}"))
                return
            for r in results:
                await ev.send(r)

        try:
            # 只接收命令发起者的回复（群内其他人回复不进入本会话）
            await waiter(event, session_filter=RequesterSessionFilter())
        except TimeoutError:
            await event.send(event.plain_result("⏰ 选择超时，已取消。"))
        finally:
            event.stop_event()

    # ------------------------------------------------------------------ #
    # 渲染封装（原生默认 + 兜底 + 缓存 + 报告）
    # ------------------------------------------------------------------ #
    def _cache_key(self, api_url: str, title: str, screen: str, logged_in: bool, mode: str) -> str:
        raw = f"{api_url}|{title}|{screen}|{'in' if logged_in else 'out'}|{mode}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _cache_hit(self, key: str) -> Optional[Path]:
        try:
            max_age = int(self._cfg("cache_max_age", 3600))
            ext = str(self._cfg("screenshot_type", "png"))
            # 只匹配当前配置的扩展名，避免 png/jpeg 切换时命中旧格式缓存
            for f in self.render_cache_dir.glob(f"{key}.{ext}"):
                if time.time() - f.stat().st_mtime <= max_age:
                    return f
        except OSError:
            pass
        return None

    def _cache_put(self, key: str, src: Path) -> None:
        try:
            ext = str(self._cfg("screenshot_type", "png"))
            dst = self.render_cache_dir / f"{key}.{ext}"
            if dst.exists():
                dst.unlink()
            dst.write_bytes(src.read_bytes())
        except OSError:
            pass

    async def _render_article_ctx(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        html: str,
        title: str,
        screen: Optional[str],
        force: bool = False,
    ) -> Path:
        sid = event.unified_msg_origin
        screen = screen or await self._screen_for(event)
        return await self._render_article(html, title, conn, screen, sid, force=force)

    async def _render_page(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        parsed,
        display_title: str,
        section,
        force: bool = False,
    ) -> tuple[Path, str]:
        """渲染页面：render_mode=native 时走原生 URL（去顶栏），失败回退自建模板。"""
        sid = event.unified_msg_origin
        screen = await self._screen_for(event)
        mode = str(self._cfg("render_mode", "native")).lower()
        if mode != "native":
            img = await self._render_article(parsed.html, display_title, conn, screen, sid, force=force)
            return img, "self"
        url = conn.article_url(parsed.title)
        try:
            img = await self._render_url(url, display_title, conn, screen, sid, force=force)
            return img, "native"
        except RenderError:
            if not self._cfg("fallback_to_text", True):
                raise
            img = await self._render_article(parsed.html, display_title, conn, screen, sid, force=force)
            return img, "self(native-fallback)"

    async def _render_native(
        self,
        event: AstrMessageEvent,
        conn: WikiConnection,
        title: str,
        display_title: str,
        force: bool = False,
    ) -> Path:
        sid = event.unified_msg_origin
        screen = await self._screen_for(event)
        url = conn.article_url(title)
        return await self._render_url(url, display_title, conn, screen, sid, force=force)

    async def _render_article(
        self, html: str, title: str, conn: WikiConnection, screen: str, sid: str, force: bool = False
    ) -> Path:
        cookies = self.login_manager.cookies_for_playwright(sid)
        key = self._cache_key(conn.api_url, title, screen, bool(cookies), "self")
        hit = None if force else self._cache_hit(key)
        if hit:
            self.report.record(ok=True, mode="self", page=title, wiki=conn.api_url, cache_hit=True)
            return hit
        await self._clean_cache()
        t0 = time.monotonic()
        async with self._render_lock:
            try:
                out = await self.renderer.render_article(
                    html,
                    title=title,
                    site_name=conn.site_name,
                    lang=conn.lang or "zh",
                    screen=screen,
                )
            except RenderError as e:
                self.report.record(ok=False, mode="self", page=title, wiki=conn.api_url, error=str(e)[:200])
                raise
        self._cache_put(key, out)
        self.report.record(
            ok=True, mode="self", page=title, wiki=conn.api_url,
            duration=round(time.monotonic() - t0, 2), size=out.stat().st_size,
        )
        return out

    async def _render_url(
        self, url: str, title: str, conn: WikiConnection, screen: str, sid: str, force: bool = False
    ) -> Path:
        cookies = self.login_manager.cookies_for_playwright(sid)
        key = self._cache_key(conn.api_url, title, screen, bool(cookies), "native")
        hit = None if force else self._cache_hit(key)
        if hit:
            self.report.record(ok=True, mode="native", page=title, wiki=conn.api_url, cache_hit=True)
            return hit
        await self._clean_cache()
        t0 = time.monotonic()
        extra_css = self._load_cached_css(conn.api_url)
        async with self._render_lock:
            try:
                out = await self.renderer.render_url(
                    url,
                    screen=screen,
                    cookies=cookies,
                    extra_css=extra_css,
                )
            except RenderError as e:
                self.report.record(ok=False, mode="native", page=title, wiki=conn.api_url, error=str(e)[:200])
                raise
        self._cache_put(key, out)
        self.report.record(
            ok=True, mode="native", page=title, wiki=conn.api_url,
            duration=round(time.monotonic() - t0, 2), size=out.stat().st_size,
        )
        return out

    # ------------------------------------------------------------------ #
    # 站点 CSS 预存（~wiki set 后异步预取）
    # ------------------------------------------------------------------ #
    def _load_cached_css(self, api_url: str) -> Optional[str]:
        key = hashlib.md5(api_url.encode("utf-8")).hexdigest()
        p = self.css_cache_dir / f"{key}.css"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return None
        return None

    async def _prefetch_site_css(self, conn: WikiConnection) -> None:
        try:
            key = hashlib.md5(conn.api_url.encode("utf-8")).hexdigest()
            dest = self.css_cache_dir / f"{key}.css"
            if dest.exists():
                return
            headers = {"User-Agent": str(self._cfg("user_agent", "") or "")}
            async with httpx.AsyncClient(
                verify=conn.verify_tls,
                proxy=conn.proxy,
                timeout=10,
                headers=headers,
                follow_redirects=True,
            ) as client:
                r = await client.get(conn.article_url("Main Page"))
                html = r.text
                links = re.findall(
                    r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
                    html,
                    re.I,
                )
                parts = []
                for href in links[:4]:
                    if href.startswith("data:"):
                        continue
                    abs_url = url_rewrite.abs_url(href, conn.server, conn.scheme)
                    try:
                        cr = await client.get(abs_url)
                        if cr.status_code == 200:
                            parts.append(cr.text)
                    except Exception:
                        continue
            if parts:
                dest.write_text("\n".join(parts), encoding="utf-8")
                logger.info(f"wiki-render: prefetched site css for {conn.api_url} ({len(parts)} files)")
        except Exception as e:  # pragma: no cover
            logger.debug(f"wiki-render: prefetch css failed: {e}")

    # ------------------------------------------------------------------ #
    # 插件页面（WebUI）API
    # ------------------------------------------------------------------ #
    def _register_web_apis(self, context: Context) -> None:
        prefix = "/astrbot_plugin_wiki_render"
        routes = [
            (f"{prefix}/overview", self._api_overview, ["GET"]),
            (f"{prefix}/bindings", self._api_bindings, ["GET"]),
            (f"{prefix}/bindings/remove", self._api_binding_remove, ["POST"]),
            (f"{prefix}/logins", self._api_logins, ["GET"]),
            (f"{prefix}/logins/remove", self._api_login_remove, ["POST"]),
            (f"{prefix}/render_report", self._api_render_report, ["GET"]),
            (f"{prefix}/settings/arrays", self._api_settings_arrays, ["GET", "POST"]),
        ]
        for path, handler, methods in routes:
            context.register_web_api(path, handler, methods, f"wiki-render: {path.rsplit('/', 1)[-1]}")

    async def _api_overview(self):
        from astrbot.api.web import json_response

        from .wiki_render.renderer import HAS_PLAYWRIGHT

        return json_response(
            {
                "has_playwright": HAS_PLAYWRIGHT,
                "browser_broken": getattr(self.renderer, "_broken", False),
                "default_wiki_api": str(self._cfg("default_wiki_api", "") or ""),
                "render_mode": str(self._cfg("render_mode", "native")),
                "screen_default": str(self._cfg("screen_default", "landscape")),
                "render_report": self.report.stats(),
                "audit_last": [r for r in self.report.read() if r.get("kind") == "audit"][-10:][::-1],
            }
        )

    async def _api_bindings(self):
        from astrbot.api.web import json_response

        bindings = await self.store.all_bindings()
        out = {}
        for sid, info in bindings.items():
            login = self.login_manager.info(sid)
            out[sid] = {**info, "logged_in": bool(login), "login_user": (login or {}).get("username", "")}
        return json_response({"bindings": out})

    async def _api_binding_remove(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        sid = str(payload.get("session_id") or "")
        if not sid:
            return error_response("missing session_id", status_code=400)
        await self.store.unset(sid)
        self.login_manager.remove(sid)
        self.report.record(
            kind="audit", action="web_binding_remove", operator=request.username,
            target=sid, success=True,
        )
        return json_response({"removed": True, "session_id": sid})

    async def _api_logins(self):
        from astrbot.api.web import json_response

        out = {}
        if self.login_manager.dir.exists():
            for p in self.login_manager.dir.glob("*.json"):
                sid = p.stem
                info = self.login_manager.info(sid)
                if info:
                    out[sid] = info
        return json_response({"logins": out})

    async def _api_login_remove(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        sid = str(payload.get("session_id") or "")
        if not sid:
            return error_response("missing session_id", status_code=400)
        ok = self.login_manager.remove(sid)
        self.report.record(
            kind="audit", action="web_login_remove", operator=request.username,
            target=sid, success=ok,
        )
        return json_response({"removed": ok, "session_id": sid})

    async def _api_render_report(self):
        from astrbot.api.web import json_response

        return json_response({"stats": self.report.stats()})

    async def _api_settings_arrays(self):
        """数组类设置的读取与保存（插件页面；AstrBot 配置界面无法友好编辑长 JSON）。"""
        from astrbot.api.web import error_response, json_response, request

        if request.method == "GET":
            return json_response(
                {
                    "random_namespace_excludes": await self.store.get_array_setting(
                        "random_namespace_excludes", []
                    )
                    or [],
                    "allowed_wiki_apis": await self.store.get_array_setting(
                        "allowed_wiki_apis", []
                    )
                    or [],
                }
            )
        payload = await request.json(default={})
        key = str(payload.get("key") or "")
        value = payload.get("value")
        if key not in ("random_namespace_excludes", "allowed_wiki_apis"):
            return error_response("invalid key", status_code=400)
        if not isinstance(value, list):
            return error_response("value must be a list", status_code=400)
        if key == "random_namespace_excludes":
            clean = []
            for item in value:
                s = str(item).strip()
                if re.match(r"(?:ID[:：])?\s*\d+$", s):
                    clean.append(s)
            value = clean
        else:
            value = [str(x).strip() for x in value if str(x).strip()]
        await self.store.set_array_setting(key, value)
        return json_response({"saved": True, "key": key, "value": value})

    async def terminate(self) -> None:
        try:
            await self.renderer.close()
        except Exception as e:  # pragma: no cover
            logger.debug(f"close renderer: {e}")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
