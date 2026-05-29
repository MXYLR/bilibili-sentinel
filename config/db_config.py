"""
数据库连接配置 — MediaCrawler 风格

支持 Redis + SQLite，可扩展 MySQL/MongoDB。
参考: MediaCrawler/config/db_config.py
"""

import os
from config.base_config import DATA_DIR

# ============================================================
#  Redis 配置 (Scrapy-Redis 分布式队列)
#  db=1 与 news_crawler 的 db=0 隔离
# ============================================================
REDIS_CONFIG = {
    "host": "localhost",
    "port": 6379,
    "db": 1,
    "password": "",
    "decode_responses": True,
    "socket_timeout": 30,
    "socket_connect_timeout": 30,
}

# 兼容旧代码的扁平属性
REDIS_HOST = REDIS_CONFIG["host"]
REDIS_PORT = REDIS_CONFIG["port"]
REDIS_DB = REDIS_CONFIG["db"]
REDIS_PASSWORD = REDIS_CONFIG["password"]

# Redis Key 前缀
REDIS_VIDEO_KEY = "bilibili_crawler:start_urls"
REDIS_REQUEST_KEY = "bilibili_crawler:requests"
REDIS_DUPEFILTER_KEY = "bilibili_crawler:dupefilter"
REDIS_COMMENT_KEY = "bilibili_crawler:comment_seeds"

# ============================================================
#  SQLite 配置 (存储后端扩展)
# ============================================================
SQLITE_CONFIG = {
    "path": os.path.join(DATA_DIR, "bilibili_sentinel.db"),
    "journal_mode": "WAL",           # WAL模式提升并发写入
    "synchronous": "NORMAL",         # 平衡安全性与性能
    "cache_size": -64 * 1024,        # 64MB 缓存
}

# ============================================================
#  Scrapy-Redis 参数
# ============================================================
SCRAPY_REDIS_PARAMS = {
    "db": REDIS_DB,
    "socket_timeout": REDIS_CONFIG["socket_timeout"],
    "socket_connect_timeout": REDIS_CONFIG["socket_connect_timeout"],
}
