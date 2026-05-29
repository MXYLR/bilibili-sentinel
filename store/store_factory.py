"""
存储工厂

参考: MediaCrawler main.py CrawlerFactory / store 工厂模式

根据配置选择存储后端，支持运行时切换。

Usage:
    from store.store_factory import StoreFactory
    
    store = StoreFactory.create("json")
    # or
    store = StoreFactory.create("sqlite")
"""

from typing import Dict, Optional

from store.abstract_store import AbstractStore
from store.json_store import JsonStore
from store.sqlite_store import SqliteStore


class StoreFactory:
    """
    存储工厂（参考 MediaCrawler 各平台 StoreFactory）。

    支持的后端:
    - json: JSON文件存储（默认）
    - sqlite: SQLite数据库存储

    扩展方式: 在 STORES 字典中注册新的存储类。
    """

    STORES: Dict[str, type] = {
        "json": JsonStore,
        "sqlite": SqliteStore,
    }

    _instances: Dict[str, AbstractStore] = {}  # 单例缓存

    @classmethod
    def create(cls, store_type: str = "json", **kwargs) -> AbstractStore:
        """
        创建或获取存储实例（单例模式）。

        Args:
            store_type: 存储类型 ("json" | "sqlite")
            **kwargs: 传递给存储类的参数

        Returns:
            AbstractStore 实例

        Raises:
            ValueError: 不支持的存储类型
        """
        if store_type not in cls.STORES:
            raise ValueError(
                f"不支持的存储类型: {store_type}。"
                f"可用: {list(cls.STORES.keys())}"
            )

        # 返回缓存的单例
        cache_key = f"{store_type}_{str(kwargs)}"
        if cache_key not in cls._instances:
            store_class = cls.STORES[store_type]
            cls._instances[cache_key] = store_class(**kwargs)

        return cls._instances[cache_key]

    @classmethod
    def register(cls, store_type: str, store_class: type):
        """注册新的存储类型（扩展点）"""
        if not issubclass(store_class, AbstractStore):
            raise TypeError(f"{store_class} 必须继承 AbstractStore")
        cls.STORES[store_type] = store_class

    @classmethod
    def clear_cache(cls):
        """清除单例缓存"""
        cls._instances.clear()
