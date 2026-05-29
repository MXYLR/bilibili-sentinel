"""
Playwright 兜底中间件 — 当 curl_cffi 仍然返回 412 时，用真实浏览器兜底

工作原理:
1. 监控连续 412 次数（通过 spider._412_count）
2. 连续 N 次 412 后，将 spider._use_playwright = True
3. 后续请求通过 process_request 用 Playwright Chromium 发送
4. Playwright 请求完全模拟真实浏览器（TLS + JS + 指纹），412 率接近 0

优先级: 88（在 CurlCffiMiddleware=89 之前，先判断是否要用 Playwright）
          实际上应该让 CurlCffi 先尝试，失败后再用 Playwright 兜底，
          所以此中间件运行在 response 阶段（检测 412）和 process_request 阶段（切换）。
                    
更合理的架构:
  - BilibiliResponseMiddleware (550) 检测 412 → 递增 spider._412_count
  - 当 _412_count >= TRIGGER → 设置 spider._use_playwright = True
  - 下次请求时，BilibiliPlaywrightFallbackMiddleware.process_request()
    检查 spider._use_playwright，如果为 True，则用 Playwright 发送
  - 为了效率，Playwright 只用于 B站 API（不是所有请求）

优先级: 88（在 CurlCffi=89 之前，优先用 Playwright 兜底）
"""

import time
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 延迟导入 playwright，避免没装时崩溃
_PLAYWRIGHT_AVAILABLE: bool = False
try:
    from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    Browser = None
    Page = None
    BrowserContext = None


class PlaywrightSession:
    """
    单例 Playwright 会话管理器。

    特点:
    - 整个爬虫生命周期只启动一次浏览器（成本高）
    - 每个请求复用同一个 context（共享 Cookie）
    - 支持 SOCKS5 代理（通过 Clash）
    """

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._initialized = False
        self._proxies: Optional[dict] = None

    def _ensure_browser(self):
        """延迟启动 Playwright 浏览器"""
        if self._initialized:
            return

        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright not installed. "
                "pip install playwright && playwright install chromium"
            )

        from config.base_config import (
            CLASH_PROXY_ENABLED, CLASH_PROXY_URL,
        )
        from config.accounts import (
            ENABLE_PLAYWRIGHT_FALLBACK, PLAYWRIGHT_HEADLESS,
            PLAYWRIGHT_BROWSER, PLAYWRIGHT_TIMEOUT,
        )

        if not ENABLE_PLAYWRIGHT_FALLBACK:
            raise RuntimeError("Playwright fallback not enabled in config")

        proxy_config = None
        if CLASH_PROXY_ENABLED and CLASH_PROXY_URL:
            # Playwright 支持 socks5 代理
            proxy_config = {"server": CLASH_PROXY_URL}
            logger.info(f"[Playwright] Using proxy: {CLASH_PROXY_URL}")

        self._playwright = sync_playwright().start()

        launch_kwargs = {
            "headless": PLAYWRIGHT_HEADLESS,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        }
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        if PLAYWRIGHT_BROWSER == "firefox":
            self._browser = self._playwright.firefox.launch(**launch_kwargs)
        else:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)

        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        # 注入 B站 Cookie（从 Cookie 池或 cookie file）
        self._inject_cookies()

        self._page = self._context.new_page()
        self._page.set_default_timeout(PLAYWRIGHT_TIMEOUT * 1000)

        self._initialized = True
        logger.info("[Playwright] Browser started (TLS=real Chrome/FF)")

    def _inject_cookies(self):
        """向 Playwright Context 注入 B站 Cookie"""
        try:
            from config.base_config import COOKIE_FILE
            import os
            if os.path.exists(COOKIE_FILE):
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                for name, value in cookies.items():
                    try:
                        self._context.add_cookies([{
                            "name": name,
                            "value": str(value),
                            "domain": ".bilibili.com",
                            "path": "/",
                        }])
                    except Exception:
                        pass
                logger.info(
                    f"[Playwright] Injected {len(cookies)} cookies "
                    f"from {COOKIE_FILE}"
                )
        except Exception as e:
            logger.warning(f"[Playwright] Failed to inject cookies: {e}")

    def fetch_json(self, url: str, headers: Optional[dict] = None) -> dict:
        """
        用 Playwright 发送请求并解析 JSON 响应。

        方法:
        1. 用 page.goto() 访问 URL
        2. 等待页面加载完成
        3. 从 document.body.innerText 提取 JSON

        注意: B站 API 返回的是 JSON，浏览器会直接显示。
        这种方式完全模拟真实浏览器行为，412 率极低。
        """
        self._ensure_browser()

        try:
            # 设置请求头（通过 route 拦截修改）
            if headers:
                def handle_route(route, request):
                    route.continue_(headers=headers)
                self._page.route("**/*", handle_route)

            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is None:
                raise RuntimeError("Playwright: page.goto returned None")

            status = resp.status
            # 从页面提取 JSON（B站 API 直接返回 JSON 文本）
            body_text = self._page.evaluate("() => document.body.innerText")
            body_bytes = body_text.encode("utf-8")

            return {
                "status": status,
                "body": body_bytes,
                "url": self._page.url,
            }
        except Exception as e:
            logger.error(f"[Playwright] fetch_json failed: {e}")
            raise

    def close(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._initialized = False


# 全局单例
_playwright_session: Optional[PlaywrightSession] = None


def _get_playwright_session() -> PlaywrightSession:
    global _playwright_session
    if _playwright_session is None:
        _playwright_session = PlaywrightSession()
    return _playwright_session


# ============================================================
# Scrapy 中间件
# ============================================================

class BilibiliPlaywrightFallbackMiddleware:
    """
    Playwright 兜底中间件。

    触发条件: spider._use_playwright == True
    （由 BilibiliResponseMiddleware 或 CookiePoolMiddleware 设置）

    一旦触发，后续所有 B站 API 请求都用 Playwright 发送，
    不再经过 curl_cffi / Scrapy 下载器。
    """

    def __init__(self):
        self._active = False

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_request(self, request, spider):
        """
        检查是否需要用 Playwright 兜底。
        如果 spider._use_playwright == True，则用 Playwright 发送请求。
        """
        # 只在启用 Playwright fallback 时工作
        try:
            from config.accounts import ENABLE_PLAYWRIGHT_FALLBACK
            if not ENABLE_PLAYWRIGHT_FALLBACK:
                return None
        except ImportError:
            return None

        # 只处理 B站 API 请求
        if "api.bilibili.com" not in request.url:
            return None

        # 检查是否触发了 Playwright 兜底
        if not getattr(spider, "_use_playwright", False):
            return None

        import asyncio
        spider.logger.warning(
            f"[PlaywrightFallback] Taking over: {request.url[:80]}"
        )
        return asyncio.to_thread(
            self._fetch_with_playwright, request.url, request.headers,
        )

    def _fetch_with_playwright(self, url: str, scrapy_headers):
        """同步函数：用 Playwright 发送请求，返回 Scrapy Response"""
        session = _get_playwright_session()

        # 转换 headers
        headers_dict = {}
        for k, v in scrapy_headers.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v[0] if isinstance(v, (list, tuple)) else v
            val = val.decode() if isinstance(val, bytes) else str(val)
            headers_dict[key] = val

        try:
            result = session.fetch_json(url, headers_dict)

            from scrapy.http import HtmlResponse
            return HtmlResponse(
                url=result["url"],
                status=result["status"],
                body=result["body"],
                encoding="utf-8",
            )
        except Exception as e:
            from scrapy.http import HtmlResponse
            import logging
            logging.getLogger(__name__).error(
                f"[PlaywrightFallback] Failed: {e}"
            )
            return HtmlResponse(
                url=url,
                status=500,
                body=b"",
            )

    def process_response(self, request, response, spider):
        """
        Passive observer: 不再主动计数（避免与 BilibiliResponseMiddleware 双重计数）。
        Playwright 触发已统一到 BilibiliResponseMiddleware 中管理。
        此方法仅保留用于日志观测，不做任何状态修改。
        """
        return response

    @classmethod
    def shutdown(cls):
        """爬虫关闭时清理 Playwright 资源"""
        global _playwright_session
        if _playwright_session:
            _playwright_session.close()
            _playwright_session = None
