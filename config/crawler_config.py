"""
爬虫参数配置 — MediaCrawler 风格

Scrapy 运行参数、并发控制、重试策略、休眠配置。
参考: MediaCrawler 各平台 {platform}_config.py
"""

# ============================================================
#  并发控制 (参考 MediaCrawler Semaphore 模式)
# ============================================================
DOWNLOAD_DELAY = 0.35                # 请求间隔 ~3 req/s
RANDOMIZE_DOWNLOAD_DELAY = True      # 随机化延迟避免规律性
MAX_CONCURRENCY_NUM = 5              # 最大并发请求信号量
CONCURRENT_REQUESTS = 8              # Scrapy全局并发
CONCURRENT_REQUESTS_PER_DOMAIN = 3   # 单域名并发限制

# ============================================================
#  休眠策略 (参考 MediaCrawler CRAWLER_MAX_SLEEP_SEC)
# ============================================================
CRAWLER_MAX_SLEEP_SEC = 1.5          # 关键操作后休眠(秒)
CRAWLER_PAGE_SLEEP_SEC = 2.0         # 翻页后休眠(秒)
CRAWLER_RATE_LIMIT_JITTER = 0.10     # 随机抖动幅度 ±10%

# ============================================================
#  重试与超时 (参考 MediaCrawler 自动降级模式)
# ============================================================
DOWNLOAD_TIMEOUT = 15                 # 单请求超时(秒)
RETRY_ENABLED = True                  # 是否启用重试
RETRY_TIMES = 3                       # 最大重试次数 (从2提升到3)
RETRY_HTTP_CODES = [429, 500, 502, 503, 504]

# 指数退避重试参数
RETRY_BACKOFF_BASE = 2.0              # 退避基数(秒)
RETRY_BACKOFF_MAX = 60.0              # 最大退避时间(秒)
RETRY_BACKOFF_MULTIPLIER = 2.0        # 退避乘数

# 风控处理
RATE_LIMIT_WAIT = 60                  # -412风控等待(秒)
RATE_LIMIT_MAX_RETRIES = 3            # 风控最大重试次数

# ============================================================
#  爬虫模式配置
# ============================================================
# 搜索模式: search(搜索), detail(指定视频), creator(创作者)
CRAWLER_TYPE = "search"

# 热门排行
HOT_PAGE_COUNT = 3                    # 热门爬取页数

# 评论采集限制
MAX_COMMENTS_TOTAL = 2000             # 单视频最大评论数
COMMENT_BUFFER_SIZE = 10              # 评论批量写入缓冲大小

# ============================================================
#  Scrapy 特定配置
# ============================================================
SCHEDULER_PERSIST = True              # Redis调度器持久化
SCHEDULER_IDLE_BEFORE_CLOSE = 0       # 禁用调度器自动关闭，由 spider_idle 和 max_idle_time 统一管理
REDIS_START_URLS_AS_SET = False       # 使用List而非Set (兼容Redis 3.0)

# 日志
LOG_LEVEL = "INFO"
LOG_STDOUT = True                     # 输出到stdout
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
