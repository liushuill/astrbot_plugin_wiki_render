"""文章 HTML 模板与内置 CSS 构建（管线 A 的渲染骨架）。

自建类 MediaWiki 样式，保证不同 wiki 的解析 HTML 在本地渲染观感统一，
不依赖站点自身的 load.php 样式表。
"""

from __future__ import annotations

from typing import Optional

from .api_client import WikiConnection

_CSS = """
:root {
  --wr-text: #202122;
  --wr-link: #36c;
  --wr-border: #a2a9b1;
  --wr-bg: #f8f9fa;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: #fff;
}
body {
  font-family: -apple-system, "Segoe UI", "Noto Sans", "WenQuanYi Zen Hei",
               "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 15px; line-height: 1.65;
  color: var(--wr-text);
  -webkit-font-smoothing: antialiased;
}
.wr-header {
  display: flex; align-items: baseline; justify-content: space-between;
  padding: 12px 20px 10px;
  border-bottom: 1px solid var(--wr-border);
  background: var(--wr-bg);
}
.wr-header .wr-title {
  font-size: 20px; font-weight: 700; color: #000;
  overflow-wrap: anywhere;
}
.wr-header .wr-sitename { font-size: 13px; color: #72777d; white-space: nowrap; }
.wr-content {
  margin: 0 auto;
  padding: 16px 22px 28px;
}
.wr-content img { max-width: 100%; height: auto; }
.wr-content a { color: var(--wr-link); text-decoration: none; }
.wr-content a:hover { text-decoration: underline; }
.wr-content h1, .wr-content h2, .wr-content h3,
.wr-content h4, .wr-content h5, .wr-content h6 {
  font-weight: 700; line-height: 1.3; color: #000;
  border-bottom: 1px solid var(--wr-border);
  padding-bottom: 3px; margin: 22px 0 10px;
}
.wr-content h1 { font-size: 26px; border-bottom: 2px solid var(--wr-border); }
.wr-content h2 { font-size: 21px; }
.wr-content h3 { font-size: 17px; border-bottom: none; }
.wr-content h4, .wr-content h5, .wr-content h6 { border-bottom: none; font-size: 15px; }
.wr-content p { margin: 8px 0; }
.wr-content ul, .wr-content ol { margin: 8px 0; padding-left: 30px; }
.wr-content blockquote {
  margin: 10px 0; padding: 4px 16px;
  border-left: 4px solid #c8ccd1; color: #404244;
}
.wr-content pre {
  background: var(--wr-bg); border: 1px solid var(--wr-border);
  padding: 10px; overflow-x: auto; font-size: 13px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}
.wr-content code {
  background: var(--wr-bg); border-radius: 2px; padding: 1px 4px;
  font-size: 0.92em;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}
.wr-content pre code { background: none; padding: 0; }
/* MediaWiki 常见结构 */
.wr-content .mw-editsection { display: none; }
.wr-content .mw-empty-elt { display: none; }
.wr-content .hatnote {
  font-size: 0.9em; color: #54595d; padding: 4px 0;
}
.wr-content .dablink { font-size: 0.9em; color: #54595d; }
.wr-content table { border-collapse: collapse; }
.wr-content table.wikitable {
  margin: 1em 0; border: 1px solid var(--wr-border);
  background: #fff;
}
.wr-content table.wikitable > tr > th,
.wr-content table.wikitable > * > tr > th {
  background: #eaecf0; border: 1px solid var(--wr-border); padding: 4px 8px;
  text-align: center;
}
.wr-content table.wikitable > tr > td,
.wr-content table.wikitable > * > tr > td {
  border: 1px solid var(--wr-border); padding: 4px 8px;
}
.wr-content .infobox,
.wr-content .Infobox {
  float: right; clear: right; width: 300px;
  border: 1px solid var(--wr-border); background: var(--wr-bg);
  margin: 0 0 0.8em 1.2em; padding: 6px 8px; font-size: 0.92em;
}
.wr-content .infobox caption,
.wr-content .Infobox caption { font-weight: 700; padding: 4px; }
.wr-content .infobox th, .wr-content .infobox td,
.wr-content .Infobox th, .wr-content .Infobox td { padding: 3px 6px; vertical-align: top; }
.wr-content .infobox th,
.wr-content .Infobox th { text-align: left; white-space: nowrap; }
.wr-content .thumb {
  float: right; clear: right; margin: 0 0 1em 1.2em;
}
.wr-content .thumbinner {
  border: 1px solid #c8ccd1; background: var(--wr-bg);
  padding: 4px; font-size: 0.9em; text-align: center;
  overflow: hidden;
}
.wr-content .thumbcaption { font-size: 0.9em; padding: 4px 2px 0; text-align: left; }
.wr-content .floatright { float: right; margin: 0 0 1em 1.2em; }
.wr-content .floatleft { float: left; margin: 0 1.2em 1em 0; }
.wr-content .center { text-align: center; }
.wr-content .toc {
  display: inline-block; border: 1px solid var(--wr-border);
  background: var(--wr-bg); padding: 8px 14px; margin: 1em 0;
  font-size: 0.95em;
}
.wr-content .toc .toctitle { font-weight: 700; text-align: center; }
.wr-content .toc ul { list-style: none; padding-left: 20px; margin: 4px 0; }
.wr-content .toc ul li { margin: 2px 0; }
.wr-content ol.references { font-size: 0.9em; }
.wr-content .mw-references-wrap { font-size: 0.92em; }
.wr-content sup.reference { font-size: 0.75em; white-space: nowrap; }
.wr-content .navbox {
  border: 1px solid var(--wr-border); background: var(--wr-bg);
  margin: 12px 0; font-size: 0.92em; width: 100%;
}
.wr-content .navbox-title { background: #ddeef7; text-align: center; padding: 4px; }
.wr-content .navbox-group { background: #ddeef7; padding: 4px 8px; white-space: nowrap; }
.wr-content .navbox-list { padding: 4px 8px; }
.wr-content .gallery { margin: 8px 0; }
.wr-content .gallerybox {
  display: inline-block; vertical-align: top; padding: 4px;
}
.wr-content .gallerytext { font-size: 0.85em; text-align: center; }
.wr-content .mw-kartographer-container, .wr-content .mw-kartographer-map { max-width: 100%; }
.wr-content table { max-width: 100%; overflow: hidden; }
.wr-content td, .wr-content th { overflow-wrap: anywhere; }
.wr-footer {
  border-top: 1px solid var(--wr-border); background: var(--wr-bg);
  padding: 8px 20px; font-size: 12px; color: #72777d;
}
.wr-content .mw-highlight { background: var(--wr-bg); border: 1px solid var(--wr-border); padding: 8px; overflow-x: auto; }
"""


def build_article_html(
    article_html: str,
    *,
    title: str = "",
    conn: Optional[WikiConnection] = None,
    site_name: str = "",
    width: int = 860,
    lang: str = "zh",
    footer_note: str = "",
) -> str:
    """组装完整 HTML 文档。"""
    site = site_name or (conn.site_name if conn else "") or ""
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<title>{_escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wr-header">
  <span class="wr-title">{_escape(title)}</span>
  <span class="wr-sitename">{_escape(site)}</span>
</div>
<div class="wr-content" style="width:{int(width)}px; max-width:100%;">
{article_html}
</div>
{('<div class="wr-footer">' + _escape(footer_note) + '</div>') if footer_note else ''}
</body>
</html>"""


def _escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
