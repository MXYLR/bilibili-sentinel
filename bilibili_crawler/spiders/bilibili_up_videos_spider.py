"""
B站 UP主投稿视频爬虫 (v2.14 → v2.16)

从指定 UP主 MID 的 Redis 种子出发，调用 /x/space/wbi/arc/search API
分页爬取该 UP主的所有投稿视频列表，并联动注入评论种子。

数据流:
  Redis: bilibili_crawler:up_video_seeds → Spider → UpVideoItem → UpVideosPipeline
                                                 ↓
                                 comment_seeds → bilibili_comment spider
  输出: data/up_videos/{mid}_videos.json + data/comments/{bvid}_comments.json

种子格式 (Redis 队列):
  JSON: {"mid": 123456}

特性:
  - 自动分页, 每页 50 条, 直到 page.count 耗尽
  - WBI 签名 (复用 bilibili_api.build_api_url)
  - Cookie 注入 (可获取更完整的作者信息)
  - 412 风控自适应退避
  - v2.16: 视频产出后自动注入评论种子 → bilibili_comment 爬虫
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import scrapy
import redis as redis_lib

from bilibili_crawler.items import UpVideoItem
from bilibili_crawler.utils.bilibili_api import (
    get_user_videos_url,
    parse_bilibili_response,
    prewarm_wbi_cache,
)

logger = logging.getLogger(__name__)

# 种子 Redis key
UP_VIDEO_SEEDS_KEY = "bilibili_crawler:up_video_seeds"


class BilibiliUpVideosSpider(scrapy.Spider):
    """爬取指定 UP主 的所有投稿视频。"""

    name = "bilibili_up_videos"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 0.5,
        "SCHEDULER_IDLE_BEFORE_CLOSE": 0,  # 由 spider_idle 自主管理生命周期
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Redis 客户端
        try:
            self._redis = redis_lib.Redis(
                host="localhost", port=6379, db=1, decode_responses=True
            )
            self._redis.ping()
        except Exception:
            self._redis = None
            logger.warning("Redis unavailable, will use empty seed list")

        # 统计
        self._videos_collected = 0
        self._pages_fetched = 0
        self._total_videos = 0
        self._start_time = time.time()
        self._idle_start = None  # v2.16: 追踪空闲开始时间
        self.max_idle_time = 120  # 最多等待 120s 新种子

        # WBI 预热 (可选, 如果本地缓存已过期则刷新)
        try:
            prewarm_wbi_cache()
        except Exception:
            logger.debug("WBI prewarm skipped (offline or network issue)")

    def start_requests(self):
        """从 Redis 队列读取 UP主 MID 种子，构造初始请求。"""
        seeds = []
        if self._redis:
            try:
                # 非阻塞读取所有种子
                while True:
                    raw = self._redis.lpop(UP_VIDEO_SEEDS_KEY)
                    if not raw:
                        break
                    seed = json.loads(raw) if isinstance(raw, str) else raw
                    mid = seed.get("mid", 0)
                    if mid and mid > 0:
                        seeds.append(mid)
            except Exception as e:
                logger.error(f"Failed to read seeds from Redis: {e}")

        if not seeds:
            logger.warning(
                "No UP video seeds found. Use inject_seeds('up_videos', mid=XXX) "
                "from Dashboard or inject via Redis: LPUSH bilibili_crawler:up_video_seeds '{\"mid\":123}'"
            )
            return

        # 去重
        seeds = list(dict.fromkeys(seeds))
        logger.info(f"Loaded {len(seeds)} UP mid seeds: {seeds}")

        for mid in seeds:
            url = get_user_videos_url(mid, page=1)
            yield scrapy.Request(
                url=url,
                callback=self.parse_video_list,
                meta={
                    "mid": mid,
                    "page": 1,
                    "up_name": "",
                },
                errback=self._handle_error,
                dont_filter=True,
            )

    # ================================================================
    #  视频列表解析
    # ================================================================

    def parse_video_list(self, response):
        """解析 /x/space/wbi/arc/search 响应，提取视频列表。"""
        mid = response.meta["mid"]
        page = response.meta.get("page", 1)
        up_name = response.meta.get("up_name", "")

        import json as _json
        result = parse_bilibili_response(_json.loads(response.text))
        if result is None:
            # API 失败, 记录但不中断后续页
            logger.warning(f"[mid={mid}] Page {page} API returned error or empty")
            return

        data = result.get("data", {})
        page_info = data.get("page", {})
        total_count = page_info.get("count", 0)
        page_size = page_info.get("ps", 50)
        current_page = page_info.get("pn", page)

        if self._total_videos == 0 and total_count > 0:
            self._total_videos = total_count
            logger.info(f"[mid={mid}] Total videos: {total_count}")

        vlist = data.get("list", {}).get("vlist", [])
        if not vlist:
            logger.debug(f"[mid={mid}] Page {page} has no videos (end of list)")
            return

        self._pages_fetched += 1

        # 提取 UP主名称 (从第1条视频的 author 字段)
        if not up_name and vlist:
            up_name = vlist[0].get("author", "") or ""

        # 产出 UpVideoItem
        for v in vlist:
            bvid = v.get("bvid", "")
            aid = v.get("aid", 0)
            if not bvid:
                continue

            yield UpVideoItem(
                up_mid=mid,
                up_name=up_name,
                bvid=bvid,
                aid=aid,
                title=v.get("title", ""),
                description=v.get("description", ""),
                length=v.get("length", ""),
                created=v.get("created", 0),
                pic=v.get("pic", ""),
                is_union_video=v.get("is_union_video", 0),
                is_steins_gate=v.get("is_steins_gate", 0),
                is_pay=v.get("is_pay", 0),
                play=v.get("play", 0),
                video_review=v.get("video_review", 0),
                comment=v.get("comment", 0),
                typeid=v.get("typeid", 0),
                tname=v.get("tname", ""),
                subtitle=v.get("subtitle", ""),
                crawl_time=datetime.now(timezone.utc).isoformat(),
                page=page,
                source=f"up:{mid}",
            )
            self._videos_collected += 1

            # v2.16: 联动 — 将视频注入评论爬虫队列
            if bvid and aid and v.get("comment", 0) > 0:
                self._push_comment_seed(bvid, aid, v.get("comment", 0))

            # v2.17: 联动 — 将 BV 号注入视频爬虫队列, 获取完整详情并出现在 Dashboard
            if bvid:
                self._push_video_seed(bvid)

        logger.debug(
            f"[mid={mid}] Page {page}: {len(vlist)} videos "
            f"(total collected: {self._videos_collected}/{total_count})"
        )

        # 判断是否有下一页
        total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0
        next_page = current_page + 1

        if next_page <= total_pages:
            # 自适应延迟: 每 5 页额外休息 1 秒
            if next_page % 5 == 0:
                time.sleep(1.0)

            next_url = get_user_videos_url(mid, page=next_page)
            yield scrapy.Request(
                url=next_url,
                callback=self.parse_video_list,
                meta={
                    "mid": mid,
                    "page": next_page,
                    "up_name": up_name,
                },
                errback=self._handle_error,
                dont_filter=True,
            )
        else:
            elapsed = time.time() - self._start_time
            logger.info(
                f"[mid={mid}] Complete! {self._videos_collected} videos "
                f"across {self._pages_fetched} pages in {elapsed:.1f}s"
            )

    # ================================================================
    #  评论联动 (v2.16)
    # ================================================================

    def _push_comment_seed(self, bvid: str, aid: int, reply_count: int):
        """将视频 bvid 注入评论爬虫的 Redis 队列。"""
        if not self._redis or reply_count <= 0:
            return

        task = json.dumps({
            "bvid": bvid,
            "aid": aid,
            "reply_count": reply_count,
        })

        self._redis.lpush("bilibili_crawler:comment_seeds", task)
        logger.info(f"[mid] Seeded comment task: {bvid} (aid={aid}, replies={reply_count})")

    def _push_video_seed(self, bvid: str):
        """将视频 BV 号注入视频爬虫的 Redis 队列 (bilibili_bvid:// 种子)。
        视频爬虫会拉取完整视频详情并存入 data/videos/{bvid}.json，
        从而出现在 Dashboard 视频列表中。
        """
        if not self._redis or not bvid:
            return

        seed_url = f"bilibili_bvid://{bvid}"
        self._redis.lpush("bilibili_crawler:start_urls", seed_url)
        logger.debug(f"Seeded video task: bilibili_bvid://{bvid}")

    # ================================================================
    #  错误处理
    # ================================================================

    def _handle_error(self, failure):
        """请求失败回调: 记录并继续。"""
        request = failure.request
        mid = request.meta.get("mid", "?")
        page = request.meta.get("page", "?")
        logger.error(f"[mid={mid}] Page {page} request failed: {failure.value}")

    # ================================================================
    #  生命周期
    # ================================================================

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_idle, signal=scrapy.signals.spider_idle)
        crawler.signals.connect(spider.spider_closed, signal=scrapy.signals.spider_closed)
        return spider

    def spider_idle(self):
        """空闲时检查 Redis 是否有新种子。若无种子则在 max_idle_time 内持续等待。"""
        if not self._redis:
            return

        try:
            remaining = self._redis.llen(UP_VIDEO_SEEDS_KEY)
        except Exception:
            return

        if remaining > 0:
            # 有种子: 重置空闲计时, 立即消费
            self._idle_start = None
            logger.info(f"Spider idle, but {remaining} seeds remain in queue. Continuing...")
            from twisted.internet import reactor
            for req in self.start_requests():
                if req:
                    reactor.callLater(0.5, self.crawler.engine.crawl, req, self)
            raise scrapy.exceptions.DontCloseSpider("waiting for seeds")

        # 无种子: 首次空闲时记录时间, 后续空闲时检查是否超时
        if self._idle_start is None:
            self._idle_start = time.time()
            logger.info(f"No seeds in queue. Will wait up to {self.max_idle_time}s for new seeds...")
            raise scrapy.exceptions.DontCloseSpider("waiting for seeds (initial)")
        else:
            waited = time.time() - self._idle_start
            if waited < self.max_idle_time:
                logger.info(f"No seeds yet ({waited:.0f}s / {self.max_idle_time}s). Waiting...")
                raise scrapy.exceptions.DontCloseSpider("waiting for seeds")
            else:
                logger.info(f"No seeds after {waited:.0f}s — closing spider.")

    def spider_closed(self, spider, reason):
        elapsed = time.time() - self._start_time
        logger.info(
            f"Spider [{self.name}] closed ({reason}): "
            f"{self._videos_collected} videos, "
            f"{self._pages_fetched} pages, "
            f"{elapsed:.1f}s elapsed"
        )
