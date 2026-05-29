"""
代理IP池管理器

参考: MediaCrawler proxy/proxy_ip_pool.py

功能:
- 从供应商获取一批代理IP
- 维护代理池队列
- 验证代理可用性
- 失效代理自动刷新
- 低于阈值自动补充
"""

import asyncio
import logging
import time
from queue import Queue
from typing import Dict, List, Optional

from proxy.base_proxy import BaseProxy

logger = logging.getLogger(__name__)


class ProxyIPPool:
    """
    代理IP池管理器。

    设计要点（对齐 MediaCrawler）:
    1. 队列式代理池，先进先出轮询
    2. 代理验证机制，过滤不可用IP
    3. 失效自动刷新
    4. 低于阈值自动补充
    """

    def __init__(
        self,
        provider: BaseProxy,
        pool_count: int = 5,
        min_pool_count: int = 2,
        validate_timeout: int = 5,
        max_retry_per_proxy: int = 3,
    ):
        """
        Args:
            provider: 代理供应商实例
            pool_count: 代理池目标大小
            min_pool_count: 最小代理数，低于此值自动补充
            validate_timeout: 代理验证超时(秒)
            max_retry_per_proxy: 单个代理最大重试次数
        """
        self._provider = provider
        self._pool_count = pool_count
        self._min_pool_count = min_pool_count
        self._validate_timeout = validate_timeout
        self._max_retry_per_proxy = max_retry_per_proxy

        self._pool: Queue = Queue()
        self._failed_count: Dict[str, int] = {}  # 代理失败计数
        self._lock = asyncio.Lock()

    async def initialize(self) -> int:
        """
        初始化代理池，预填充代理。

        Returns:
            成功获取的有效代理数量
        """
        logger.info(
            f"初始化代理池: provider={self._provider.provider_name}, "
            f"target_count={self._pool_count}"
        )
        return await self._refill()

    async def get_proxy(self) -> Optional[Dict[str, str]]:
        """
        从代理池获取一个可用代理。

        如果池为空，尝试自动补充。

        Returns:
            代理字典，或 None（无可用的代理）
        """
        async with self._lock:
            # 检查池是否需补充
            if self._pool.qsize() < self._min_pool_count:
                await self._refill()

            if self._pool.empty():
                logger.warning("代理池已空，无法获取代理")
                return None

            proxy = self._pool.get()
            proxy_key = self._proxy_key(proxy)

            # 检查失败次数
            if self._failed_count.get(proxy_key, 0) >= self._max_retry_per_proxy:
                logger.debug(f"代理 {proxy_key} 已达最大重试次数，丢弃")
                return await self.get_proxy()  # 递归获取下一个

            return proxy

    async def refresh_proxy(self, bad_proxy: Dict[str, str]) -> None:
        """
        标记代理失效并刷新。

        参考 MediaCrawler 的 refresh_proxy() 模式：失效代理被丢弃，池自动补充。

        Args:
            bad_proxy: 失效的代理字典
        """
        proxy_key = self._proxy_key(bad_proxy)
        self._failed_count[proxy_key] = self._failed_count.get(proxy_key, 0) + 1

        logger.debug(
            f"代理 {proxy_key} 失效 "
            f"(失败 {self._failed_count[proxy_key]}/{self._max_retry_per_proxy})"
        )

        async with self._lock:
            if self._pool.qsize() < self._min_pool_count:
                await self._refill()

    async def _refill(self) -> int:
        """
        从供应商补充代理到池中。

        Returns:
            成功添加的有效代理数量
        """
        try:
            proxies = await self._provider.get_proxies(
                count=self._pool_count - self._pool.qsize()
            )
        except Exception as e:
            logger.error(f"从供应商获取代理失败: {e}")
            return 0

        valid_count = 0
        for proxy in proxies:
            proxy_key = self._proxy_key(proxy)
            if self._failed_count.get(proxy_key, 0) >= self._max_retry_per_proxy:
                continue

            # 验证代理
            if await self._provider.validate_proxy(proxy, self._validate_timeout):
                self._pool.put(proxy)
                valid_count += 1
            else:
                logger.debug(f"代理 {proxy_key} 验证失败，跳过")

        if valid_count > 0:
            logger.info(
                f"代理池补充完成: {valid_count} 个有效代理, "
                f"当前池大小={self._pool.qsize()}, "
                f"来源={self._provider.provider_name}"
            )

        return valid_count

    @property
    def pool_size(self) -> int:
        """当前代理池大小"""
        return self._pool.qsize()

    @property
    def provider_name(self) -> str:
        """代理供应商名称"""
        return self._provider.provider_name

    @staticmethod
    def _proxy_key(proxy: Dict[str, str]) -> str:
        """生成代理唯一标识"""
        http = proxy.get("http", proxy.get("https", ""))
        return http.replace("http://", "").replace("https://", "")
