"""
Redis分布式缓存

参考: MediaCrawler cache/redis_cache.py

复用项目现有 Redis (db=1)，提供分布式缓存能力。
适合多进程/多机器共享的去重和缓存场景。
"""

import json
import time
from typing import Any, Optional

import redis

from cache.abs_cache import AbstractCache


class RedisCache(AbstractCache):
    """
    Redis 分布式缓存。

    复用项目现有 Redis 连接 (db=1)，
    所有 Key 使用统一前缀避免冲突。

    适用场景:
    - 多进程/分布式爬虫的去重
    - 跨会话的持久化缓存
    - 大规模数据缓存

    限制:
    - 需要 Redis 服务运行
    - 网络延迟比本地缓存高
    """

    KEY_PREFIX = "bilibili_sentinel:cache:"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 1,
        password: str = "",
        default_ttl: int = 3600,
    ):
        self._default_ttl = default_ttl

        self._client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )

    def _make_key(self, key: str) -> str:
        """生成带前缀的缓存键"""
        return f"{self.KEY_PREFIX}{key}"

    async def get(self, key: str) -> Optional[Any]:
        """获取缓存值，自动反序列化JSON"""
        full_key = self._make_key(key)
        try:
            value = self._client.get(full_key)
            if value is None:
                return None
            return json.loads(value)
        except (json.JSONDecodeError, redis.RedisError):
            return None

    async def set(self, key: str, value: Any, ttl: int = None) -> None:
        """设置缓存值，自动序列化JSON"""
        if ttl is None:
            ttl = self._default_ttl

        full_key = self._make_key(key)
        try:
            serialized = json.dumps(value, ensure_ascii=False)
            self._client.setex(full_key, ttl, serialized)
        except redis.RedisError:
            pass

    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        full_key = self._make_key(key)
        try:
            return bool(self._client.exists(full_key))
        except redis.RedisError:
            return False

    async def delete(self, key: str) -> None:
        """删除缓存键"""
        full_key = self._make_key(key)
        try:
            self._client.delete(full_key)
        except redis.RedisError:
            pass

    async def clear(self) -> None:
        """清空所有哨兵系统的缓存"""
        try:
            pattern = f"{self.KEY_PREFIX}*"
            cursor = 0
            while True:
                cursor, keys = self._client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except redis.RedisError:
            pass

    async def size(self) -> int:
        """缓存条目数（不精确，仅估算）"""
        try:
            pattern = f"{self.KEY_PREFIX}*"
            cursor = 0
            count = 0
            while True:
                cursor, keys = self._client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                count += len(keys)
                if cursor == 0:
                    break
            return count
        except redis.RedisError:
            return 0

    def get_sync(self, key: str) -> Optional[Any]:
        """同步获取（兼容Scrapy同步Pipeline）"""
        full_key = self._make_key(key)
        try:
            value = self._client.get(full_key)
            if value is None:
                return None
            return json.loads(value)
        except (json.JSONDecodeError, redis.RedisError):
            return None

    def set_sync(self, key: str, value: Any, ttl: int = None) -> None:
        """同步设置（兼容Scrapy同步Pipeline）"""
        if ttl is None:
            ttl = self._default_ttl
        full_key = self._make_key(key)
        try:
            serialized = json.dumps(value, ensure_ascii=False)
            self._client.setex(full_key, ttl, serialized)
        except redis.RedisError:
            pass

    def close(self):
        """关闭Redis连接"""
        try:
            self._client.close()
        except Exception:
            pass
