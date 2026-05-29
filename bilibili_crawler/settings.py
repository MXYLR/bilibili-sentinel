"""
B站爬虫 Scrapy 配置 (Scrapy-Redis 分布式模式)

参考 MediaCrawler 的分层配置架构，从 config/ 包统一读取参数。

Key features:
1. JSON API mode (no HTML parsing needed)
2. B站-specific headers (Referer required)
3. Conservative rate limiting (3 req/s)
4. Redis db=1 isolation from news_crawler's db=0
5. 可插拔代理中间件、缓存层
"""

import sys
import os

# Add project root to path so config package can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.base_config import (
    BILIBILI_REFERER, LOG_DIR, COMMENT_PAGE_SIZE,
    MAX_COMMENT_PAGES, MAX_SUB_REPLIES, CRAWLER_MAX_NOTES_COUNT,
    ENABLE_IP_PROXY, ENABLE_CACHE_DEDUP, SAVE_DATA_OPTION,
)
from config.db_config import (
    REDIS_DB, REDIS_HOST, REDIS_PORT, SCRAPY_REDIS_PARAMS,
    REDIS_VIDEO_KEY, REDIS_COMMENT_KEY,
)
from config.crawler_config import (
    DOWNLOAD_DELAY, RANDOMIZE_DOWNLOAD_DELAY, CONCURRENT_REQUESTS,
    CONCURRENT_REQUESTS_PER_DOMAIN, MAX_CONCURRENCY_NUM,
    DOWNLOAD_TIMEOUT, RETRY_ENABLED, RETRY_TIMES, RETRY_HTTP_CODES,
    SCHEDULER_PERSIST, SCHEDULER_IDLE_BEFORE_CLOSE,
    REDIS_START_URLS_AS_SET, LOG_LEVEL, LOG_STDOUT, LOG_FORMAT,
    CRAWLER_MAX_SLEEP_SEC, CRAWLER_PAGE_SLEEP_SEC,
    RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX, RETRY_BACKOFF_MULTIPLIER,
)

# ---- Project ----
BOT_NAME = "bilibili_crawler"
SPIDER_MODULES = ["bilibili_crawler.spiders"]
NEWSPIDER_MODULE = "bilibili_crawler.spiders"

# ---- Download (API mode) ----
DOWNLOAD_DELAY = DOWNLOAD_DELAY
RANDOMIZE_DOWNLOAD_DELAY = RANDOMIZE_DOWNLOAD_DELAY
CONCURRENT_REQUESTS = CONCURRENT_REQUESTS
CONCURRENT_REQUESTS_PER_DOMAIN = CONCURRENT_REQUESTS_PER_DOMAIN

# Timeout & Retry (增强指数退避)
DOWNLOAD_TIMEOUT = DOWNLOAD_TIMEOUT
RETRY_ENABLED = RETRY_ENABLED
RETRY_TIMES = RETRY_TIMES
RETRY_HTTP_CODES = RETRY_HTTP_CODES

# Cookies required for B站 API
COOKIES_ENABLED = True

# B站 API 请求头（模拟真实 Chrome，Header 顺序也很重要）
DEFAULT_REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Referer": BILIBILI_REFERER,
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

# Disable AutoThrottle (manual control is more precise)
AUTOTHROTTLE_ENABLED = False

# ---- Middleware (可插拔架构，参考 MediaCrawler) ----
# ⚠️ 优先级至关重要：Cookie/Header/RateLimit 必须在 WbiRefresh 之前运行。
# 原因：WbiRefresh 返回 request.replace() 新 Request，会跳过下游中间件。
#      只有排在前面的中间件设置的 cookies/headers 才能通过 replace() 继承到新 Request。
#
# 三层 412 对抗架构:
#   1. BilibiliCurlCffiMiddleware (priority 89)
#      → 用 curl_cffi 伪装 Chrome TLS 指纹（替代原来的 RequestsFallback）
#   2. BilibiliCookiePoolMiddleware (priority 26, 条件启用)
#      → 多账号 Cookie 轮换，降低单账号限速风险
#   3. BilibiliPlaywrightFallbackMiddleware (priority 88, 条件启用)
#      → curl_cffi 仍然 412 时，用真实 Playwright 浏览器兜底
# ===========================================================

# 先定义基础中间件（不含 Cookie 层，后面按条件注册）
DOWNLOADER_MIDDLEWARES = {
    # 禁用默认UA中间件
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,

    # --- 第二层: 请求头 ---
    "bilibili_crawler.middlewares.BilibiliHeaderMiddleware": 50,

    # --- 第三层: 频率控制 ---
    "bilibili_crawler.middlewares.BilibiliRateLimitMiddleware": 75,

    # --- 第四层(备选): Playwright 真实浏览器兜底 ---
    # 优先级 88（在 curl_cffi 之前，让 Playwright 可以接管）
    "bilibili_crawler.middlewares_playwright.BilibiliPlaywrightFallbackMiddleware": 88,

    # --- 第四层: TLS 指纹伪装 (curl_cffi) ---
    # 替换原来的 BilibiliRequestsFallbackMiddleware (priority 89)
    # curl_cffi 伪装 Chrome 124 TLS 指纹，绕过 B站 WAF JA3/JA4 检测
    "bilibili_crawler.handlers.curl_cffi_handler.BilibiliCurlCffiMiddleware": 89,

    # --- 第五层: WBI 签名刷新 ---
    "bilibili_crawler.middlewares.BilibiliWbiRefreshMiddleware": 100,

    # 重试中间件
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": 500,

    # 响应处理中间件 (风控处理/降级 — 在 Retry 之后，处理已重试过的响应)
    # 同时负责: 检测 412 → 触发 Cookie 池冷却 / Playwright 兜底
    "bilibili_crawler.middlewares.BilibiliResponseMiddleware": 550,
}

# ---- Cookie 中间件（互斥逻辑）----
# 规则: ENABLE_COOKIE_POOL=True → 用多账号池，禁用单账号中间件
#       ENABLE_COOKIE_POOL=False → 用单账号 CookieMiddleware
try:
    from config.accounts import ENABLE_COOKIE_POOL
    if ENABLE_COOKIE_POOL:
        # 多账号 Cookie 池模式
        DOWNLOADER_MIDDLEWARES["bilibili_crawler.middlewares_cookie_pool.BilibiliCookiePoolMiddleware"] = 26
        # 禁用单账号中间件（设为 None）
        DOWNLOADER_MIDDLEWARES["bilibili_crawler.middlewares.BilibiliCookieMiddleware"] = None
    else:
        # 单账号模式
        DOWNLOADER_MIDDLEWARES["bilibili_crawler.middlewares.BilibiliCookieMiddleware"] = 25
except ImportError:
    # config/accounts.py 不存在时，默认用单账号模式
    DOWNLOADER_MIDDLEWARES["bilibili_crawler.middlewares.BilibiliCookieMiddleware"] = 25

# 条件注册代理中间件（参考 MediaCrawler ENABLE_IP_PROXY 开关）
if ENABLE_IP_PROXY:
    DOWNLOADER_MIDDLEWARES["bilibili_crawler.middlewares.BilibiliProxyMiddleware"] = 150

# ---- Pipeline (可插拔存储后端) ----
ITEM_PIPELINES = {
    "bilibili_crawler.pipelines.BilibiliDedupPipeline": 100,
    "bilibili_crawler.pipelines.BilibiliCleanPipeline": 200,
}

# 根据 SAVE_DATA_OPTION 选择存储后端
if SAVE_DATA_OPTION == "sqlite":
    ITEM_PIPELINES["bilibili_crawler.pipelines.BilibiliSqlitePipeline"] = 300
else:
    ITEM_PIPELINES["bilibili_crawler.pipelines.BilibiliJsonPipeline"] = 300

# UserPostsPipeline (v2.1): 用户动态存储, 在 JSON/SQLite 之后
ITEM_PIPELINES["bilibili_crawler.pipelines.UserPostsPipeline"] = 350

# DanmakuPipeline (v2.2): 弹幕存储
ITEM_PIPELINES["bilibili_crawler.pipelines.DanmakuPipeline"] = 360

# UpVideosPipeline (v2.14): UP主投稿视频存储
ITEM_PIPELINES["bilibili_crawler.pipelines.UpVideosPipeline"] = 370

ITEM_PIPELINES["bilibili_crawler.pipelines.UserCachePipeline"] = 400

# ---- Scrapy-Redis (distributed mode) ----
SCHEDULER = "scrapy_redis.scheduler.Scheduler"
DUPEFILTER_CLASS = "scrapy_redis.dupefilter.RFPDupeFilter"
SCHEDULER_PERSIST = SCHEDULER_PERSIST
SCHEDULER_QUEUE_CLASS = "scrapy_redis.queue.PriorityQueue"
SCHEDULER_IDLE_BEFORE_CLOSE = SCHEDULER_IDLE_BEFORE_CLOSE

# ---- Redis (db=1 to avoid conflict with news_crawler db=0) ----
REDIS_HOST = REDIS_HOST
REDIS_PORT = REDIS_PORT
REDIS_DB = REDIS_DB                          # 显式声明，优先级 > REDIS_PARAMS.db
REDIS_PARAMS = SCRAPY_REDIS_PARAMS

REDIS_START_URLS_KEY = REDIS_VIDEO_KEY
REDIS_START_URLS_AS_SET = REDIS_START_URLS_AS_SET

# ---- Logging ----
import datetime as _dt
LOG_LEVEL = LOG_LEVEL
LOG_ENABLED = True
LOG_STDOUT = LOG_STDOUT
LOG_FORMAT = LOG_FORMAT
LOG_FILE = os.path.join(LOG_DIR, "bilibili_crawler.log")

# ---- Extensions ----
EXTENSIONS = {
    "scrapy.extensions.logstats.LogStats": 0,
    "scrapy.extensions.telnet.TelnetConsole": None,
}

# ---- Robot.txt ----
ROBOTSTXT_OBEY = False
