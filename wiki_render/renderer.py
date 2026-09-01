"""Playwright 渲染引擎：浏览器进程级单例 + 管线 A（HTML 渲染）/ 管线 B（URL 截图）。

对应需求.md §4.4：
- 管线 A：parse API + 自建模板 -> page.set_content -> 强制加载懒加载图片 -> 元素截图
- 管线 B：真实 URL -> 注入 CSS 隐藏站点导航 -> 内容容器元素截图（特殊页面/章节）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

from . import template as tmpl
from .errors import RenderError, RendererUnavailableError

try:
    from playwright.async_api import async_playwright

    HAS_PLAYWRIGHT = True
except Exception:  # pragma: no cover - 依赖缺失场景
    HAS_PLAYWRIGHT = False

# 强制图片立即加载（去除原生懒加载）
_FORCE_IMAGES_JS = """
() => {
  document.querySelectorAll('img, video, iframe').forEach(el => {
    el.loading = 'eager';
    el.decoding = 'auto';
    if (el.tagName === 'IMG' && !el.src && el.dataset && el.dataset.src) {
      el.src = el.dataset.src;
    }
  });
}
"""

# 滚动页面触发懒加载
_SCROLL_JS = """
async () => {
  const step = 700;
  const max = Math.min(document.body.scrollHeight, 200000);
  for (let y = 0; y < max; y += step) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 30));
  }
  window.scrollTo(0, 0);
}
"""

# 特殊页面截图时隐藏的站点导航元素（管线 B / 原生渲染去顶栏）
_DEFAULT_HIDE_SELECTORS = [
    "#mw-navigation",
    "#mw-panel",
    "#mw-head",
    "#mw-page-base",
    "#footer",
    "#p-personal",
    "#p-cactions",
    "#p-namespaces",
    ".vector-header",
    ".vector-sticky-header",
    ".vector-page-titlebar",
    ".vector-page-toolbar",
    ".mw-indicators",
    "#siteSub",
    "#contentSub",
    ".printfooter",
    "#catlinks",
    ".mw-editTools",
    ".mw-jump-link",
    ".mw-article-toolbar",
    "#p-logo",
    ".mw-workspace-container",
    ".toc-sidebar",
    "#mw-relatedpages",
]

_CONTENT_SELECTORS = [
    "#mw-content-text",
    ".mw-parser-output",
    "#content",
    "#mw-content",
    "article",
]

# 横屏/竖屏的 UA 与视口
_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)


def _viewport_for(screen: str, landscape_width: int, portrait_width: int) -> tuple[int, str]:
    if screen == "portrait":
        return portrait_width, _MOBILE_UA
    return landscape_width, _PC_UA


class Renderer:
    """Playwright 渲染器。线程安全由外部渲染队列保证（见 main.py）。"""

    def __init__(
        self,
        cache_dir: Path,
        *,
        width: int = 860,
        landscape_width: int = 1280,
        portrait_width: int = 420,
        device_scale_factor: int = 2,
        timeout: float = 30.0,
        max_height: int = 15000,
        screenshot_type: str = "png",
        resource_wait_ms: int = 5000,
        content_padding: int = 16,
    ) -> None:
        self.cache_dir = cache_dir
        self.width = width
        self.landscape_width = landscape_width
        self.portrait_width = portrait_width
        self.scale = device_scale_factor
        self.resource_wait_ms = resource_wait_ms
        self.content_padding = content_padding
        self.timeout = timeout
        self.max_height = max_height
        self.screenshot_type = screenshot_type
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._broken = False

    # ------------------------------------------------------------------ #
    # 浏览器生命周期
    # ------------------------------------------------------------------ #
    async def _get_browser(self):
        if self._broken:
            raise RendererUnavailableError(
                "Playwright/Chromium 不可用：请确认已执行 `pip install playwright` 和 "
                "`playwright install chromium`（浏览器未安装或启动失败）"
            )
        async with self._lock:
            if self._browser is None:
                if not HAS_PLAYWRIGHT:
                    self._broken = True
                    raise RendererUnavailableError(
                        "未检测到 playwright 依赖，请安装：pip install playwright && playwright install chromium"
                    )
                try:
                    self._pw = await async_playwright().start()
                    self._browser = await self._pw.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                        ],
                    )
                except Exception as e:
                    self._broken = True
                    if self._pw is not None:
                        try:
                            await self._pw.stop()
                        except Exception:
                            pass
                    raise RendererUnavailableError(f"Chromium 启动失败: {e}") from e
        return self._browser

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            self._broken = False

    # ------------------------------------------------------------------ #
    # 公共渲染入口
    # ------------------------------------------------------------------ #
    async def render_article(
        self,
        article_html: str,
        *,
        title: str = "",
        site_name: str = "",
        lang: str = "zh",
        footer_note: str = "",
        width: Optional[int] = None,
        screen: str = "landscape",
    ) -> Path:
        """管线 A：渲染文章 HTML 为图片，返回本地 PNG 路径。"""
        browser = await self._get_browser()
        w = width or self.width
        full_html = tmpl.build_article_html(
            article_html,
            title=title,
            site_name=site_name,
            width=w,
            lang=lang,
            footer_note=footer_note,
        )
        vw, ua = _viewport_for(screen, self.landscape_width, self.portrait_width)
        context = await browser.new_context(
            device_scale_factor=self.scale,
            viewport={"width": vw, "height": 900},
            user_agent=ua,
            locale=lang,
        )
        try:
            page = await context.new_page()
            try:
                await page.set_content(full_html, wait_until="load", timeout=min(12000, self.timeout * 1000))
            except Exception:
                pass  # 图片加载慢时忽略，交由后续强制加载
            return await self._screenshot(page, "body", w)
        except RenderError:
            raise
        except Exception as e:
            raise RenderError(f"渲染失败: {e}") from e
        finally:
            await context.close()

    async def render_url(
        self,
        url: str,
        *,
        content_selector: Optional[str] = None,
        hide_selectors: Optional[list[str]] = None,
        width: Optional[int] = None,
        footer_note: str = "",
        screen: str = "landscape",
        cookies: Optional[list[dict]] = None,
        extra_css: Optional[str] = None,
    ) -> Path:
        """管线 B / 原生渲染：打开真实 URL 并对内容容器截图，返回本地 PNG 路径。"""
        browser = await self._get_browser()
        vw, ua = _viewport_for(screen, self.landscape_width, self.portrait_width)
        w = width or vw
        context = await browser.new_context(
            device_scale_factor=self.scale,
            viewport={"width": vw, "height": 900},
            user_agent=ua,
            locale="zh-CN",
        )
        try:
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except Exception:
                    pass
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            except Exception as e:
                raise RenderError(f"打开页面失败: {url} ({e})") from e
            # 隐藏站点导航，只留内容
            hides = hide_selectors or _DEFAULT_HIDE_SELECTORS
            css = " ".join(f"{s}{{display:none!important}}" for s in hides)
            try:
                await page.add_style_tag(content=css)
            except Exception:
                pass
            # 注入缓存的核心 CSS（可选，加速/兜底样式）
            if extra_css:
                try:
                    await page.add_style_tag(content=extra_css)
                except Exception:
                    pass
            # 探测内容容器
            selector = content_selector
            if not selector:
                for s in _CONTENT_SELECTORS:
                    try:
                        if await page.query_selector(s):
                            selector = s
                            break
                    except Exception:
                        continue
            if not selector:
                selector = "body"
            # 给截图容器加边距，避免文字贴边（用户可配 content_padding）
            if selector != "body" and self.content_padding > 0:
                try:
                    await page.evaluate(
                        """(sel, pad) => {
                            const el = document.querySelector(sel);
                            if (el) {
                                el.style.padding = pad + 'px ' + Math.round(pad * 1.25) + 'px ' + Math.round(pad * 1.5) + 'px';
                            }
                        }""",
                        selector,
                        self.content_padding,
                    )
                except Exception:
                    pass
            return await self._screenshot(page, selector, w)
        except RenderError:
            raise
        except Exception as e:
            raise RenderError(f"渲染失败: {e}") from e
        finally:
            await context.close()

    # ------------------------------------------------------------------ #
    # 内部截图
    # ------------------------------------------------------------------ #
    async def _screenshot(self, page, selector: str, width: int) -> Path:
        try:
            await page.evaluate(_FORCE_IMAGES_JS)
        except Exception:
            pass
        try:
            await page.evaluate(_SCROLL_JS)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=min(5000, self.timeout * 1000))
        except Exception:
            pass
        await page.wait_for_timeout(600)
        # 等待图片资源加载完成（超时上限后返回当前渲染结果，避免图片多时截图残缺）
        try:
            await page.evaluate(
                """async (timeoutMs) => {
                    const imgs = Array.from(document.images).filter(i => !i.complete);
                    if (!imgs.length) return;
                    await Promise.race([
                        Promise.all(imgs.map(i => new Promise(res => {
                            if (i.complete) return res();
                            i.addEventListener('load', res, { once: true });
                            i.addEventListener('error', res, { once: true });
                        }))),
                        new Promise(res => setTimeout(res, timeoutMs)),
                    ]);
                }""",
                self.resource_wait_ms,
            )
        except Exception:
            pass

        el = await page.query_selector(selector)
        if el is None:
            el = await page.query_selector("body")
        if el is None:
            raise RenderError("页面无可截图内容")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out = self.cache_dir / f"wr_{int(time.time())}_{uuid.uuid4().hex[:10]}.{self.screenshot_type}"

        opts: dict = {"path": str(out), "type": self.screenshot_type}
        if self.screenshot_type == "jpeg":
            opts["quality"] = 85
        try:
            height = await el.evaluate("e => e.scrollHeight")
        except Exception:
            height = 0
        if self.max_height and height > self.max_height:
            opts["clip"] = {
                "x": 0,
                "y": 0,
                "width": width,
                "height": self.max_height,
            }
        try:
            await el.screenshot(**opts)
        except Exception as e:
            raise RenderError(f"截图失败: {e}") from e
        if not out.exists() or out.stat().st_size == 0:
            raise RenderError("截图产物为空")
        return out
