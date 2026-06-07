"""
B站账号配置 — Cookie 池 + Playwright 兜底开关

使用方式:
  1. 复制 accounts_local.py → accounts.py（不提交到 Git）
  2. 填入你的 B站 Cookie（多个账号轮流使用）
  3. 在 Dashboard Settings 页面控制开关
"""

import os

# ============================================================
# Cookie 池开关
# ============================================================

# 是否启用多账号 Cookie 池（True = 多账号轮换，False = 单账号）
# 注意: 启用后，settings.py 会自动禁用 BilibiliCookieMiddleware（互斥）
ENABLE_COOKIE_POOL = False  # ← 填入 True / False


# ============================================================
# Cookie 池账号列表
# ============================================================

# 每个账号是一个 dict，包含从浏览器复制的完整 Cookie 字符串
# 获取方式: 浏览器登录 B站 → F12 → Application → Cookies → .bilibili.com
# 复制所有 Cookie 为 "key1=value1; key2=value2" 格式
BILIBILI_ACCOUNTS = [
    {
        "name": "account_1",
        "cookie": (
            "buvid3=xxx; "
            "i-wanna-go-back=-1; "
            "b_nut=xxx; "
            "_uuid=xxx; "
            "buvid4=xxx; "
            "DedeUserID=xxx; "
            "DedeUserToken=xxx; "
            "SESSDATA=xxx; "
            "bili_jct=xxx; "
            "PVID=xxx"
        ),
        "cooldown_until": 0,  # 自动管理，无需手动填
    },
    # 如果有第二个账号，取消注释并填入
    # {
    #     "name": "account_2",
    #     "cookie": "buvid3=yyy; ...",
    #     "cooldown_until": 0,
    # },
]


# ============================================================
# Cookie 池行为配置
# ============================================================

# 触发冷却的 412 次数阈值（单个账号）
# 当某个账号连续触发 N 次 412，自动冷却 5 分钟
COOKIE_POOL_TRIGGER_412 = 3

# 冷却时长（秒）
COOKIE_POOL_COOLDOWN_SECS = 300  # 5 分钟

# ---- Cookie 池中间件兼容别名 (middlewares_cookie_pool.py 使用) ----
# 不要直接修改这些值，修改上面的 COOKIE_POOL_* 即可
COOKIE_ROTATE_STRATEGY = "round_robin"         # round_robin / random
COOKIE_SWITCH_EVERY_N_REQUESTS = 5             # 每 N 个请求切换账号
COOKIE_COOLDOWN_SECONDS = COOKIE_POOL_COOLDOWN_SECS
BILIBILI_COOKIE_POOL = BILIBILI_ACCOUNTS       # 别名兼容


# ============================================================
# Playwright 兜底配置
# ============================================================

# 是否启用 Playwright 真实浏览器兜底
# 当 curl_cffi 仍然返回 412 时（连续 N 次），自动切换 Playwright
ENABLE_PLAYWRIGHT_FALLBACK = True  # 默认启用，curl_cffi 扛不住时自动切真实浏览器

# 触发 Playwright 兜底的连续 412 次数
# （在 BilibiliResponseMiddleware 中检测）
PLAYWRIGHT_TRIGGER_412_COUNT = 5

# Playwright 浏览器类型 ("chromium" / "firefox")
PLAYWRIGHT_BROWSER = "chromium"

# 是否无头模式（True = 后台运行，False = 可见窗口 — 调试时有用）
PLAYWRIGHT_HEADLESS = True

# Playwright 请求超时（毫秒）
PLAYWRIGHT_TIMEOUT = 30000

# ============================================================
# ★ 新增: Playwright 空间页爬取器兜底配置
# ============================================================

# 是否启用空间页爬取兜底（SpacePageScraper）
# 当 card API / space page 都失败时，启动真实浏览器爬取 B站空间页 DOM 元素
ENABLE_PW_SPACE_SCRAPER = True

# 空间页爬取器超时（毫秒）
PW_SPACE_SCRAPER_TIMEOUT = 30000

# 空间页爬取器是否无头模式（True = 后台运行）
PW_SPACE_SCRAPER_HEADLESS = True

# 视频列表最大翻页数
PW_SPACE_SCRAPER_VIDEO_MAX_PAGES = 5
