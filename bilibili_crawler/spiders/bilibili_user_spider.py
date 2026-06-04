"""
B站用户数据采集 Spider — F12-F14 特征数据源

核心流程:
  1. 从 Redis (db=1) 读取用户 MID 种子
  2. Step A: GET /x/space/wbi/acc/info → 用户画像 → yield UserInfoItem
  3. Step B: GET /x/space/wbi/arc/search → 投稿列表 → 补充 video_count/post_count
  4. Step C: GET /x/polymer/web-dynamic/v1/feed/space → 动态列表 → yield UserPostItem
  5. Spider idle 时检查 Redis 新种子，超时后自动退出

数据用途:
  - UserInfoItem → F1(账号年龄), F2(粉丝比), F3(等级), F4(头像), F12(骨架)
  - UserPostItem → F13(转发抽奖), F14(敏感内容)

种子格式 (JSON):
  {"mid": 24512285}

运行方式:
  scrapy crawl bilibili_user
"""

import json
import logging
import os
import time

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

logger = logging.getLogger("bilibili_user")

# ---- 安全限制 ----
MAX_USERS_PER_RUN = 500        # 单次运行最多处理500个用户
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
    - 用户动态 (UserPostItem)
    """

    name = "bilibili_user"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 2.5,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "DOWNLOAD_TIMEOUT": 30,
        "COOKIES_ENABLED": True,
        "PLAYWRIGHT_ENABLED": False,  # ★ 用户爬虫不需要 Playwright，card API curl 直通即可
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._redis = None
        self._user_count = 0
        self._total_posts = 0
        self._idle_start_time = None
        self._seen_mids = set()
        self._use_playwright = False  # ★ 不触发 Playwright 兜底
        self._412_count = 0
        logger.info(f"BilibiliUserSpider initialized (Redis db={_REDIS_DB}, key={_REDIS_KEY}, max_idle={MAX_IDLE_TIME}s)")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_idle, signal=signals.spider_idle)
        return spider

    def spider_opened(self):
        """Spider 打开后立即消费种子（批量跳过已采集的）。"""
        skipped = 0
        while True:
            mid = self._pop_seed()
            if mid is None:
                break
            if mid in self._seen_mids:
                skipped += 1
                continue
            self._seen_mids.add(mid)
            req = self._request_user_info(mid)
            if req:
                self.crawler.engine.crawl(req)
                if skipped > 0:
                    logger.info(f"Started with mid={mid} (skipped {skipped} already-collected)")
                else:
                    logger.info(f"Initial seed consumed: mid={mid}")
                return
            else:
                skipped += 1
                logger.debug(f"Initial seed skipped (already collected): mid={mid}")

        if skipped > 0:
            logger.info(f"All {skipped} seeds already collected, entering idle mode")
        else:
            logger.info("No seeds at spider open, entering idle mode")
        self._idle_start_time = time.time()

    # ================================================================
    #  Redis 种子读取
    # ================================================================

    def _get_redis(self):
        if self._redis is None:
            self._redis = redis.Redis(
                host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
                decode_responses=True,
            )
        return self._redis

    def _get_redis(self):
        if not hasattr(self, '_redis') or self._redis is None:
            self._redis = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB, decode_responses=True)
        return self._redis

    def _pop_seed(self):
        """从 Redis 队列弹出一个 MID 种子。返回 MID int 或 None。"""
        r = self._get_redis()
        try:
            raw = r.lpop(_REDIS_KEY)
            if not raw:
                return None
            seed = json.loads(raw) if isinstance(raw, str) else raw
            mid = seed.get("mid") if isinstance(seed, dict) else int(raw)
            return int(mid)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Invalid seed data: {raw[:80]}... ({e})")
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
    #  爬虫入口
    # ================================================================

    def start_requests(self):
        """启动: 从 Redis 读取种子并开始爬取。"""
        prewarm_wbi_cache()

        seeds = []
        for _ in range(50):
            mid = self._pop_seed()
            if mid is None:
                break
            if mid not in self._seen_mids:
                seeds.append(mid)

        if not seeds:
            self._idle_start_time = time.time()
            logger.info("No user seeds in Redis. Entering idle mode, waiting for seeds...")
            return

        logger.info(f"Starting with {len(seeds)} user seed(s): {seeds}")
        for mid in seeds:
            self._seen_mids.add(mid)
            yield self._request_user_info(mid)

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

        meta["user_info_item"] = user_item
        videos_url = get_user_videos_url(mid, page=1, ps=1)
        yield scrapy.Request(
            videos_url, callback=self.parse_user_videos,
            meta=meta, errback=self._handle_error, dont_filter=True,
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
        """解析 card API 响应。39字节=不存在/已注销, 直接跳过。"""
        mid = response.meta["mid"]
        meta = response.meta.get("user_meta", response.meta)
        import json as _json
        try:
            data = _json.loads(response.text)
            if data.get("code") != 0:
                size = len(response.text)
                if size < 100:
                    logger.info(f"[mid={mid}] skip (card {size}bytes, not found)")
                    self._fetch_next_user()
                    return
                logger.warning(f"[mid={mid}] card API code={data.get('code')}")
                yield from self._space_page_fallback(mid, meta)
                return
            card = data.get("data", {}).get("card", {})
            if not card or not card.get("name"):
                logger.warning(f"[mid={mid}] card API empty")
                self._fetch_next_user()
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
            logger.warning(f"[mid={mid}] card API parse: {e}")
            self._fetch_next_user()

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
            self._fetch_next_user()

    # ================================================================
    #  Step B: 投稿列表 → 补充 UserInfoItem
    # ================================================================

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
            self._fetch_next_user()

    # ================================================================
    #  辅助方法
    # ================================================================

    def _fetch_next_user(self):
        """从 Redis 取下一个 MID，批量跳过已采集。"""
        if self._user_count >= MAX_USERS_PER_RUN:
            logger.info(f"Reached MAX_USERS_PER_RUN ({MAX_USERS_PER_RUN}), stopping")
            return

        skipped = 0
        while True:
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
                return req
            skipped += 1

        # 队列耗尽
        if skipped > 0:
            logger.info(f"Queue exhausted, {skipped} seeds checked (all collected)")
        if self._idle_start_time is None:
            self._idle_start_time = time.time()
            logger.info("Queue empty. Waiting for new seeds...")
        return None

    def _handle_error(self, failure):
        """请求失败时的通用处理，API 失败则 Playwright 兜底。"""
        request = failure.request
        mid = request.meta.get("mid", "?")
        callback_name = request.callback.__name__ if request.callback else ""
        logger.warning(f"[mid={mid}] Request failed: {failure.value}")

        # ★ 用户画像 API 失败 → Playwright 兜底
        if "parse_user_info" in callback_name:
            yield from self._fetch_user_info_playwright(mid, request.meta)
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

    def spider_idle(self):
        """
        空闲时检查 Redis 是否有新种子。
        参考 bilibili_comment_spider 的实现。
        """
        # 先检查是否还有待处理种子
        mid = self._pop_seed()
        if mid and mid not in self._seen_mids:
            self._seen_mids.add(mid)
            req = self._request_user_info(mid)
            if req:
                self.crawler.engine.crawl(req)
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
        logger.info(
            f"Spider closed ({reason}). "
            f"Users: {self._user_count}, Posts: {self._total_posts}"
        )
