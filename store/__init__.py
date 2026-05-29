"""
数据存储抽象层

参考: MediaCrawler store/ + AbstractStore 设计

功能:
- 抽象存储基类 (AbstractStore)
- JSON存储实现 (JsonStore)
- SQLite存储实现 (SqliteStore)
- 存储工厂 (StoreFactory)

Usage:
    from store import StoreFactory
    
    store = StoreFactory.create("json")  # or "sqlite"
    await store.store_content(video_dict)
    await store.store_comment(comment_dict)
"""
