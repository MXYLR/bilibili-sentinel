"""
B站用户数据采集 Spider — F12-F14 特征数据源

核心流程:
 1. 从 Redis (db=1) 读取用户 MID 种子
 2. Step A: card API → 用户画像 → yield UserInfoItem
 3. Step B: 视频 API → 补充 video_count → yield UserInfoItem
 4. Step C: polymer 动态 API → 动态列表 → yield UserPostItem
 5. ★ 新增: Playwright 空间页爬取兜底 (SpacePageScraper)
 6. Spider idle 时检查 Redis 新种子，超时后自动退出

数据用途:
 - UserInfoItem → F1(账号年龄), F2(粉丝比), F3(等级), F4(头像), F12(骨架)
 - UserPostItem → F13(转发抽奖), F14(敏感内容)

种子格式 (JSON):
 {"mid": 24512285}

运行方式:
 scrapy crawl bilibili_user
"""


# ★ v2.33 Windows 事件循环策略补丁: 必须在所有 import 之前设置,
# 否则 Playwright sync API 内部调用 asyncio.new_event_loop() 会拿到 SelectorEventLoop,
# 导致 create_subprocess_exec 抛 NotImplementedError
# 注意: 不能在模块顶层 import twisted.internet (会提前安装 SelectReactor),
# deferToThread 改为懒导入 (在 _fetch_user_info_via_pw_scraper 内部)
import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import logging
import os
import time

# ★ 不再在顶层导入 twisted.internet.threads
# from twisted.internet.threads import deferToThread  # ← 移到方法内部懒加载
import redis
import scrapy
from scrapy import signals
from scrapy.exceptions import DontCloseSpider

from bilibili_crawler.items import UserInfoItem, UserPostItem
from bilibili_crawler.utils.bilibili_api import (
    get_user_info_url,
    get_user_posts_url,
    get_user_videos_url,
    parse_bilibili_response,
    prewarm_wbi_cache,
)
# ★ 新增: 导入 Playwright 空间页爬取器
from bilibili_crawler.utils.playwright_space_scraper import (
    SpacePageScraper,
    scrape_user_profile as pw_scrape_profile,
)

logger = logging.getLogger("bilibili_user")

# ---- 安全限制 ----
MAX_USERS_PER_RUN = 2000       # 单次运行最多处理2000个用户（单视频~1000评论者）
POSTS_PER_USER = 50            # 每个用户最多采集50条动态
MAX_POSTS_PAGES = 5            # 动态最多翻5页 (每页约10-12条)
MAX_IDLE_TIME = 300            # 空闲超时(s): 等待新种子的最大时间

# ---- Redis 配置 ----
_REDIS_HOST = "localhost"
_REDIS_PORT = 6379
_REDIS_DB = 1
_REDIS_KEY = "bilibili_crawler:user_seeds"

# ---- 本地路径 ----
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")


class BilibiliUserSpider(scrapy.Spider):
    """
    B站用户数据爬虫 — 补充 F12-F14 水军检测所需信息。

    从 Redis 队列获取 MID 列表，依次采集:
    - 用户画像 (UserInfoItem)
    - 投稿视频 (data/up_videos/{mid}_videos.json)  ★ v2.35
    - 用户动态 (UserPostItem)
    """

    name = "bilibili_user"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 2.5,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "DOWNLOAD_TIMEOUT": 30,
        "COOKIES_ENABLED": True,
        "PLAYWRIGHT_ENABLED": False,
        "SCHEDULER": "scrapy.core.scheduler.Scheduler",  # ★ 不用scrapy_redis
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._redis = None
        self._user_count = 0
        self._total_posts = 0
        self._idle_start_time = None
        self._seen_mids = set()
        self._use_playwright = False
        self._never_use_playwright = True  # ★ 禁用中间件的 Playwright 兜底
        self._412_count = 0
        logger.info(f"BilibiliUserSpider v2026-06-06-v2.32 (native scheduler, visible PW fallback) initialized")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_idle, signal=signals.spider_idle)
        return spider

    # ★ Scrapy 2.16+ API: start_requests() 已弃用，改为 async start()
    # 旧的 def start_requests(self) 在 Scrapy 2.16 中永不被调用（Spider.start_requests 已移除）
    # 详见: https://docs.scrapy.org/en/latest/topics/spiders.html#scrapy.Spider.start
    async def start(self):
        """使用 native scheduler: 消费种子或保持存活。"""
        logger.info("★★★ async start() called ★★★")
        try:
            prewarm_wbi_cache()
            logger.info("prewarm_wbi_cache done")
            skipped = 0
            while True:
                mid = self._pop_seed()
                logger.info(f"_pop_seed returned: {mid}")
                if mid is None:
                    break
                if mid in self._seen_mids:
                    skipped += 1
                    continue
                self._seen_mids.add(mid)
                req = self._request_user_info(mid)
                if req:
                    logger.info(f"First seed consumed: mid={mid}")
                    yield req
                    return
                skipped += 1
            if skipped > 0:
                logger.info(f"All {skipped} seeds already collected")
            logger.info("start_requests: no seeds, spider will stay alive via spider_idle")
            self._idle_start_time = time.time()
            # ★ yield 一个空请求占位，防止 spider 立即关闭
            yield scrapy.Request("data:text/plain,keepalive", callback=self._keepalive, dont_filter=True)
        except Exception as e:
            import traceback
            logger.error(f"start_requests exception: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            raise

    def _keepalive(self, response):
        """占位回调：触发 spider_idle 继续轮询种子。"""
        pass

    # ================================================================
    #  Redis 种子读取
    # ================================================================

    def _get_redis(self):
        if not hasattr(self, '_redis') or self._redis is None:
            self._redis = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB, decode_responses=True)
        return self._redis

    def _pop_seed(self):
        """从 Redis 队列弹出一个 MID 种子。返回 MID int 或 None。
        
        v2.32 fix: 兼容两种格式 — 
          1. JSON: {"mid": 123} (推荐，系统内部注入)
          2. 纯数字字符串: "123" (兼容 redis-cli LPUSH 的手动注入)
        """
        r = self._get_redis()
        try:
            raw = r.lpop(_REDIS_KEY)
            if not raw:
                return None
            # 尝试 JSON 解析，失败则当作纯数字兼容
            try:
                seed = json.loads(raw)
                if isinstance(seed, dict):
                    mid = seed.get("mid", 0) or int(seed.get("uid", 0))
                else:
                    mid = int(seed)
            except (json.JSONDecodeError, ValueError, TypeError):
                # ★ v2.32 兼容纯数字字符串
                mid = int(raw.strip())
            return int(mid)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid seed data: {raw[:80] if raw else 'None'}... ({e})")
            return None
        except Exception as e:
            logger.error(f"Redis read error: {e}")
            return None

    def _push_mids_to_redis(self, mids: list):
        """批量注入 MID 到 Redis 种子队列。"""
        r = self._get_redis()
        try:
            for mid in mids:
                r.rpush(_REDIS_KEY, json.dumps({"mid": mid}))
            logger.info(f"Injected {len(mids)} MIDs into {_REDIS_KEY}")
        except Exception as e:
            logger.error(f"Redis write error: {e}")

    # ================================================================
    #  Step A: 用户画像 → UserInfoItem
    # ================================================================

    def _request_user_info(self, mid: int):
        """请求用户信息 (优先 card API)。已采集的返回 None, 由调用方跳过。"""
        user_file = os.path.join(DATA_DIR, "users", f"{mid}.json")
        if os.path.exists(user_file) and os.path.getsize(user_file) > 100:
            return None  # 已采集, 不递归, 由 while 循环处理

        # ★ 主接口: card API | 每次请求前强制关闭 Playwright 兜底
        self._use_playwright = False
        self._never_use_playwright = True  # ★ 禁用中间件的 Playwright 兜底
        self._412_count = 0
        card_url = f"https://api.bilibili.com/x/web-interface/card?mid={mid}"
        return scrapy.Request(
            card_url, callback=self._parse_card_api,
            meta={"mid": mid, "_primary": True},
            errback=self._handle_error,
            dont_filter=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                     "Referer": f"https://space.bilibili.com/{mid}"},
        )

    def parse_user_info(self, response):
        """解析 /x/space/wbi/acc/info 响应，产出 UserInfoItem。"""
        mid = response.meta["mid"]
        import json as _json
        result = parse_bilibili_response(_json.loads(response.text))

        if result is None:
            logger.warning(f"[mid={mid}] User info API unavailable, falling back to Playwright")
            yield from self._fetch_user_info_playwright(mid, response.meta)
            return

        data = result.get("data", {})
        if not data:
            logger.warning(f"[mid={mid}] Empty user data")
            self._fetch_next_user()
            return

        yield from self._build_user_info_item(mid, data, response.meta)

    # ================================================================
    #  Playwright 兜底: 从 B站空间页 HTML 提取用户数据
    # ================================================================

    def _build_user_info_item(self, mid, data, meta):
        """用 API 数据构造 UserInfoItem 并继续后续步骤。"""
        vip = data.get("vip", {}) or {}
        official = data.get("official", {}) or {}

        user_item = UserInfoItem(
            mid=mid,
            name=data.get("name", ""),
            sex=data.get("sex", ""),
            face=data.get("face", ""),
            sign=data.get("sign", ""),
            level=data.get("level", 0),
            birthday=data.get("birthday", ""),
            vip_status=vip.get("status", 0),
            official_verify=json.dumps(official, ensure_ascii=False) if official.get("type", -1) >= 0 else "",
            follower=data.get("follower", 0),
            following=data.get("following", 0),
            video_count=data.get("archive_count", 0),
            post_count=data.get("post_count", -1),
            upload_count=data.get("archive_count", 0),
            crawl_time=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        # ★ 防御性检查: 禁止关键字段为硬编码零值(upload_count=0除外,正常)
        if int(user_item["follower"]) == 0 and int(data.get("follower", 0)) > 0:
            logger.error(f"BUG: follower={data.get('follower')} lost! item={user_item['follower']}")
        if int(user_item["following"]) == 0 and int(data.get("following", 0)) > 0:
            logger.error(f"BUG: following={data.get('following')} lost! item={user_item['following']}")

        meta["user_info_item"] = user_item
        # ★ card API 已有 archive_count，直接产出
        yield user_item
        self._user_count += 1

        # ★ v2.35: 并行请求视频投稿 + 动态（独立 API，互不依赖）
        # 视频投稿: /x/space/wbi/arc/search
        videos_url = get_user_videos_url(mid, page=1, ps=50)
        yield scrapy.Request(
            videos_url,
            callback=self._parse_user_videos_api,
            meta={"mid": mid, "page": 1, "videos_collected": []},
            errback=self._videos_error,
            dont_filter=True,
        )
        # 动态: /x/polymer/web-dynamic/v1/feed/space
        posts_url = get_user_posts_url(mid)
        yield scrapy.Request(
            posts_url,
            callback=self.parse_user_posts,
            meta={"mid": mid, "posts_collected": 0, "pages": 1},
            errback=self._posts_error,
            dont_filter=True,
        )

    def _fetch_user_info_playwright(self, mid, meta):
        """★ 兜底: 多层回退 (card API → 空间页 HTML)。"""
        card_url = f"https://api.bilibili.com/x/web-interface/card?mid={mid}"
        logger.info(f"[mid={mid}] FallbackA: card API")
        yield scrapy.Request(
            card_url,
            callback=self._parse_card_api,
            meta={"mid": mid, "user_meta": meta},
            errback=self._card_api_fallback,
            dont_filter=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0", "Referer": f"https://space.bilibili.com/{mid}"},
        )

    def _card_api_fallback(self, failure):
        """card API errback → 空间页 HTML。"""
        request = failure.request
        mid = request.meta.get("mid")
        meta = request.meta.get("user_meta", {})
        logger.warning(f"[mid={mid}] card API errback: {failure.value}")
        page_url = f"https://space.bilibili.com/{mid}"
        logger.info(f"[mid={mid}] FallbackB: space page")
        yield scrapy.Request(
            page_url, callback=self._parse_space_page,
            meta={"mid": mid, "user_meta": meta}, errback=self._handle_error, dont_filter=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0", "Referer": "https://www.bilibili.com/"},
        )

    def _space_page_fallback(self, mid, meta):
        """card API 失败 → 空间页 HTML。"""
        page_url = f"https://space.bilibili.com/{mid}"
        logger.info(f"[mid={mid}] FallbackB: space page")
        yield scrapy.Request(
            page_url, callback=self._parse_space_page,
            meta={"mid": mid, "user_meta": meta}, errback=self._handle_error, dont_filter=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0", "Referer": "https://www.bilibili.com/"},
        )

    def _parse_card_api(self, response):
        """解析 card API 响应。失败时逐步降级: 直接 Playwright 兜底（v2.32: 跳过无意义的 -352 重试）。"""
        mid = response.meta["mid"]
        meta = response.meta.get("user_meta", response.meta)
        import json as _json
        try:
            data = _json.loads(response.text)
            if data.get("code") != 0:
                size = len(response.text)
                if size < 100:
                    # ★ v2.32: card API 不含 WBI 签名，-352 重试无意义（cookie/session 问题不会因重试修复）
                    # 直接走 Playwright 空间页兜底，跳过 3 秒无效等待
                    code_val = data.get("code")
                    reason = "被限流" if code_val == -352 else f"code={code_val}"
                    if code_val == -352:
                        logger.warning(f"[mid={mid}] card API -352 ({size}bytes) → 跳过重试，直接 Playwright 兜底")
                    else:
                        logger.warning(f"[mid={mid}] card API {reason} ({size}bytes) → Playwright fallback")
                    yield from self._fetch_user_info_via_pw_scraper(mid, meta)
                    return
                logger.warning(f"[mid={mid}] card API code={data.get('code')}")
                yield from self._space_page_fallback(mid, meta)
                return
            card = data.get("data", {}).get("card", {})
            if not card or not card.get("name"):
                logger.warning(f"[mid={mid}] card API empty → Playwright fallback")
                yield from self._fetch_user_info_via_pw_scraper(mid, meta)
                return
            user_data = {
                "mid": mid, "name": card.get("name", ""), "face": card.get("face", ""),
                "sign": card.get("sign", ""), "level": card.get("level_info", {}).get("current_level", 0),
                "sex": card.get("sex", ""), "birthday": card.get("birthday", ""),
                "archive_count": int(card.get("archives", 0)),
                "follower": int(card.get("fans", 0)),
                "following": int(card.get("attention", 0)),  # card API 有此字段
                "vip": {"status": card.get("vip", {}).get("status", 0)},
                "official": card.get("official") or {},
                "post_count": -1,
                "upload_count": int(card.get("archives", 0)),
            }
            logger.info(f"[mid={mid}] card API OK: name={user_data['name']} Lv{user_data['level']}")
            yield from self._build_user_info_item(mid, user_data, meta)
        except Exception as e:
            logger.warning(f"[mid={mid}] card API parse: {e} → Playwright fallback")
            yield from self._fetch_user_info_via_pw_scraper(mid, meta)

    def _parse_space_page(self, response):
        """解析 B站空间页 HTML, 提取 __INITIAL_STATE__ JSON。"""
        mid = response.meta["mid"]
        meta = response.meta.get("user_meta", response.meta)
        import re
        data = {}
        try:
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});', response.text, re.DOTALL)
            if match:
                state = json.loads(match.group(1))
                up = state.get("up", {})
                data = {
                    "mid": mid,
                    "name": up.get("name", ""),
                    "face": up.get("face", ""),
                    "sign": up.get("sign", ""),
                    "level": up.get("level", 0),
                    "sex": up.get("sex", ""),
                    "birthday": up.get("birthday", ""),
                    "archive_count": int(up.get("archive_count") or 0),
                    "follower": int(up.get("follower") or 0),
                    "following": 0,
                    "vip": {"status": up.get("vip", {}).get("status", 0)},
                    "official": up.get("official", {}),
                    "post_count": 0,
                    "upload_count": int(up.get("archive_count") or 0),
                }
                logger.info(f"[mid={mid}] space page success: name={data['name']} Lv{data['level']}")
            else:
                logger.warning(f"[mid={mid}] No __INITIAL_STATE__ in space page")
        except Exception as e:
            logger.warning(f"[mid={mid}] Parse space page failed: {e}")

        if data and data.get("name"):
            yield from self._build_user_info_item(mid, data, meta)
        else:
            logger.warning(f"[mid={mid}] Space page兜底失败, 跳过")
            # ★ 新增: 最后尝试 SpacePageScraper 直接爬取
            yield from self._fetch_user_info_via_pw_scraper(mid, meta)

    # ================================================================
    #  ★ 新增: Playwright 空间页爬取器兜底
    # ================================================================

    def _fetch_user_info_via_pw_scraper(self, mid, meta):
        """
        最后兜底: 用 SpacePageScraper 直接爬取空间页。
        在 card API 和 space page (__INITIAL_STATE__) 都失败时调用。

        v2.33: 改用 subprocess 调用独立脚本 run_pw_scraper.py，
        彻底避开 Windows SelectorEventLoop 不支持子进程的问题。
        """
        # ★ v2.32: 首次调用带上 _pw_visible=True 标记
        if "_pw_visible" not in meta:
            meta["_pw_visible"] = True
        logger.info(f"[mid={mid}] ★ Final fallback: SpacePageScraper (visible={meta.get('_pw_visible', True)})")

        import sys, os, subprocess, json as _json
        from twisted.internet.threads import deferToThread

        def _run_via_subprocess(mid, meta):
            """在子线程中调用独立 Python 进程运行 Playwright"""
            cookie_str = self._load_cookie_from_file()
            headless = not meta.get("_pw_visible", True)
            if meta.get("_pw_retried", 0) > 0:
                headless = True

            params = _json.dumps({
                "mid": mid,
                "cookie": cookie_str or "",
                "headless": headless
            })

            # 定位独立脚本路径
            script_dir = os.path.dirname(os.path.abspath(__file__))
            script_path = os.path.join(script_dir, "..", "utils", "run_pw_scraper.py")
            script_path = os.path.abspath(script_path)

            if not os.path.exists(script_path):
                logger.error(f"[mid={mid}] run_pw_scraper.py not found: {script_path}")
                return

            try:
                result = subprocess.run(
                    [sys.executable, script_path],
                    input=params.encode("utf-8"),
                    capture_output=True,
                    timeout=120
                )
                if result.returncode != 0:
                    err = result.stderr.decode("utf-8", errors="replace")[:500]
                    logger.error(f"[mid={mid}] PW subprocess failed (code={result.returncode}): {err}")
                    return
                output = result.stdout.decode("utf-8", errors="replace").strip()
                if not output:
                    logger.warning(f"[mid={mid}] PW subprocess returned empty output")
                    return
                profile = _json.loads(output)
                if "_error" in profile:
                    logger.error(f"[mid={mid}] PW subprocess error: {profile['_error']}")
                    return
                logger.info(f"[mid={mid}] PW subprocess success: {len(str(profile))} bytes")
                return profile
            except subprocess.TimeoutExpired:
                logger.error(f"[mid={mid}] PW subprocess timeout (120s)")
                return
            except Exception as e:
                logger.error(f"[mid={mid}] PW subprocess exception: {type(e).__name__}: {e}")
                return

        d = deferToThread(_run_via_subprocess, mid, meta)
        d.addCallback(self._pw_profile_callback, mid, meta)
        d.addErrback(self._pw_profile_errback, mid)
        return d

    # 保留 _run_pw_profile_scraper 作为非 Windows 平台的备用路径（如果需要）
    def _run_pw_profile_scraper(self, mid, meta):
        """（已废弃）原 Windows 子线程 Playwright 调用，保留避免引用错误。"""
        logger.warning(f"[mid={mid}] _run_pw_profile_scraper is deprecated, use subprocess instead")
        return {}

    def _run_pw_profile_scraper(self, mid, meta):
        """在线程中运行 Playwright 爬取（同步代码）

        v2.32: 
        - 优先从 cookies.json 加载真实 Cookie（而非 BILIBILI_ACCOUNTS 占位符）
        - 作为最终兜底时使用可见浏览器（headless=False），方便观察抓取过程
        - v2.32 Windows 子线程修复: 强制设置 ProactorEventLoop，
          否则 asyncio.create_subprocess_exec 抛 NotImplementedError
        """
        import asyncio, sys
        # ★ v2.33 Windows 子线程事件循环补丁（关键修复）
        # Playwright sync API 内部用 asyncio.new_event_loop() 创建新循环，
        # 仅 set_event_loop() 不够，必须设置事件循环策略，
        # 否则 new_event_loop() 在非主线程中返回不支持子进程的 SelectorEventLoop。
        if sys.platform == 'win32':
            try:
                _old_policy = asyncio.get_event_loop_policy()
                if type(_old_policy).__name__ != 'WindowsProactorEventLoopPolicy':
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    logger.info(f"[mid={mid}] Windows补丁: 事件循环策略已切换 WindowsProactorEventLoopPolicy")
            except Exception:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            # 同时确保当前线程有可用的事件循环
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 没有运行中的循环，创建新的
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

        try:
            from bilibili_crawler.utils.playwright_space_scraper import SpacePageScraper

            # ★ v2.32: 优先从 cookies.json 加载真实 Cookie
            cookie_str = self._load_cookie_from_file()
            if not cookie_str:
                # 回退：BILIBILI_ACCOUNTS（仅当非占位符时使用）
                try:
                    from config.accounts import BILIBILI_ACCOUNTS
                    if BILIBILI_ACCOUNTS:
                        raw = BILIBILI_ACCOUNTS[0].get("cookie", "")
                        # 检测占位符：含 "xxx" 或全是占位值则跳过
                        if raw and "xxx" not in raw and "DedeUserID=xxx" not in raw:
                            cookie_str = raw
                            logger.info(f"[mid={mid}] SpacePageScraper using BILIBILI_ACCOUNTS cookie")
                except ImportError:
                    pass

            if not cookie_str:
                logger.warning(f"[mid={mid}] SpacePageScraper: no valid cookie found, proceeding without login")

            # ★ v2.32: 最终兜底时用可见浏览器（headless=False），用户可观察抓取过程
            headless = not meta.get("_pw_visible", True)  # 默认为可见
            # 如果 meta 明确要求无头（自动重试场景），则用无头
            if meta.get("_pw_retried", 0) > 0:
                headless = True  # 重试时仍用无头避免窗口闪烁

            scraper = SpacePageScraper(cookie=cookie_str, headless=headless, timeout=30000)
            logger.info(f"[mid={mid}] SpacePageScraper starting (headless={headless}, cookie={'✓' if cookie_str else '✗'})")
            profile = scraper.scrape_user_profile(mid)
            return profile
        except Exception as e:
            import traceback as _tb
            logger.error(f"[mid={mid}] SpacePageScraper error: {type(e).__name__}: {e}")
            logger.error(f"[mid={mid}] SpacePageScraper traceback:\n{_tb.format_exc()}")
            return {}

    def _load_cookie_from_file(self) -> str:
        """★ v2.32: 从 cookies.json 加载真实 Cookie 字符串"""
        import json as _json
        import os as _os
        cookie_file = os.path.join(DATA_DIR, "cookies.json")
        if not _os.path.exists(cookie_file):
            return ""
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookies = _json.load(f)
            if not cookies:
                return ""
            # 构造 cookie 字符串: key1=value1; key2=value2; ...
            parts = []
            for k, v in cookies.items():
                parts.append(f"{k}={v}")
            cookie_str = "; ".join(parts)
            logger.info(f"[CookieLoader] loaded {len(cookies)} keys from cookies.json, SESSDATA={'***' if 'SESSDATA' in cookies else 'N/A'}")
            return cookie_str
        except Exception as e:
            logger.warning(f"[CookieLoader] failed: {e}")
            return ""

    def _pw_profile_callback(self, result, mid, meta):
        """Playwright 爬取成功的回调 (v2.33: 处理扩展后的返回数据)"""
        import json as _json, os as _os
        # result 现在是 {"profile":..., "videos":..., "posts":...}
        profile = result.get("profile", {}) if isinstance(result, dict) else {}
        videos = result.get("videos", []) if isinstance(result, dict) else []
        posts  = result.get("posts",  []) if isinstance(result, dict) else []

        # ★ 保存投稿视频到 data/up_videos/
        if videos:
            _vid_dir = _os.path.join(DATA_DIR, "up_videos")
            _os.makedirs(_vid_dir, exist_ok=True)
            _vid_file = _os.path.join(_vid_dir, f"{mid}_videos.json")
            try:
                with open(_vid_file, "w", encoding="utf-8") as f:
                    _json.dump(videos, f, ensure_ascii=False, indent=2)
                logger.info(f"[mid={mid}] ✅ PW 投稿视频已保存: {len(videos)} 条 → {_vid_file}")
            except Exception as e:
                logger.warning(f"[mid={mid}] PW 投稿视频保存失败: {e}")

        # ★ 保存动态到 data/users/{mid}_posts.json
        if posts:
            _user_dir = _os.path.join(DATA_DIR, "users")
            _os.makedirs(_user_dir, exist_ok=True)
            _post_file = _os.path.join(_user_dir, f"{mid}_posts.json")
            try:
                with open(_post_file, "w", encoding="utf-8") as f:
                    _json.dump(posts, f, ensure_ascii=False, indent=2)
                logger.info(f"[mid={mid}] ✅ PW 动态已保存: {len(posts)} 条 → {_post_file}")
            except Exception as e:
                logger.warning(f"[mid={mid}] PW 动态保存失败: {e}")

        # 处理 profile（与原逻辑一致）
        if profile and profile.get("name"):
            logger.info(f"[mid={mid}] ✅ SpacePageScraper 成功: {profile['name']}")
            return list(self._build_user_info_item(mid, profile, meta))
        else:
            retried = meta.get("_pw_retried", 0)
            if retried < 1:
                logger.warning(f"[mid={mid}] SpacePageScraper empty, auto-retry ({retried+1}/1)")
                meta["_pw_retried"] = retried + 1
                return self._fetch_user_info_via_pw_scraper(mid, meta)
            logger.warning(f"[mid={mid}] SpacePageScraper failed after retry, recording to failed set")
            try:
                import redis as _rd
                r = _rd.Redis(host="localhost", port=6379, db=1, decode_responses=True)
                r.sadd("bilibili_crawler:user_seeds_failed", str(mid))
            except Exception:
                pass
            self._fetch_next_user()
            return []

    def _pw_profile_errback(self, failure, mid):
        """Playwright 爬取失败的回调"""
        logger.error(f"[mid={mid}] SpacePageScraper 异常: {failure.getErrorMessage()}")
        # ★ 自动重试一次
        try:
            meta = None  # errback 没有 meta 参数
        except Exception:
            pass
        # ★ 记录到 Redis 失败集合
        try:
            import redis as _rd
            r = _rd.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            r.sadd("bilibili_crawler:user_seeds_failed", str(mid))
        except Exception:
            pass
        self._fetch_next_user()
        return []

    # ================================================================
    #  Step B: 投稿列表 → 保存到 data/up_videos/ (v2.35)
    # ================================================================

    def _parse_user_videos_api(self, response):
        """解析 /x/space/wbi/arc/search 响应，保存视频列表到本地 JSON，处理翻页。

        v2.35 新增: 从 _build_user_info_item 并行发起，与动态抓取独立。
        保存路径: data/up_videos/{mid}_videos.json
        """
        mid = response.meta["mid"]
        page = response.meta.get("page", 1)
        prev_collected = response.meta.get("videos_collected", [])

        import json as _json
        result = parse_bilibili_response(_json.loads(response.text))

        if result is None:
            logger.warning(f"[mid={mid}] Video API page {page} returned error/empty")
            # 即使当前页失败，也把之前收集的保存
            self._flush_videos_to_file(mid, prev_collected)
            return

        data = result.get("data", {})
        page_info = data.get("page", {})
        total_count = page_info.get("count", 0)
        vlist = data.get("list", {}).get("vlist", [])

        if not vlist:
            logger.debug(f"[mid={mid}] Page {page} no videos (total={total_count}), saving {len(prev_collected)}")
            self._flush_videos_to_file(mid, prev_collected)
            return

        # 提取视频核心字段
        for v in vlist:
            bvid = v.get("bvid", "")
            if not bvid:
                continue
            prev_collected.append({
                "bvid": bvid,
                "aid": v.get("aid", 0),
                "title": v.get("title", ""),
                "cover": v.get("pic", ""),
                "play": v.get("play", 0),
                "comment": v.get("comment", 0),
                "created": v.get("created", 0),
                "length": v.get("length", ""),
                "description": v.get("description", ""),
                "typeid": v.get("typeid", 0),
                "tname": v.get("tname", ""),
            })

        logger.info(f"[mid={mid}] Video page {page}: +{len(vlist)} videos (total_so_far={len(prev_collected)}/{total_count})")

        # 翻页逻辑
        page_size = page_info.get("ps", 50)
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        max_pages = min(total_pages, 10)  # 最多翻 10 页 (500 条视频)

        if page < max_pages:
            next_page = page + 1
            next_url = get_user_videos_url(mid, page=next_page, ps=50)
            yield scrapy.Request(
                next_url,
                callback=self._parse_user_videos_api,
                meta={"mid": mid, "page": next_page, "videos_collected": prev_collected},
                errback=self._videos_error,
                dont_filter=True,
            )
        else:
            # 最后一页 or 达到上限 → 保存
            self._flush_videos_to_file(mid, prev_collected)

    def _flush_videos_to_file(self, mid, videos):
        """原子写入视频列表到 data/up_videos/{mid}_videos.json"""
        if not videos:
            return
        import json as _json
        vid_dir = os.path.join(DATA_DIR, "up_videos")
        os.makedirs(vid_dir, exist_ok=True)
        vid_file = os.path.join(vid_dir, f"{mid}_videos.json")
        tmp_file = vid_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                _json.dump(videos, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, vid_file)
            logger.info(f"[mid={mid}] ✅ 投稿视频已保存: {len(videos)} 条 → {vid_file}")
        except Exception as e:
            logger.warning(f"[mid={mid}] 投稿视频保存失败: {e}")
            try:
                os.remove(tmp_file)
            except OSError:
                pass

    def _videos_error(self, failure):
        """视频 API 请求失败的回调"""
        request = failure.request
        mid = request.meta.get("mid", "?")
        prev_collected = request.meta.get("videos_collected", [])
        logger.warning(f"[mid={mid}] Video API errback: {failure.value}, saving {len(prev_collected)} collected")
        if prev_collected:
            self._flush_videos_to_file(mid, prev_collected)

    def parse_user_videos(self, response):
        """解析 /x/space/wbi/arc/search 响应，补充统计信息。"""
        mid = response.meta["mid"]
        user_item = response.meta.get("user_info_item")
        import json as _json
        result = parse_bilibili_response(_json.loads(response.text))

        if result is not None:
            data = result.get("data", {})
            page_info = data.get("page", {})
            video_count = page_info.get("count", 0)

            if user_item:
                # 补全投稿数 (以 API 返回为准)
                if video_count > 0:
                    user_item["video_count"] = video_count
                    user_item["upload_count"] = video_count

            # 提取第一条视频的 UP 主统计 (粉丝/关注)
            vlist = data.get("list", {}).get("vlist", [])
            if vlist:
                first_video = vlist[0]
                if user_item:
                    if user_item["follower"] == 0:
                        user_item["follower"] = first_video.get("author_fans", 0) or 0

            logger.debug(f"[mid={mid}] Video count={video_count}, follower={user_item.get('follower', 0) if user_item else '?'}")

        # ---- 产出 UserInfoItem ----
        if user_item:
            yield user_item

        self._user_count += 1

        # ---- Step C: 抓取用户动态 ----
        posts_url = get_user_posts_url(mid)
        yield scrapy.Request(
            posts_url,
            callback=self.parse_user_posts,
            meta={
                "mid": mid,
                "posts_collected": 0,
                "pages": 1,
            },
            errback=self._posts_error,
            dont_filter=True,
        )

    # ================================================================
    #  Step C: 动态列表 → UserPostItem
    # ================================================================

    def parse_user_posts(self, response):
        """解析 /x/polymer/web-dynamic/v1/feed/space 响应。"""
        mid = response.meta["mid"]
        posts_collected = response.meta.get("posts_collected", 0)
        page_num = response.meta.get("pages", 1)

        # 非标准 JSON (B站 polymer API 有时返回 text/html)
        try:
            result = json.loads(response.text)
        except json.JSONDecodeError:
            logger.warning(f"[mid={mid}] Polymer API returned non-JSON (likely auth required)")
            # 动态 API 可能因未登录返回 HTML，不影响画像采集
            self._fetch_next_user()
            return

        code = result.get("code", -1)
        if code != 0:
            logger.debug(f"[mid={mid}] Polymer API code={code}, msg={result.get('message', '')}")
            self._fetch_next_user()
            return

        data = result.get("data", {})
        items = data.get("items", [])
        has_more = data.get("has_more", False)
        offset = data.get("offset", "")

        # 解析每条动态
        for item_data in items:
            if posts_collected >= POSTS_PER_USER:
                break

            # 提取动态 module
            modules = item_data.get("modules", {})
            desc_module = modules.get("module_dynamic", {}) or modules.get("module_desc", {})
            author_module = modules.get("module_author", {})

            # 动态文本
            desc = desc_module.get("text") if isinstance(desc_module, dict) else ""
            if not desc:
                # 尝试从 desc 字段提取
                desc_data = desc_module.get("desc") if isinstance(desc_module, dict) else None
                desc = desc_data.get("text", "") if desc_data else ""

            # 动态类型
            dyn_type = item_data.get("type", "")
            # DYNAMIC_TYPE_WORD=4, DYNAMIC_TYPE_DRAW=2, DYNAMIC_TYPE_AV=8, DYNAMIC_TYPE_ARTICLE=64
            # DYNAMIC_TYPE_FORWARD=1 (转发)

            # 判断是否转发
            is_repost = (dyn_type == "DYNAMIC_TYPE_FORWARD") or (item_data.get("orig") is not None)

            # 提取纯文本 (去除 HTML 标签和 B站表情)
            content_text = self._strip_html(desc)

            if content_text:
                post_item = UserPostItem(
                    mid=mid,
                    dynamic_id=item_data.get("id_str", str(item_data.get("id", ""))),
                    content=content_text,
                    timestamp=author_module.get("pub_ts", 0) if isinstance(author_module, dict) else 0,
                    is_repost=is_repost,
                    post_type=dyn_type,
                    crawl_time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                yield post_item
                posts_collected += 1
                self._total_posts += 1

        # 翻页
        if has_more and offset and posts_collected < POSTS_PER_USER and page_num < MAX_POSTS_PAGES:
            next_url = get_user_posts_url(mid, offset=offset)
            yield scrapy.Request(
                next_url,
                callback=self.parse_user_posts,
                meta={
                    "mid": mid,
                    "posts_collected": posts_collected,
                    "pages": page_num + 1,
                },
                errback=self._posts_error,
                dont_filter=True,
            )
        else:
            logger.debug(
                f"[mid={mid}] Posts done: {posts_collected} collected "
                f"(pages={page_num}, has_more={has_more})"
            )
            # ★ 更新用户JSON中的post_count
            self._update_user_post_count(mid, posts_collected)
            self._fetch_next_user()

    # ================================================================
    #  辅助方法
    # ================================================================

    @staticmethod
    def _update_user_post_count(mid, count):
        """更新 data/users/{mid}.json 中的 post_count 字段。"""
        import json as _json
        user_file = os.path.join(DATA_DIR, "users", f"{mid}.json")
        if not os.path.exists(user_file):
            return
        try:
            with open(user_file, "r", encoding="utf-8") as f:
                data = _json.load(f)
            data["post_count"] = count
            with open(user_file, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _fetch_next_user(self):
        """从 Redis 取下一个 MID，批量跳过已采集。

        ★ v2.33 fix: 改为自调度模式 — 找到新种子后直接通过 engine.crawl()
        注入请求，不再依赖调用者 yield。修复各处调用者丢弃返回值导致
        种子被弹出但未调度的 Bug（只能靠 spider_idle 兜底，效率极低）。
        """
        if self._user_count >= MAX_USERS_PER_RUN:
            logger.info(f"Reached MAX_USERS_PER_RUN ({MAX_USERS_PER_RUN}), stopping")
            return

        skipped = 0
        loop_guard = 0
        while True:
            loop_guard += 1
            if loop_guard > 10000:  # ★ 安全上限: 防止死循环
                logger.error(f"Loop guard triggered! {loop_guard} iterations, breaking")
                break
            mid = self._pop_seed()
            if mid is None:
                break
            if mid in self._seen_mids:
                skipped += 1
                continue
            self._seen_mids.add(mid)
            req = self._request_user_info(mid)
            if req:
                if skipped > 0:
                    logger.info(f"Next seed consumed: mid={mid} (skipped {skipped})")
                # ★ 直接注入引擎调度，不依赖调用者 yield
                self.crawler.engine.crawl(req)
                self._idle_start_time = None  # 重置 idle 计时
                return
            skipped += 1

        # 队列耗尽
        if skipped > 0:
            logger.info(f"Queue exhausted, {skipped} seeds checked (all collected)")
        if self._idle_start_time is None:
            self._idle_start_time = time.time()
            logger.info("Queue empty. Waiting for new seeds...")

    def _handle_error(self, failure):
        """请求失败时的通用处理，API 失败则 Playwright 兜底。"""
        request = failure.request
        mid = request.meta.get("mid", "?")
        callback_name = request.callback.__name__ if request.callback else ""
        logger.warning(f"[mid={mid}] Request failed: {failure.value}")

        # ★ 用户画像 API 失败 → Playwright 兜底
        if any(k in callback_name for k in ("parse_user_info", "_parse_card_api", "_fetch_user_info")):
            return self._fetch_user_info_via_pw_scraper(mid, request.meta)
            return

        self._fetch_next_user()

    def _posts_error(self, failure):
        """动态 API 请求失败时的处理 (不影响画像采集, 静默跳过)。"""
        request = failure.request
        mid = request.meta.get("mid", "?")
        logger.debug(f"[mid={mid}] Posts API failed: {failure.value}")
        self._fetch_next_user()

    def _strip_html(self, text: str) -> str:
        """移除 HTML 标签和 B站表情，返回纯文本。"""
        import re
        if not text:
            return ""
        # 移除 HTML 标签
        text = re.sub(r'<[^>]+>', '', text)
        # 移除 B站表情 [xxx]
        text = re.sub(r'\[[^\]]+\]', '', text)
        # 规范化空白
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    # ================================================================
    #  Spider Idle — 等待新种子
    # ================================================================

    spider_idle_count = 0  # ★ 防止信号处理死循环

    def spider_idle(self):
        """空闲时检查 Redis 是否有新种子。"""
        self.spider_idle_count += 1
        if self.spider_idle_count > 1000:
            logger.error(f"spider_idle called {self.spider_idle_count} times, forcing close")
            return  # 强制关闭，允许蜘蛛退出
        mid = self._pop_seed()
        if mid and mid not in self._seen_mids:
            self._seen_mids.add(mid)
            req = self._request_user_info(mid)
            if req:
                # ★ 成功获取新种子，重置 idle 计时器
                self._idle_start_time = None
                self.spider_idle_count = 0
                self.crawler.engine.crawl(req)
                logger.info(f"spider_idle: new seed mid={mid}")
                raise DontCloseSpider("New user seed found, continue crawling")

        # 检查超时
        if self._idle_start_time and (time.time() - self._idle_start_time) > MAX_IDLE_TIME:
            logger.info(
                f"Idle timeout ({MAX_IDLE_TIME}s). "
                f"Collected {self._user_count} users, {self._total_posts} posts."
            )
            return  # 允许关闭

        # 等待新种子
        self.crawler.engine.downloader.total_concurrency = 1  # 降低并发等新种子
        raise DontCloseSpider("Waiting for new user seeds...")

    # ================================================================
    #  统计
    # ================================================================

    def closed(self, reason):
        # 统计失败用户数量
        failed_count = 0
        try:
            import redis as _rd
            r = _rd.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            failed_count = r.scard("bilibili_crawler:user_seeds_failed")
        except Exception:
            pass
        logger.info(
            f"Spider closed ({reason}). "
            f"Users: {self._user_count}, Posts: {self._total_posts}, "
            f"Failed: {failed_count} (see bilibili_crawler:user_seeds_failed in Redis)"
        )
