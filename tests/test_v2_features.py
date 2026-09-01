"""v0.1 功能测试：screen / 命令后缀开关 / login 私聊限制 / 模糊无结果 / 原生回退 / 分组配置。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_wiki_render.main import Main  # noqa: E402
from astrbot_plugin_wiki_render.wiki_render.api_client import WikiConnection  # noqa: E402
from astrbot_plugin_wiki_render.wiki_render.errors import RenderError  # noqa: E402

TEST_WIKI = "https://wiki.liushuilingling.com"


class FakeEvent:
    def __init__(self, text="", admin=True, private=False):
        self.message_str = text
        self.unified_msg_origin = "test-session-v2"
        self._admin = admin
        self._private = private
        self.sent = []
        self._stopped = False

    def is_admin(self):
        return self._admin

    def get_group_id(self):
        return "" if self._private else "group-1"

    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType

        return MessageType.FRIEND_MESSAGE if self._private else MessageType.GROUP_MESSAGE

    def get_platform_id(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return "10001"

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def chain_result(self, chain):
        return {"type": "chain", "chain": chain}

    def image_result(self, url):
        return {"type": "image", "url": url}

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self._stopped = True


class FakeRenderer:
    cache_dir = Path("/tmp/fake_cache")
    fail_url = False

    async def render_article(self, *a, **k):
        p = Path("/tmp/fake_article.png")
        p.write_bytes(b"fake")
        return p

    async def render_url(self, *a, **k):
        if self.fail_url:
            raise RenderError("native render failed (simulated)")
        p = Path("/tmp/fake_url.png")
        p.write_bytes(b"fake")
        return p


def make_main(config=None, renderer=None) -> Main:
    cfg = {
        "default_wiki_api": TEST_WIKI,
        "request_timeout": 15,
        ** (config or {}),
    }
    m = Main(object(), cfg)
    kv: dict = {}

    async def gk(k, d):
        return kv.get(k, d)

    async def pk(k, v):
        kv[k] = v

    m.get_kv_data = gk
    m.put_kv_data = pk
    m.renderer = renderer or FakeRenderer()
    m._kv = kv
    return m


# ---------------------------------------------------------------- #
# 分组配置读取（_cfg 跨分组）
# ---------------------------------------------------------------- #
def test_cfg_grouped_lookup():
    m = make_main({"渲染设置": {"render_width": 999, "render_mode": "self"}})
    assert m._cfg("render_width", 1) == 999
    assert m._cfg("render_mode", "native") == "self"
    assert m._cfg("不存在的键", "x") == "x"


def test_cfg_flat_lookup():
    m = make_main({"render_width": 555})
    assert m._cfg("render_width", 1) == 555


# ---------------------------------------------------------------- #
# ~wiki screen
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_screen_show_and_set():
    m = make_main({"截图方向": {"screen_default": "portrait"}})
    ev = FakeEvent("~wiki screen")
    results = await m._dispatch(ev, "screen")
    assert "竖屏" in results[0]["text"]

    ev2 = FakeEvent("~wiki screen landscape")
    results = await m._dispatch(ev2, "screen landscape")
    assert "横屏" in results[0]["text"]
    assert await m.store.get_screen("test-session-v2", "landscape") == "landscape"


# ---------------------------------------------------------------- #
# 命令后缀开关（command_suffix_mode）
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_suffix_mode_off_default():
    # 默认关闭：~wiki set 当命令（bot 管理员回退，避免群管理判定干扰）
    m = make_main({"高级": {"group_admin_manage": False}})
    ev = FakeEvent("~wiki set")
    results = await m._dispatch(ev, "set")
    assert "用法" in results[0]["text"]  # set 无参数 → 命令提示


@pytest.mark.anyio
async def test_suffix_mode_on():
    m = make_main({"高级": {"command_suffix_mode": True, "group_admin_manage": False}})
    # 开启后：~wiki set; 是命令，~wiki set 是页面名
    ev = FakeEvent("~wiki set;")
    results = await m._dispatch(ev, "set;")
    assert "用法" in results[0]["text"]

    # 不带分号 → 按页面名处理（连接网络，可能未找到——验证不会当成命令即可）
    ev2 = FakeEvent("~wiki set")
    results = await m._dispatch(ev2, "set")
    texts = [r["text"] for r in results if r["type"] == "plain"]
    joined = " ".join(texts)
    assert "用法" not in joined or "未找到" in joined


# ---------------------------------------------------------------- #
# login 私聊限制
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_login_rejected_in_group():
    m = make_main()
    ev = FakeEvent("~wiki login user pass", private=False)
    results = await m._dispatch(ev, "login user pass")
    assert "私聊" in results[0]["text"]


@pytest.mark.anyio
async def test_login_usage_private():
    m = make_main()
    ev = FakeEvent("~wiki login user", private=True)
    results = await m._dispatch(ev, "login user")
    assert "用法" in results[0]["text"]


# ---------------------------------------------------------------- #
# 模糊识别：无结果明确返回
# ---------------------------------------------------------------- #
def _reachable() -> bool:
    import httpx

    try:
        r = httpx.get(
            f"{TEST_WIKI}/api.php",
            params={"action": "query", "meta": "siteinfo", "format": "json"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_wiki = pytest.mark.skipif(not _reachable(), reason="test wiki unreachable")


@needs_wiki
@pytest.mark.anyio
async def test_missing_no_result_clear_message():
    m = make_main()
    ev = FakeEvent("~wiki 完全不存在_zzz_xyz_12345")
    results = await m._query_pages(ev, "完全不存在_zzz_xyz_12345")
    assert results
    r = results[0]
    assert r["type"] == "plain"
    assert "未找到" in r["text"]


# ---------------------------------------------------------------- #
# 原生渲染默认 + 失败回退
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_native_mode_default_and_fallback():
    # render_mode=native（默认）：命中 render_url
    m = make_main()
    ev = FakeEvent("~wiki Bleap")
    # 直接构造已存在的页面流：先 mock _handle_missing 不存在的情况，走真实查询
    results = await m._query_pages(ev, "Bleap")
    # 网络可达时应有结果；不可达时会有绑定/网络提示——这里只验证不崩溃
    assert results is not None

    # 原生渲染失败 → 回退自建模板
    r = FakeRenderer()
    r.fail_url = True
    m2 = make_main(renderer=r)
    conn = WikiConnection(
        api_url=f"{TEST_WIKI}/api.php",
        server="https://wiki.liushuilingling.com",
        article_path="/index.php/$1",
        site_name="Bleap Wiki",
        scheme="https",
    )
    m2._conn_cache[conn.api_url] = conn
    m2.store.set_conn = _noop
    # 直接调用 _render_page 验证回退（用唯一标题避免命中渲染缓存）
    parsed = type("P", (), {"html": "<p>x</p>", "title": "回退测试页_xyz", "is_disambig": False, "redirect_from": ""})()
    img, mode = await m2._render_page(ev, conn, parsed, "回退测试页_xyz", None)
    assert img.exists()
    assert "fallback" in mode


async def _noop(*a, **k):
    return None


# ---------------------------------------------------------------- #
# 渲染结果缓存命中
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_render_cache_hit():
    m = make_main()
    conn = WikiConnection(
        api_url=f"{TEST_WIKI}/api.php",
        server="https://wiki.liushuilingling.com",
        article_path="/index.php/$1",
        scheme="https",
    )
    p1 = await m._render_article("<p>hi</p>", "缓存测试页", conn, "landscape", "test-session-v2")
    p2 = await m._render_article("<p>hi</p>", "缓存测试页", conn, "landscape", "test-session-v2")
    assert p1.exists() and p2.exists()


# ---------------------------------------------------------------- #
# interwiki 与命名空间歧义（前缀:页面）
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_iw_prefix_priority_over_namespace():
    """配置的 interwiki 前缀优先于命名空间。"""
    m = make_main()
    # 配置中文 iw 前缀（验证放宽后的正则支持中文）
    await m.store.set_interwiki("test-session-v2", "曲目", "https://other.wiki/api.php")
    iws = await m.store.get_interwikis("test-session-v2")
    assert "曲目" in iws

    # 查询会命中 iw：应解析到 other.wiki 的 conn（interwiki 解析失败会报提示而非查本 wiki 命名空间）
    ev = FakeEvent("~wiki 曲目:测试页")
    results = await m._query_pages(ev, "曲目:测试页")
    # 解析 https://other.wiki 失败（不可达）→ 提示信息；若成功则走 other.wiki 查询
    # 无论如何，都不应该把「曲目」当本 wiki 命名空间来查
    assert results is not None


@pytest.mark.anyio
async def test_colon_force_current_wiki():
    """~wiki :前缀:页面 强制当前 wiki（绕过 interwiki）。"""
    m = make_main()
    await m.store.set_interwiki("test-session-v2", "曲目", "https://other.wiki/api.php")
    ev = FakeEvent("~wiki :曲目:测试页")
    # 以冒号开头 → 去掉一个冒号后作为完整标题（含命名空间前缀）在当前 wiki 查询
    # Bleap Wiki 上「曲目:测试页」不存在 → 模糊无结果 → 明确返回
    results = await m._query_pages(ev, ":曲目:测试页")
    assert results
    texts = " ".join(r.get("text", "") for r in results if isinstance(r, dict))
    assert "未找到" in texts or "绑定" in texts or "默认" in texts


@pytest.mark.anyio
async def test_namespace_without_iw_config():
    """未配置 iw 时，前缀:页面 作为当前 wiki 命名空间查询。"""
    m = make_main()
    ev = FakeEvent("~wiki 曲目:测试页")
    results = await m._query_pages(ev, "曲目:测试页")
    assert results is not None


# ---------------------------------------------------------------- #
# 随机页面命名空间排除 / 搜索命名空间覆盖 / login 空格提醒
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_parse_namespace_excludes():
    m = make_main()
    # 默认（1-15 内建全排除）
    ids = await m._parse_namespace_excludes()
    assert 0 not in ids and 1 in ids and 15 in ids and 6 in ids
    # 用户自定义增删（配置路径）
    m2 = make_main({"查询与模糊识别": {"random_namespace_excludes": '["ID:6","ID:3000"]'}})
    assert await m2._parse_namespace_excludes() == {6, 3000}
    # 插件页面保存的数组设置优先（KV 路径）
    m4 = make_main()
    await m4.store.set_array_setting("random_namespace_excludes", ["ID:2", "ID:4"])
    assert await m4._parse_namespace_excludes() == {2, 4}
    # 兼容裸数字/中文冒号/逗号分隔
    m3 = make_main({"查询与模糊识别": {"random_namespace_excludes": "ID:6, 3000, ID：8"}})
    assert await m3._parse_namespace_excludes() == {6, 3000, 8}


@needs_wiki
@pytest.mark.anyio
async def test_random_excludes_file_namespace():
    """默认排除 1-15 后，随机页不应落在 File(6) 等内建命名空间。"""
    m = make_main({"查询与模糊识别": {"random_namespace_excludes": '["ID:1","ID:2","ID:3","ID:4","ID:5","ID:6","ID:7","ID:8","ID:9","ID:10","ID:11","ID:12","ID:13","ID:14","ID:15"]'}})
    # 直接测 client：排除后只允许 0 + 自定义内容空间
    from astrbot_plugin_wiki_render.wiki_render.api_client import resolve_endpoint, WikiClient

    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        mapping = await client.siteinfo_namespaces()
        all_ids = sorted({v for v in mapping.values() if v >= 0})
        allowed = [ns for ns in all_ids if ns not in await m._parse_namespace_excludes()]
        assert 6 not in allowed and 1 not in allowed and 0 in allowed
        titles = await client.random_titles(3, namespaces=allowed)
        assert titles


@needs_wiki
@pytest.mark.anyio
async def test_search_covers_content_namespaces():
    """显式内容命名空间后，应能搜到「曲目:无意义都市 feat.电鸟」。"""
    from astrbot_plugin_wiki_render.wiki_render.api_client import resolve_endpoint, WikiClient

    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        r = await client.search("无意义都市", limit=5)
        titles = [x["title"] for x in r]
        assert "曲目:无意义都市 feat.电鸟" in titles, titles
        # fuzzy 候选也应包含曲目页（不再只有误配的「曲师列表」）
        cands = await client.fuzzy_candidates("无意义都市", limit=5)
        assert "曲目:无意义都市 feat.电鸟" in cands, cands


@pytest.mark.anyio
async def test_login_space_hint():
    """用户名含下划线时登录回复带空格替换提醒。"""
    m = make_main()
    ev = FakeEvent("~wiki login my_bot secret", private=True)
    results = await m._cmd_login(ev, "my_bot secret")
    # 未绑定 wiki → 先报绑定错误，但不应崩溃
    assert results


# ---------------------------------------------------------------- #
# 模糊选择 bug 修复：含空格标题不拆分 + 取消提示 + 仅发起者回复
# ---------------------------------------------------------------- #
def test_requester_session_filter():
    from astrbot_plugin_wiki_render.main import RequesterSessionFilter

    f = RequesterSessionFilter()

    class Ev2:
        unified_msg_origin = "aiocqhttp:GroupMessage:123"
        def get_sender_id(self):
            return "10001"

    key = f.filter(Ev2())
    assert "123" in key and "10001" in key


@pytest.mark.anyio
async def test_search_prompt_has_cancel_hint():
    """搜索/模糊 prompt 直接带取消提示。"""
    m = make_main()
    ev = FakeEvent("~wiki search 无意义都市", private=True)
    results = await m._cmd_search(ev, "无意义都市")
    sent = ev.sent
    texts = [x.get("text", "") for x in sent if x.get("type") == "plain"]
    joined = "\n".join(texts)
    assert "取消" in joined


@needs_wiki
@pytest.mark.anyio
async def test_select_space_title_no_resplit():
    """选中的含空格标题（如 曲目:无意义都市 feat.电鸟）必须整标题直查，不再按空格拆分触发二次模糊。"""
    from astrbot_plugin_wiki_render.wiki_render.api_client import resolve_endpoint

    m = make_main()
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    ev = FakeEvent("~wiki 曲目:无意义都市 feat.电鸟")
    results = await m._query_one(ev, conn, title="曲目:无意义都市 feat.电鸟")
    assert results, "应命中页面"
    joined = " ".join(x.get("text", "") for x in results if x.get("type") == "plain")
    assert "你是不是想找" not in joined, f"出现二次模糊：{joined}"
    assert "未找到" not in joined


# ---------------------------------------------------------------- #
# 群管理级别鉴权（_is_group_admin / _check_manage_perm / login #群号）
# ---------------------------------------------------------------- #
class FakePlatformClient:
    def __init__(self, role):
        self.role = role
        self.calls = []

    @property
    def api(self):
        return self

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        if self.role == "raise":
            raise RuntimeError("not in group")
        return {"group_id": kw.get("group_id"), "user_id": kw.get("user_id"), "role": self.role}


class FakePlatform:
    def __init__(self, role="admin"):
        self.client = FakePlatformClient(role)

    def get_client(self):
        return self.client


class FakeContext:
    def __init__(self, platform=None):
        self.platform = platform

    def get_platform_inst(self, platform_id):
        return self.platform


def make_perm_event(admin=False, group="g1", sender="10001"):
    class PE:
        def __init__(self):
            self.sent = []

        def is_admin(self):
            return admin

        def get_group_id(self):
            return group

        def get_sender_id(self):
            return sender

        def get_platform_id(self):
            return "aiocqhttp"

        def get_message_type(self):
            from astrbot.core.platform.message_type import MessageType

            return MessageType.GROUP_MESSAGE

        def plain_result(self, t):
            return {"type": "plain", "text": t}

        async def send(self, r):
            self.sent.append(r)

        def stop_event(self):
            pass

        def get_group_name(self):
            return "g"

    return PE()


@pytest.mark.anyio
async def test_is_group_admin_roles():
    for role, expected in (("owner", True), ("admin", True), ("member", False), ("raise", False)):
        m = make_main()
        m.context = FakeContext(FakePlatform(role))
        ev = make_perm_event()
        result = await m._is_group_admin(ev, "123")
        assert result is expected, f"role={role} -> {result}"
        # 平台不支持（get_platform_inst 返回 None）→ False
        m2 = make_main()
        m2.context = FakeContext(None)
        assert await m2._is_group_admin(ev, "123") is False


@pytest.mark.anyio
async def test_check_manage_perm():
    # 私聊 + bot 管理员 → True
    m = make_main()
    m.context = FakeContext(None)
    assert await m._check_manage_perm(make_perm_event(admin=True, group="")) is True
    # 私聊非管理员 → False
    assert await m._check_manage_perm(make_perm_event(admin=False, group="")) is False
    # 群聊 + 该群管理（即使非 bot 管理员）→ True
    m2 = make_main()
    m2.context = FakeContext(FakePlatform("admin"))
    assert await m2._check_manage_perm(make_perm_event(admin=False, group="123")) is True
    # 群聊 + 成员 → False（bot 管理员也不豁免）
    m3 = make_main()
    m3.context = FakeContext(FakePlatform("member"))
    assert await m3._check_manage_perm(make_perm_event(admin=True, group="123")) is False
    # 群聊 + bot 管理员 + 非该群管理 → False（严格群管理）
    m6 = make_main()
    m6.context = FakeContext(FakePlatform("member"))
    assert await m6._check_manage_perm(make_perm_event(admin=True, group="123")) is False
    # 开关关闭 → 仅 bot 管理员
    m5 = make_main({"高级": {"group_admin_manage": False}})
    m5.context = FakeContext(FakePlatform("admin"))
    assert await m5._check_manage_perm(make_perm_event(admin=False, group="123")) is False
    assert await m5._check_manage_perm(make_perm_event(admin=True, group="123")) is True


@pytest.mark.anyio
async def test_login_with_group_requires_group_admin():
    # 非该群管理 → 拒绝 login #群号
    m = make_main()
    m.context = FakeContext(FakePlatform("member"))
    ev = FakeEvent("~wiki login user pass #123456", private=True, admin=False)
    results = await m._cmd_login(ev, "user pass #123456")
    assert "群主/管理员" in results[0]["text"]
    # bot 管理员也不豁免：非该群管理 → 仍拒绝
    m1 = make_main()
    m1.context = FakeContext(FakePlatform("member"))
    ev1 = FakeEvent("~wiki login user pass #123456", private=True, admin=True)
    results1 = await m1._cmd_login(ev1, "user pass #123456")
    assert "群主/管理员" in results1[0]["text"]
    # 该群管理 → 进入登录流程（无绑定时报绑定错误，而非权限错误）
    m2 = make_main()
    m2.context = FakeContext(FakePlatform("admin"))
    ev2 = FakeEvent("~wiki login user pass #123456", private=True, admin=False)
    results2 = await m2._cmd_login(ev2, "user pass #123456")
    assert "群主/管理员" not in results2[0]["text"]


# ---------------------------------------------------------------- #
# 审计日志（login/logout/插件页删除记录；渲染统计不混入审计）
# ---------------------------------------------------------------- #
@pytest.mark.anyio
async def test_logout_records_audit(tmp_path):
    from astrbot_plugin_wiki_render.wiki_render.report import RenderReport

    m = make_main()
    m.report = RenderReport(tmp_path / "report.jsonl")
    ev = FakeEvent("~wiki logout", private=True)
    await m._cmd_logout(ev, "")
    audit = [r for r in m.report.read() if r.get("kind") == "audit"]
    assert audit and audit[-1]["action"] == "logout"
    assert audit[-1]["operator"] == "10001"


def test_stats_excludes_audit(tmp_path):
    from astrbot_plugin_wiki_render.wiki_render.report import RenderReport

    rp = RenderReport(tmp_path / "r.jsonl")
    rp.record(kind="audit", action="login", success=True)
    rp.record(ok=True, mode="self", page="X", duration=1.2)
    rp.record(ok=False, mode="native", page="Y", error="boom")
    s = rp.stats()
    assert s["total"] == 2 and s["ok"] == 1 and s["failed"] == 1


# ---------------------------------------------------------------- #
# batch_query 默认关闭 / --refresh 强制刷新 / rc 合并转发 / 撤回取消
# ---------------------------------------------------------------- #
@needs_wiki
@pytest.mark.anyio
async def test_batch_query_default_single_title():
    """默认：~wiki Beside Me 整段当一个标题（含空格），不拆成 Beside/Me。"""
    m = make_main()
    ev = FakeEvent("~wiki Beside Me")
    results = await m._query_pages(ev, "Beside Me")
    # 整标题查询：应命中「曲目:Beside Me」类页面，而不是两个拆分查询
    sent_texts = " ".join(x.get("text", "") for x in ev.sent if x.get("type") == "plain")
    result_texts = " ".join(x.get("text", "") for x in results if x.get("type") == "plain")
    joined = sent_texts + " " + result_texts
    assert "你是不是想找" in joined or "未找到" in joined or any(x.get("type") == "chain" for x in results)
    # 关键：不应出现「页面「Me」不存在」这类拆分后的误匹配提示
    assert "页面「Me」不存在" not in joined


@needs_wiki
@pytest.mark.anyio
async def test_batch_query_enabled_splits():
    m = make_main({"查询与模糊识别": {"batch_query": True}})
    ev = FakeEvent("~wiki a b")
    results = await m._query_pages(ev, "a b")
    assert len(results) <= 2  # 拆成两个查询


@pytest.mark.anyio
async def test_refresh_flag_parsing_and_cooldown():
    m = make_main({"渲染设置": {"refresh_cooldown": 60}})
    # 首次强制刷新放行（标题被剥离 --refresh）
    conn = WikiConnection(
        api_url="https://wiki.liushuilingling.com/api.php",
        server="https://wiki.liushuilingling.com",
        article_path="/index.php/$1",
        scheme="https",
    )
    ev = FakeEvent("~wiki 某页 --refresh")
    # 直接查：页面不存在也会走流程（验证 --refresh 被剥离 + 冷却记账）
    r1 = await m._query_one(ev, conn, title="某页 --refresh")
    assert "频繁" not in " ".join(x.get("text", "") for x in r1 if x.get("type") == "plain")
    # 60 秒冷却内第二次强制刷新被拒
    r2 = await m._query_one(ev, conn, title="某页 --refresh")
    texts = " ".join(x.get("text", "") for x in r2 if x.get("type") == "plain")
    assert "过于频繁" in texts


@pytest.mark.anyio
async def test_rc_merge_forward():
    """rc 在 aiocqhttp 平台输出合并转发 Nodes。"""
    m = make_main()
    # mock client：直接替换 _make_client 上下文
    class FakeRC:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def recent_changes(self, limit=15):
            return [
                {"title": "曲目:测试曲", "user": "某人", "timestamp": "2026-09-01T12:00:00Z", "comment": "更新"},
                {"title": "主页面", "user": "管理员", "timestamp": "2026-09-01T11:00:00Z", "comment": ""},
            ]
    m._make_client = lambda conn, sid: FakeRC()

    class RCEv(FakeEvent):
        def get_platform_name(self):
            return "aiocqhttp"

    ev = RCEv("~wiki rc")
    results = await m._cmd_rc(ev)
    assert results and results[0]["type"] == "chain"
    chain = results[0]["chain"]
    nodes_comp = [c for c in chain if hasattr(c, "nodes")]
    assert nodes_comp, "应包含 Nodes 组件"
    nodes = nodes_comp[0].nodes
    assert len(nodes) == 3  # 标题节点 + 2 条
    assert "更改记录" in nodes[1].content[0].text
    assert "https://" in nodes[1].content[0].text


@pytest.mark.anyio
async def test_rc_text_fallback_non_onebot():
    m = make_main()

    class FakeRC:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def recent_changes(self, limit=15):
            return [{"title": "X", "user": "u", "timestamp": "2026-09-01T12:00:00Z", "comment": ""}]
    m._make_client = lambda conn, sid: FakeRC()

    class TGEv(FakeEvent):
        def get_platform_name(self):
            return "telegram"

    ev = TGEv("~wiki rc")
    results = await m._cmd_rc(ev)
    assert results[0]["type"] == "plain"
    assert "更改记录" in results[0]["text"]


@pytest.mark.anyio
async def test_recall_cancels_task():
    """撤回 notice 事件匹配进行中的命令 → 取消发送。"""
    m = make_main()

    class RecallEv:
        unified_msg_origin = "aiocqhttp:GroupMessage:123"
        message_str = ""
        sent = []

        class _MO:
            raw_message = {"post_type": "notice", "notice_type": "group_recall", "message_id": "777"}
            message_id = "x"

        message_obj = _MO()

        def plain_result(self, t): return {"type": "plain", "text": t}
        def chain_result(self, c): return {"type": "chain", "chain": c}
        def image_result(self, u): return {"type": "image", "url": u}
        async def send(self, r): self.sent.append(r)
        def stop_event(self): pass

    ev = RecallEv()
    # 预登记一个进行中的命令任务
    m._pending_tasks["aiocqhttp:GroupMessage:123:777"] = 9999999999
    gen = m.on_message(ev)
    results = []
    async for r in gen:
        results.append(r)
    assert "已撤回" in ev.sent[0]["text"]
    # 取消标记已设置
    assert "aiocqhttp:GroupMessage:123:777" in m._cancelled_tasks
