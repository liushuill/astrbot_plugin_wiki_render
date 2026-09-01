"""命令解析（~wiki 前缀与子命令分发）单元测试。"""

from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_wiki_render.main import CMD_RE, SUBCOMMANDS


def match(text):
    m = CMD_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).strip()


def test_basic_match():
    assert match("~wiki 地球") == "地球"
    assert match("~wiki 地球 火星") == "地球 火星"
    assert match("~wiki") == ""
    assert match("~wiki  ") == ""


def test_case_and_fullwidth():
    assert match("~WIKI 地球") == "地球"
    assert match("～wiki 地球") == "地球"
    assert match("~Wiki 地球") == "地球"


def test_no_match():
    assert match("wiki 地球") is None
    assert match("~wik 地球") is None
    assert match("~wikis 地球") is None
    assert match("你好 ~wiki 地球") is None  # 前缀必须从开头匹配
    assert match("") is None


def test_subcommand_tokens():
    assert match("~wiki set https://zh.wikipedia.org").split(None, 1)[0] == "set"
    assert match("~wiki search 地球 大气层").split(None, 1)[0] == "search"
    assert match("~wiki rc").lower() in ("rc",)
    assert match("~wiki recentchanges").lower() in ("recentchanges",)


def test_subcommand_set():
    for s in ("set", "unset", "status", "iw", "search", "id", "random", "rc", "recentchanges", "help"):
        assert s in SUBCOMMANDS


def test_page_with_spaces():
    assert match("~wiki 地球 大气层") == "地球 大气层"


def test_section_syntax():
    assert match("~wiki 地球#历史") == "地球#历史"
