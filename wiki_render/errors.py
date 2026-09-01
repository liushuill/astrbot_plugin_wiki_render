"""wiki-render 业务异常定义。"""


class WikiRenderError(Exception):
    """所有 wiki-render 异常的基类。"""


class WikiUnavailableError(WikiRenderError):
    """站点无法解析/连接（非 MediaWiki、网络不通、超时等）。"""


class WikiAPIError(WikiRenderError):
    """MediaWiki API 返回错误或请求失败。"""


class PageNotFoundError(WikiRenderError):
    """页面不存在。"""


class InvalidTitleError(WikiRenderError):
    """标题非法。"""


class SectionNotFoundError(WikiRenderError):
    """指定的章节不存在。"""


class RenderError(WikiRenderError):
    """渲染失败。"""


class RendererUnavailableError(RenderError):
    """Playwright / Chromium 不可用。"""


class NotBoundError(WikiRenderError):
    """未绑定 wiki。"""
