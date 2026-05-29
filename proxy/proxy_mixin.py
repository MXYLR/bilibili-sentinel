"""
API客户端代理混入类

参考: MediaCrawler proxy/proxy_mixin.py

功能:
- 为API客户端提供代理获取与释放接口
- 自动处理代理失效切换
- 支持请求失败时自动换IP重试
"""

import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class ProxyRefreshMixin:
    """
    混入API客户端，提供代理自动管理能力。

    使用方式 (Mixin 模式):
        class BilibiliClient(ProxyRefreshMixin):
            def __init__(self):
                self._proxy_pool = None  # 由外部注入

    参考 MediaCrawler 设计:
    - get_proxy(): 获取可用代理
    - refresh_proxy(): 失效代理更换
    - request_with_proxy_retry(): 带代理自动切换的请求
    """

    _proxy_pool = None  # ProxyIPPool 实例，由外部注入

    def set_proxy_pool(self, pool):
        """注入代理池实例"""
        self._proxy_pool = pool

    async def get_current_proxy(self) -> Optional[Dict[str, str]]:
        """从代理池获取当前可用代理"""
        if self._proxy_pool is None:
            return None
        return await self._proxy_pool.get_proxy()

    async def mark_proxy_bad(self, proxy: Dict[str, str]) -> None:
        """标记代理失效"""
        if self._proxy_pool is not None and proxy:
            await self._proxy_pool.refresh_proxy(proxy)

    @property
    def has_proxy(self) -> bool:
        """是否已配置代理池"""
        return self._proxy_pool is not None and self._proxy_pool.pool_size > 0

    def format_proxy_for_requests(self, proxy: Dict[str, str]) -> Dict[str, str]:
        """
        将代理字典格式化为 requests 库可用格式。

        Args:
            proxy: {"http": "http://ip:port", "https": "https://ip:port"}

        Returns:
            requests proxies 参数格式
        """
        if proxy is None:
            return {}
        return {
            "http": proxy.get("http", ""),
            "https": proxy.get("https", proxy.get("http", "")),
        }

    def format_proxy_for_playwright(self, proxy: Dict[str, str]) -> Optional[Dict]:
        """
        将代理字典格式化为 Playwright 可用格式。

        Args:
            proxy: {"http": "http://ip:port"}

        Returns:
            Playwright proxy 参数格式: {"server": "http://ip:port"}
        """
        if proxy is None:
            return None
        http = proxy.get("http", "")
        if http:
            return {"server": http}
        return None
