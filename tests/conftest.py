"""pytest 配置：stub 掉 astrbot 相关模块，使 main.py 可在宿主机被导入测试。

只 stub 监听器/消息组件等薄接口；业务逻辑（wiki_render 包）使用真实代码。
"""

import enum
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------- astrbot 桩 ----------
class _EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _PermissionType(enum.Flag):
    ADMIN = enum.auto()
    MEMBER = enum.auto()


def _make_filter_module():
    mod = types.ModuleType("astrbot.api.event.filter")
    mod.EventMessageType = _EventMessageType
    mod.PermissionType = _PermissionType
    mod.command = lambda *a, **k: (lambda f: f)
    mod.event_message_type = lambda *a, **k: (lambda f: f)
    mod.permission_type = lambda *a, **k: (lambda f: f)
    return mod


def _make_event_module():
    mod = types.ModuleType("astrbot.api.event")
    mod.filter = _make_filter_module()
    mod.AstrMessageEvent = object
    return mod


def _make_components_module():
    mod = types.ModuleType("astrbot.api.message_components")

    class BaseMessageComponent:
        pass

    class Plain(BaseMessageComponent):
        def __init__(self, text=""):
            self.text = text

        def __repr__(self):
            return f"Plain({self.text!r})"

    class Image(BaseMessageComponent):
        @classmethod
        def fromFileSystem(cls, path):
            return cls(path=path)

        def __init__(self, **kw):
            self.__dict__.update(kw)

    class At(BaseMessageComponent):
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Node(BaseMessageComponent):
        def __init__(self, content=None, **kw):
            self.content = content or []
            self.__dict__.update(kw)

    class Nodes(BaseMessageComponent):
        def __init__(self, nodes=None, **kw):
            self.nodes = nodes or []
            self.__dict__.update(kw)

    mod.BaseMessageComponent = BaseMessageComponent
    mod.Plain = Plain
    mod.Image = Image
    mod.At = At
    mod.Node = Node
    mod.Nodes = Nodes
    return mod


def _make_star_module():
    mod = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    mod.Context = Context
    mod.Star = Star
    return mod


def _make_path_module():
    mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    mod.get_astrbot_data_path = lambda: "/tmp/astrbot_data"
    mod.get_astrbot_plugin_data_path = lambda: "/tmp/astrbot_data/plugin_data"
    return mod


def _make_session_waiter_module():
    mod = types.ModuleType("astrbot.core.utils.session_waiter")

    class SessionController:
        def stop(self):
            pass

        def keep(self, **kw):
            pass

    class SessionFilter:
        def filter(self, event):
            return event.unified_msg_origin

    def session_waiter(timeout=30, record_history_chains=False):
        def deco(fn):
            async def wrapper(event, session_filter=None):
                # 测试桩：直接执行一次内部函数（真实实现会循环等待用户回复）
                return await fn(None, event)

            return wrapper

        return deco

    mod.SessionController = SessionController
    mod.SessionFilter = SessionFilter
    mod.session_waiter = session_waiter
    return mod


def _make_message_type_module():
    import enum

    mod = types.ModuleType("astrbot.core.platform.message_type")

    class MessageType(enum.Enum):
        GROUP_MESSAGE = "GroupMessage"
        FRIEND_MESSAGE = "FriendMessage"
        OTHER_MESSAGE = "OtherMessage"

    mod.MessageType = MessageType
    return mod


def _make_message_session_module():
    from dataclasses import dataclass

    mod = types.ModuleType("astrbot.core.platform.message_session")
    mt_mod = _make_message_type_module()

    @dataclass
    class MessageSession:
        platform_name: str
        message_type: mt_mod.MessageType
        session_id: str
        platform_id: str = None

        def __post_init__(self):
            if self.platform_id is None:
                self.platform_id = self.platform_name

        def __str__(self):
            return f"{self.platform_id}:{self.message_type.value}:{self.session_id}"

    mod.MessageSession = MessageSession
    return mod


def _install_stubs():
    if "astrbot" in sys.modules:
        return

    filter_mod = _make_filter_module()
    event_mod = _make_event_module()
    event_mod.filter = filter_mod
    comp_mod = _make_components_module()
    star_mod = _make_star_module()

    api_mod = types.ModuleType("astrbot.api")
    api_mod.event = event_mod
    api_mod.star = star_mod
    api_mod.message_components = comp_mod
    api_mod.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.api = api_mod
    astrbot_mod.logger = api_mod.logger

    path_mod = _make_path_module()
    sw_mod = _make_session_waiter_module()
    utils_mod = types.ModuleType("astrbot.core.utils")
    utils_mod.astrbot_path = path_mod
    utils_mod.session_waiter = sw_mod
    core_mod = types.ModuleType("astrbot.core")
    core_mod.utils = utils_mod

    # platform 子模块（MessageType / MessageSession）
    platform_mod = types.ModuleType("astrbot.core.platform")
    mt_mod = _make_message_type_module()
    ms_mod = _make_message_session_module()
    platform_mod.message_type = mt_mod
    platform_mod.message_session = ms_mod
    core_mod.platform = platform_mod

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.event.filter"] = filter_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.utils"] = utils_mod
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
    sys.modules["astrbot.core.utils.session_waiter"] = sw_mod
    sys.modules["astrbot.core.platform"] = platform_mod
    sys.modules["astrbot.core.platform.message_type"] = mt_mod
    sys.modules["astrbot.core.platform.message_session"] = ms_mod


def _register_plugin_package():
    """把插件根目录注册为包，使 main.py 的相对导入在测试中可用。"""
    plugin_root = Path(__file__).resolve().parent.parent  # astrbot_plugin_wiki_render/
    pkg_name = "astrbot_plugin_wiki_render"
    if pkg_name in sys.modules:
        return
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(plugin_root)]
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg


_install_stubs()
_register_plugin_package()
