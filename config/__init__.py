"""
B站哨兵系统 — 分层配置中心

参考 MediaCrawler 的配置架构设计，将单一 config.py 拆分为分层模块。

Usage:
    from config import base_config, db_config, crawler_config
    # 或使用聚合导出:
    from config import *
"""

from config.base_config import *
from config.db_config import *
from config.crawler_config import *

# 聚合水军检测权重（兼容旧代码）
DEFAULT_WEIGHTS = base_config.DEFAULT_WEIGHTS
RISK_HIGH = base_config.RISK_HIGH
RISK_MEDIUM = base_config.RISK_MEDIUM
