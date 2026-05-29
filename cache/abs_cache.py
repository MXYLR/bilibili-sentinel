"""
缓存抽象基类

参考: MediaCrawler cache/abs_cache.py

提供统一的缓存接口，支持本地缓存和 Redis 分布式缓存。
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class AbstractCache(ABC):
    """
    缓存抽象基类。

    参考 MediaCrawler 的设计:
    - get/set/exists 基本操作
    - TTL 支持
    - 批量操作接口
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """
        获取缓存值。

        Args:
            key: 缓存键

        Returns:
            缓存值，不存在或过期返回 None
        """
        pass

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """
        设置缓存值。

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 过期时间(秒)，默认1小时
        """
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """
        检查缓存键是否存在。

        Args:
            key: 缓存键

        Returns:
            True 如果存在且未过期
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> None:
        """
        删除缓存键。
        """
        pass

    async def get_or_set(self, key: str, factory, ttl: int = 3600) -> Any:
        """
        获取缓存，不存在时通过 factory 生成并缓存。

        Args:
            key: 缓存键
            factory: 生成值的异步函数 async def factory() -> Any
            ttl: 过期时间

        Returns:
            缓存值或新生成的值
        """
        value = await self.get(key)
        if value is not None:
            return value

        value = await factory()
        if value is not None:
            await self.set(key, value, ttl)
        return value

    @abstractmethod
    async def clear(self) -> None:
        """清空所有缓存"""
        pass

    @abstractmethod
    async def size(self) -> int:
        """缓存条目数"""
        pass
