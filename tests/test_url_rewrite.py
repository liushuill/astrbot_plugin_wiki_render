"""url_rewrite 单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiki_render.api_client import WikiConnection
from wiki_render import url_rewrite


def make_conn(**kw) -> WikiConnection:
    base = dict(
        api_url="https://zh.wikipedia.org/w/api.php",
        server="https://zh.wikipedia.org",
        article_path="/wiki/$1",
        scheme="https",
    )
    base.update(kw)
    return WikiConnection(**base)


def test_relative_wiki_links():
    html = '<a href="/wiki/%E5%9C%B0%E7%90%83">地球</a>'
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert 'href="https://zh.wikipedia.org/wiki/%E5%9C%B0%E7%90%83"' in out


def test_protocol_relative_images():
    html = '<img src="//upload.wikimedia.org/foo.png">'
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert 'src="https://upload.wikimedia.org/foo.png"' in out


def test_protocol_relative_http_scheme():
    conn = make_conn(scheme="http", server="http://127.0.0.1:7990")
    html = '<img src="//img.example.org/a.png">'
    out = url_rewrite.rewrite_article_html(html, conn)
    assert 'src="http://img.example.org/a.png"' in out


def test_srcset_rewrite():
    html = '<img srcset="//a.example.org/x.png 1x, /wiki/y.png 2x">'
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert "https://a.example.org/x.png 1x" in out
    assert "https://zh.wikipedia.org/wiki/y.png 2x" in out


def test_anchors_untouched():
    html = '<a href="#section-1">锚点</a>'
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert 'href="#section-1"' in out


def test_absolute_and_special_schemes_untouched():
    html = (
        '<a href="https://example.com/x">外链</a>'
        '<a href="mailto:a@b.c">mail</a>'
        '<img src="data:image/png;base64,AAAA">'
    )
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert 'href="https://example.com/x"' in out
    assert 'href="mailto:a@b.c"' in out
    assert 'src="data:image/png;base64,AAAA"' in out


def test_script_removed():
    html = "<p>hi</p><script>alert(1)</script><p>bye</p>"
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert "<script" not in out
    assert "alert" not in out


def test_data_src_rewritten():
    html = '<img data-src="/w/images/thumb.png">'
    out = url_rewrite.rewrite_article_html(html, make_conn())
    assert 'data-src="https://zh.wikipedia.org/w/images/thumb.png"' in out
