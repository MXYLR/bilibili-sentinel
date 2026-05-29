"""
本地内存缓存（LRU淘汰策略）

参考: MediaCrawler cache/local_cache.py

使用 OrderedDict 实现 LRU（最近最少使用）缓存。
适合单进程场景的快速去重和缓存。
"""

import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Optional

from cache.abs_cache import AbstractCache


class LocalCache(AbstractCache):
    """
    本地内存缓存，LRU淘汰策略。

    Features:
    - 线程安全（Lock保护）
    - TTL支持
    - 自动LRU淘汰
    - 零外部依赖

    适用场景:
    - 单进程内的请求去重
    - 临时结果缓存
    - 高频读取的小数据

    限制:
    - 进程重启后丢失
    - 内存占用随条目增加而增长
    """

    def __init__(self, max_size: int = 10000, default_ttl: int = 3600):
        """
        Args:
            max_size: 最大缓存条目数
            default_ttl: 默认TTL(秒)
        """
        self._max_size = max_size
        self._default_ttl = default_ttl

        self._cache: OrderedDict[str, tuple] = OrderedDict()  # {key: (value, expire_at)}
        self._lock = Lock()

        # 统计
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """获取缓存值，过期返回None"""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            value, expire_at = self._cache[key]

            # 检查过期
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                self._misses += 1
                return None

            # 移到队尾（最近使用）
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    async def set(self, key: str, value: Any, ttl: int = None) -> None:
        """设置缓存值"""
        if ttl is None:
            ttl = self._default_ttl

        expire_at = time.time() + ttl if ttl > 0 else 0

        with self._lock:
            # LRU: 超出容量时淘汰最旧的条目
            if len(self._cache) >= self._max_size and key not in self._cache:
                self._cache.popitem(last=False)  # 淘汰最久未使用的

            self._cache[key] = (value, expire_at)
            self._cache.move_to_end(key)

    async def exists(self, key: str) -> bool:
        """检查键是否存在且未过期"""
        value = await self.get(key)
        return value is not None

    async def delete(self, key: str) -> None:
        """删除缓存键"""
        with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """清空所有缓存"""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    async def size(self) -> int:
        """当前缓存条目数"""
        with self._lock:
            return len(self._cache)

    def get_sync(self, key: str) -> Optional[Any]:
        """同步获取（兼容Scrapy同步Pipeline）"""
        if key not in self._cache:
            self._misses += 1
            return None

        value, expire_at = self._cache[key]
        if expire_at > 0 and time.time() > expire_at:
            del self._cache[key]
            self._misses += 1
            return None

        self._cache.move_to_end(key)
        self._hits += 1
        return value

    def set_sync(self, key: str, value: Any, ttl: int = None) -> None:
        """同步设置（兼容Scrapy同步Pipeline）"""
        if ttl is None:
            ttl = self._default_ttl
        expire_at = time.time() + ttl if ttl > 0 else 0

        if len(self._cache) >= self._max_size and key not in self._cache:
            self._cache.popitem(last=False)

        self._cache[key] = (value, expire_at)
        self._cache.move_to_end(key)

    @property
    def hit_rate(self) -> float:
        """缓存命中率"""
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def stats(self) -> dict:
        """缓存统计"""
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self.hit_rate:.1%}",
        }
