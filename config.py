"""
B站哨兵系统 — 全局配置 (兼容性重导出)

保留此文件以确保旧代码 from config import XYZ 继续工作。
新代码建议直接 from config.base_config import ...
参考: MediaCrawler 配置架构
"""

# 从新配置包重导出所有内容（向后兼容）
from config.base_config import *
from config.db_config import *
from config.crawler_config import *

# 显式重新导出以保持 IDE 友好
DEFAULT_WEIGHTS = base_config.DEFAULT_WEIGHTS
RISK_HIGH = base_config.RISK_HIGH
RISK_MEDIUM = base_config.RISK_MEDIUM
