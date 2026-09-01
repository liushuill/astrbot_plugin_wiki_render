"""wiki-render 业务逻辑包。"""

from .api_client import (
    DEFAULT_UA,
    PageState,
    ParsedPage,
    WikiClient,
    WikiConnection,
    resolve_endpoint,
)
from .errors import (
    InvalidTitleError,
    NotBoundError,
    PageNotFoundError,
    RenderError,
    RendererUnavailableError,
    SectionNotFoundError,
    WikiAPIError,
    WikiRenderError,
    WikiUnavailableError,
)

__all__ = [
    "PageState",
    "ParsedPage",
    "WikiClient",
    "WikiConnection",
    "resolve_endpoint",
    "InvalidTitleError",
    "NotBoundError",
    "PageNotFoundError",
    "RenderError",
    "RendererUnavailableError",
    "SectionNotFoundError",
    "WikiAPIError",
    "WikiRenderError",
    "WikiUnavailableError",
]
