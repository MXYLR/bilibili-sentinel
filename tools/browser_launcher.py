"""
Playwright 浏览器启动器

参考: MediaCrawler tools/browser_launcher.py + tools/cdp_browser.py

支持两种模式:
1. 标准模式: 启动新的 Chromium 实例
2. CDP模式: 连接已有 Chrome 浏览器（复用登录态，降低风控）
"""

import logging
import os
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class BrowserLauncher:
    """
    Playwright 浏览器启动器。

    参考 MediaCrawler 的 launch_browser / launch_browser_with_cdp 方法。

    设计特点:
    1. 标准模式注入 stealth.js 反检测脚本
    2. CDP 模式复用已有 Chrome 登录态
    3. 支持代理配置
    4. 自动降级：CDP失败 → 标准模式
    """

    def __init__(
        self,
        headless: bool = True,
        user_agent: Optional[str] = None,
        stealth_js_path: Optional[str] = None,
    ):
        """
        Args:
            headless: 是否无头模式
            user_agent: 自定义UA
            stealth_js_path: stealth.min.js 路径
        """
        self._headless = headless
        self._user_agent = user_agent
        self._stealth_js_path = stealth_js_path

        self._browser = None
        self._context = None
        self._playwright = None

    async def launch_standard(
        self,
        proxy: Optional[Dict] = None,
    ) -> "BrowserContext":
        """
        标准模式启动浏览器（参考 MediaCrawler launch_browser）。

        Args:
            proxy: Playwright proxy 配置 {"server": "http://ip:port"}

        Returns:
            BrowserContext 实例
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright 未安装。请运行: pip install playwright && playwright install chromium"
            )

        self._playwright = await async_playwright().start()

        launch_args = {
            "headless": self._headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }

        if proxy:
            launch_args["proxy"] = proxy

        self._browser = await self._playwright.chromium.launch(**launch_args)

        context_args = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
        }

        if self._user_agent:
            context_args["user_agent"] = self._user_agent

        self._context = await self._browser.new_context(**context_args)

        # 注入 stealth.js（参考 MediaCrawler add_init_script）
        if self._stealth_js_path and os.path.exists(self._stealth_js_path):
            await self._context.add_init_script(path=self._stealth_js_path)
            logger.info("Stealth.js injected successfully")

        logger.info(f"Browser launched (standard mode, headless={self._headless})")
        return self._context

    async def launch_cdp(
        self,
        cdp_url: str = "http://localhost:9222",
        proxy: Optional[Dict] = None,
    ) -> "BrowserContext":
        """
        CDP 模式连接已有 Chrome（参考 MediaCrawler launch_browser_with_cdp）。

        需要先启动 Chrome 调试模式:
            chrome.exe --remote-debugging-port=9222

        Args:
            cdp_url: Chrome DevTools Protocol URL
            proxy: Playwright proxy 配置

        Returns:
            BrowserContext 实例

        Raises:
            ConnectionError: CDP连接失败
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright 未安装。请运行: pip install playwright"
            )

        self._playwright = await async_playwright().start()

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            logger.error(f"CDP connection failed: {e}")
            raise ConnectionError(f"无法连接到CDP浏览器 ({cdp_url}): {e}")

        # CDP模式下使用已有浏览器的默认context
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._browser.new_context()

        logger.info(f"CDP browser connected: {cdp_url}")
        return self._context

    async def launch(
        self,
        mode: str = "standard",
        cdp_url: str = "http://localhost:9222",
        proxy: Optional[Dict] = None,
    ) -> "BrowserContext":
        """
        统一启动入口，支持自动降级（参考 MediaCrawler 降级模式）。

        Args:
            mode: "standard" | "cdp"
            cdp_url: CDP模式下的调试URL
            proxy: 代理配置

        Returns:
            BrowserContext 实例
        """
        if mode == "cdp":
            try:
                return await self.launch_cdp(cdp_url, proxy)
            except ConnectionError as e:
                logger.warning(f"CDP mode failed, falling back to standard: {e}")
                return await self.launch_standard(proxy)
        else:
            return await self.launch_standard(proxy)

    async def close(self):
        """关闭浏览器（参考 MediaCrawler cleanup）"""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        logger.info("Browser closed")


# ---- 辅助函数 ----

async def fetch_with_playwright(
    url: str,
    context: "BrowserContext",
    timeout: int = 15000,
) -> dict:
    """
    使用 Playwright 获取 B站 API JSON 响应。

    当纯API请求被封禁时，使用浏览器作为降级方案。
    参考 MediaCrawler 的浏览器请求模式。

    Args:
        url: B站API URL
        context: Playwright BrowserContext
        timeout: 超时毫秒

    Returns:
        解析后的JSON dict
    """
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="networkidle", timeout=timeout)
        if response and response.ok:
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return await response.json()
            else:
                body = await page.content()
                logger.warning(f"Non-JSON response from Playwright: {body[:200]}")
                return {"code": -1, "message": "Non-JSON response"}
        else:
            status = response.status if response else "N/A"
            return {"code": -1, "message": f"HTTP {status}"}
    except Exception as e:
        logger.error(f"Playwright fetch failed: {e}")
        return {"code": -1, "message": str(e)}
    finally:
        await page.close()
