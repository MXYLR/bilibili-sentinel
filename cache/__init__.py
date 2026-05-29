"""
缓存抽象层

参考: MediaCrawler cache/ 设计

功能:
- 抽象缓存基类 (AbstractCache)
- 本地内存LRU缓存 (LocalCache)
- Redis分布式缓存 (RedisCache) — 需要 redis 包
"""

from cache.abs_cache import AbstractCache
from cache.local_cache import LocalCache

# RedisCache 懒加载（避免 redis 未安装时阻塞整个模块）
try:
    from cache.redis_cache import RedisCache
except ImportError:
    RedisCache = None  # redis 未安装时不可用
