"""api_client 单元测试 + 对本地/公网 MediaWiki 的集成测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiki_render.api_client import (
    WikiClient,
    WikiConnection,
    resolve_endpoint,
)
from wiki_render.errors import WikiUnavailableError

# 可用的测试 wiki（Bleap Wiki，公网可达）
TEST_WIKI = "https://wiki.liushuilingling.com"

# ---------------------------------------------------------------- #
# 纯单元测试
# ---------------------------------------------------------------- #
def test_connection_roundtrip():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
        site_name="维基百科",
        lang="zh",
        mw_version="MediaWiki 1.43.0",
        scheme="https",
    )
    d = conn.to_dict()
    conn2 = WikiConnection.from_dict(d)
    assert conn2.api_url == conn.api_url
    assert conn2.mw_version == conn.mw_version
    assert conn2.auth_pass is None


def test_version_support():
    conn = WikiConnection(api_url="x", mw_version="MediaWiki 1.46.0")
    assert conn.supports_formatversion2()
    conn_old = WikiConnection(api_url="x", mw_version="MediaWiki 1.19.0")
    assert not conn_old.supports_formatversion2()


def test_article_url():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    assert conn.article_url("地球") == "https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83"
    assert conn.article_url("地球 大气层") == "https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83_%E5%A4%A7%E6%B0%94%E5%B1%82"


def test_find_section_index():
    client = WikiClient(WikiConnection(api_url="http://x/api.php"))
    sections = [
        {"index": "1", "line": "历史", "anchor": "历史"},
        {"index": "2", "line": "地理", "anchor": "地理"},
    ]
    assert client.find_section_index(sections, "地理") == 2
    assert client.find_section_index(sections, "不存在的章节") is None


def test_mask_sensitive():
    conn = WikiConnection(api_url="x", auth_pass="secret")
    d = conn.mask_sensitive()
    assert d["auth_pass"] == "******"


# ---------------------------------------------------------------- #
# 集成测试（Bleap Wiki；不可达时跳过）
# ---------------------------------------------------------------- #
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
async def test_resolve_endpoint_public():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    assert conn.api_url.endswith("/api.php")
    assert conn.site_name == "Bleap Wiki"
    assert conn.scheme == "https"
    assert conn.mw_version.startswith("MediaWiki")
    # 能生成文章 URL
    assert conn.article_url("Bleap").startswith("https://")


@needs_wiki
@pytest.mark.anyio
async def test_parse_page():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        parsed = await client.parse_page("Bleap")
        assert parsed.title == "Bleap"
        assert parsed.pageid == 4
        assert "mw-parser-output" in parsed.html


@needs_wiki
@pytest.mark.anyio
async def test_missing_page():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        state = await client.page_state("这个页面肯定不存在_xyz")
        assert state.missing


@needs_wiki
@pytest.mark.anyio
async def test_search_and_opensearch():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        titles = await client.opensearch("Bleap")
        assert titles
        results = await client.search("Bleap")
        assert results
        assert results[0]["title"]


@needs_wiki
@pytest.mark.anyio
async def test_random_and_rc():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        titles = await client.random_titles(2)
        assert len(titles) == 2
        rcs = await client.recent_changes(5)
        assert rcs
        assert rcs[0]["title"]


@needs_wiki
@pytest.mark.anyio
async def test_sections():
    conn = await resolve_endpoint(TEST_WIKI, timeout=12)
    async with WikiClient(conn) as client:
        parsed = await client.parse_page("Bleap")
        assert parsed.sections
        idx = client.find_section_index(parsed.sections, parsed.sections[0]["line"])
        assert idx is not None
        sec = await client.parse_page("Bleap", section=idx, prop=("text",))
        assert sec.html.strip()


def test_resolve_endpoint_invalid():
    import asyncio

    async def run():
        with pytest.raises(WikiUnavailableError):
            await resolve_endpoint("http://127.0.0.1:1/notawiki", timeout=3)

    asyncio.run(run())


# ---------------------------------------------------------------- #
# Help:URL 规范的 UTF-8/特殊字符 URL 编码测试
# 依据：MediaWiki 官方 Help:URL（documents/MediaWiki API/帮助_URL - MediaWiki.html）
# ---------------------------------------------------------------- #
def test_article_url_utf8_chinese():
    # 中文按 UTF-8 百分号编码
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    assert conn.article_url("地球") == "https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83"
    assert conn.article_url("曲目:闪音跃动") == (
        "https://zh.wikipedia.org/wiki/%E6%9B%B2%E7%9B%AE:%E9%97%AA%E9%9F%B3%E8%B7%83%E5%8A%A8"
    )


def test_article_url_space_to_underscore():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    # 空格 → 下划线（Help:URL 规范）
    assert conn.article_url("地球 大气层") == (
        "https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83_%E5%A4%A7%E6%B0%94%E5%B1%82"
    )


def test_article_url_encodes_url_semantic_chars():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    # & # ? 必须转义，否则改变 URL 语义（Help:URL 字符转换表）
    assert conn.article_url("A & B") == "https://zh.wikipedia.org/wiki/A_%26_B"
    assert conn.article_url("What?X") == "https://zh.wikipedia.org/wiki/What%3FX"
    assert conn.article_url("C#D") == "https://zh.wikipedia.org/wiki/C%23D"
    assert conn.article_url("'q'") == "https://zh.wikipedia.org/wiki/%27q%27"


def test_article_url_keeps_allowed_chars():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    # Help:URL 允许字符集中的安全字符保留原样
    assert conn.article_url("~foo!bar@baz+qux") == "https://zh.wikipedia.org/wiki/~foo!bar@baz+qux"


def test_article_url_no_double_encoding():
    conn = WikiConnection(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
    )
    # 已编码标题（%XX）不双重编码（% 保留）
    assert conn.article_url("%E5%9C%B0%E7%90%83") == "https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83"
