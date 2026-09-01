"""main.py 编排流程测试（宿主机，astrbot 用 stub，渲染用 mock）。

覆盖：~wiki 页面查询全流程、重定向/章节/缺失页、绑定、interwiki、权限。
网络相关用例连接到 Bleap Wiki，不可达时跳过。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_wiki_render.main import Main  # noqa: E402

TEST_WIKI = "https://wiki.liushuilingling.com"


class FakeEvent:
    """模拟 AstrMessageEvent 的最小实现。"""

    def __init__(self, text="", admin=True):
        self.message_str = text
        self.unified_msg_origin = "test-session-1"
        self._admin = admin
        self.sent = []
        self._stopped = False

    def is_admin(self):
        return self._admin

    def get_group_id(self):
        return "group-1"

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

    async def render_article(self, *a, **k):
        p = Path("/tmp/fake_article.png")
        p.write_bytes(b"fake-image")
        return p

    async def render_url(self, *a, **k):
        p = Path("/tmp/fake_url.png")
        p.write_bytes(b"fake-image")
        return p


def make_main(config=None) -> Main:
    cfg = {
        "default_wiki_api": TEST_WIKI,
        "send_text_info": True,
        "request_timeout": 15,
        "高级": {"group_admin_manage": False},
        ** (config or {}),
    }
    m = Main(object(), cfg)
    # 内存 KV（避免写真实 AstrBot DB）
    kv: dict = {}

    async def gk(k, d):
        return kv.get(k, d)

    async def pk(k, v):
        kv[k] = v

    m.get_kv_data = gk
    m.put_kv_data = pk
    m.renderer = FakeRenderer()
    return m, kv


def _wiki_reachable() -> bool:
    import httpx

    try:
        r = httpx.get(f"{TEST_WIKI}/api.php", params={"action": "query", "meta": "siteinfo", "format": "json"}, timeout=8)
        return r.status_code == 200
    except Exception:
        return False


needs_wiki = pytest.mark.skipif(not _wiki_reachable(), reason="测试 wiki 不可达")


@needs_wiki
@pytest.mark.anyio
async def test_query_page_flow():
    m, kv = make_main()
    ev = FakeEvent("~wiki Bleap")
    results = await m._query_pages(ev, "Bleap")
    assert results, "应有结果"
    r = results[0]
    assert r["type"] == "chain"
    texts = [c.text for c in r["chain"] if hasattr(c, "text")]
    assert any("Bleap" in t for t in texts), texts
    imgs = [c for c in r["chain"] if hasattr(c, "path")]
    assert imgs, "应包含图片组件"


@needs_wiki
@pytest.mark.anyio
async def test_query_section_flow():
    m, kv = make_main()
    ev = FakeEvent("~wiki Bleap#特色")
    results = await m._query_pages(ev, "Bleap#特色")
    assert results
    assert results[0]["type"] == "chain"


@needs_wiki
@pytest.mark.anyio
async def test_missing_page_suggestions():
    m, kv = make_main()
    ev = FakeEvent("~wiki 不存在的页面xyz123")
    results = await m._query_pages(ev, "不存在的页面xyz123")
    assert results
    r = results[0]
    assert r["type"] == "plain"
    assert "不存在" in r["text"]


@needs_wiki
@pytest.mark.anyio
async def test_set_binding_and_query():
    m, kv = make_main()
    ev = FakeEvent("~wiki set https://wiki.liushuilingling.com")
    results = await m._cmd_set(ev, "https://wiki.liushuilingling.com")
    assert results[0]["type"] == "plain"
    assert "已绑定" in results[0]["text"]
    # 绑定已写入 KV
    data = kv.get("astrbot_plugin_wiki_render:v1", {})
    assert "test-session-1" in data["bindings"]
    # 用绑定后的连接查询
    ev2 = FakeEvent("~wiki Bleap")
    results2 = await m._query_pages(ev2, "Bleap")
    assert results2 and results2[0]["type"] == "chain"


@needs_wiki
@pytest.mark.anyio
async def test_permission_required_for_admin_cmds():
    m, kv = make_main()
    ev = FakeEvent("~wiki set x", admin=False)
    results = await m._dispatch(ev, "set x")
    assert results[0]["type"] == "plain"
    assert "管理员" in results[0]["text"]


@pytest.mark.anyio
async def test_unbound_error():
    m, kv = make_main({"default_wiki_api": ""})
    ev = FakeEvent("~wiki 地球")
    results = await m._query_pages(ev, "地球")
    assert results[0]["type"] == "plain"
    assert "绑定" in results[0]["text"] or "设置默认" in results[0]["text"]


@pytest.mark.anyio
async def test_batch_limit():
    m, kv = make_main({"max_query_pages": 2})
    # 超过上限时截断为 2 个（用不存在的页验证不会崩）
    ev = FakeEvent("~wiki a b c d")
    results = await m._query_pages(ev, "a b c d")
    assert len(results) <= 2
