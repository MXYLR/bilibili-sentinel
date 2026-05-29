"""
B站视频采集 Spider — Redis 驱动 (Scrapy-Redis)

数据源 (三选一，通过 Redis 种子 URL 控制):
  1. 热门排行榜: bilibili_hot://page/{page}
  2. 搜索关键词: bilibili_search://{keyword}/page/{page}
  3. 指定 BV 号: bilibili_bvid://{bvid}

种子 URL 通过 deploy/deploy_bilibili.py 注入 Redis。

运行方式 (单 worker):
  scrapy crawl bilibili_video

多 worker 分布式:
  start "W1" scrapy crawl bilibili_video
  start "W2" scrapy crawl bilibili_video
"""

import json
import logging
from datetime import datetime

import scrapy
import redis
from scrapy import signals
from scrapy.exceptions import DontCloseSpider
from scrapy_redis.spiders import RedisSpider

from bilibili_crawler.items import VideoItem
from bilibili_crawler.utils.bilibili_api import (
    build_api_url,
    get_popular_url,
    get_video_info,
    get_search_url,
    parse_bilibili_response,
    prewarm_wbi_cache,
)

logger = logging.getLogger(__name__)


class BilibiliVideoSpider(RedisSpider):
    """
    B站视频爬虫。

    从 Redis 队列读取种子 URL, 解析为具体任务:
    - 热门榜 → 分页拉取视频列表 → 逐个请求详情 → yield VideoItem
    - 搜索 → 同上
    - BV号 → 直接请求详情 → yield VideoItem

    采集完成后, 自动将视频 bvid 注入评论爬虫的 Redis 队列。
    """

    name = "bilibili_video"
    redis_key = "bilibili_crawler:start_urls"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 3,
        "DOWNLOAD_DELAY": 0.35,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Redis connection for comment seed injection
        self._redis = redis.Redis(
            host="localhost", port=6379, db=1, decode_responses=True
        )
        # 清空上次运行的 dupefilter，确保每次运行独立采集。
        # 否则热门榜上相同的 BV 号会被旧指纹全部过滤。
        try:
            self._redis.delete("bilibili_video:dupefilter")
            logger.info("Cleared dupefilter for fresh run")
        except Exception:
            pass
        # 同步刷新 WBI 密钥，确保首批 API 请求就使用最新密钥。
        # 在 __init__ 中调用是安全的 — Twisted reactor 尚未启动，不会阻塞事件循环。
        # 回退密钥可能对部分端点（如 /x/web-interface/view）无效。
        try:
            prewarm_wbi_cache()
        except Exception as e:
            self.logger.warning(
                f"WBI prewarm failed (will use fallback keys): {e}"
            )
        # 诊断计数器
        self._diag = {"video_ok": 0, "video_fail": 0, "fail_codes": {}}
        self._item_count = 0  # 成功产出的 VideoItem 序号
        # spider_idle 超时：max_idle_time 秒内无新种子则自动关闭
        # scrapy_redis 默认 MAX_IDLE_TIME=0（永不超时），这里设 120s
        self.max_idle_time = getattr(self, "max_idle_time", 0) or 120

    def make_request_from_data(self, data):
        """
        覆盖 RedisSpider 默认行为。
        自定义 scheme (bilibili_hot://, 等) 不是真实 URL，
        需要用 dummy URL + meta 传递种子。
        """
        url = data if isinstance(data, str) else data.decode("utf-8")
        if url.startswith("bilibili_"):
            return scrapy.Request(
                url="https://www.bilibili.com/",
                callback=self.parse,
                meta={"seed_url": url},
                dont_filter=True,
            )
        return scrapy.Request(url=url, callback=self.parse)

    def parse(self, response, **kwargs):
        """
        解析种子 URL, 分发给具体处理函数。

        种子格式:
          bilibili_hot://page/1           → 热门榜第1页
          bilibili_hot://page/1-5         → 热门榜1-5页
          bilibili_bvid://BV1xx411c7mD    → 指定视频
          bilibili_search://华为/page/1    → 搜索"华为"第1页

        如果种子是普通 URL, 也兼容直接请求。
        """
        url = response.meta.get("seed_url", response.url)

        # ---- 热门排行榜 ----
        if "bilibili_hot://" in url:
            hot_spec = url.replace("bilibili_hot://", "").strip("/")

            if hot_spec.startswith("page/"):
                page_range = hot_spec.replace("page/", "")
                if "-" in page_range:
                    parts = page_range.split("-")
                    start, end = int(parts[0]), int(parts[1])
                else:
                    start = end = int(page_range)

                self.logger.info(
                    f"[SEED] 热门排行榜 p{start}-{end} (共 {end - start + 1} 页)"
                )
                for pn in range(start, end + 1):
                    api_url = get_popular_url(pn)
                    yield scrapy.Request(
                        api_url,
                        callback=self.parse_popular_list,
                        meta={"page": pn, "source": "hot"},
                        dont_filter=True,
                    )

        # ---- 指定 BV 号 ----
        elif "bilibili_bvid://" in url:
            bvid = url.split("://")[1].strip("/")
            self.logger.info(f"[SEED] 指定 BV: {bvid}")
            api_url = get_video_info(bvid=bvid)
            yield scrapy.Request(
                api_url,
                callback=self.parse_video_api,
                meta={"bvid": bvid, "source": "bvid"},
            )

        # ---- 搜索关键词 ----
        elif "bilibili_search://" in url:
            key_part = url.replace("bilibili_search://", "").strip("/")
            parts = key_part.split("/page/")
            keyword = parts[0]
            page_num = int(parts[1]) if len(parts) > 1 else 1

            self.logger.info(f"[SEED] 搜索 '{keyword}' p{page_num}")
            api_url = get_search_url(keyword, page_num)
            yield scrapy.Request(
                api_url,
                callback=self.parse_search_results,
                meta={"keyword": keyword, "page": page_num, "source": f"search:{keyword}"},
            )

        # ---- Fallback: direct URL request ----
        else:
            yield scrapy.Request(
                url,
                callback=self.parse_video_api,
                meta={"source": "unknown"},
                dont_filter=True,
            )

    # ================================================================
    #  Parse Handlers
    # ================================================================

    def parse_popular_list(self, response):
        """解析热门排行榜 API 响应, 逐个请求视频详情"""
        raw_json = response.json()
        data = parse_bilibili_response(raw_json)
        if not data:
            return

        video_list = data.get("list", [])
        bv_list = [v.get("bvid", "?") for v in video_list if v.get("bvid")]
        bv_preview = ", ".join(bv_list[:3]) + (f" ... (+{len(bv_list)-3})" if len(bv_list) > 3 else "")
        self.logger.info(
            f"[HOT p{response.meta['page']}] {len(video_list)} videos: {bv_preview}"
        )

        for video in video_list:
            bvid = video.get("bvid")
            if not bvid:
                continue
            api_url = get_video_info(bvid=bvid)
            yield scrapy.Request(
                api_url,
                callback=self.parse_video_api,
                meta={"bvid": bvid, "source": response.meta.get("source", "hot")},
            )

    def parse_search_results(self, response):
        """解析搜索结果, 逐个请求视频详情"""
        data = parse_bilibili_response(response.json())
        if not data:
            return

        results = data.get("result", [])
        self.logger.info(
            f"Search '{response.meta['keyword']}' page {response.meta['page']}: "
            f"{len(results)} results"
        )

        for result in results:
            bvid = result.get("bvid")
            if not bvid:
                continue
            api_url = get_video_info(bvid=bvid)
            yield scrapy.Request(
                api_url,
                callback=self.parse_video_api,
                meta={"bvid": bvid, "source": response.meta.get("source", f"search:{response.meta.get('keyword','?')}")},
            )

    def parse_video_api(self, response):
        """
        解析单个视频详情 → yield VideoItem.

        B站 API 返回结构:
        {
          "code": 0,
          "data": {
            "bvid": "BV...",
            "aid": 123456,
            "title": "...",
            "desc": "...",
            "duration": 300,
            "pubdate": 1700000000,
            "cid": 789012,
            "owner": {"mid": 123, "name": "..."},
            "stat": {
              "view": 100000, "danmaku": 3000, "reply": 500,
              "favorite": 2000, "coin": 800, "share": 150, "like": 5000
            },
            "tname": "科技",
            "pic": "https://...",
            "tags": [{"tag_name": "..."}, ...]
          }
        }
        """
        bvid_trace = response.meta.get("bvid", "?")

        resp_data = parse_bilibili_response(response.json())
        if not resp_data:
            bvid_fail = response.meta.get("bvid", "?")
            raw = response.json()
            code = raw.get("code", -1)
            msg = raw.get("message", "")[:80]
            self.logger.warning(
                f"Video API returned error for {bvid_fail}: "
                f"code={code}, msg={msg}"
            )
            # 诊断: 累计失败码
            self._diag["video_fail"] += 1
            self._diag["fail_codes"][str(code)] = \
                self._diag["fail_codes"].get(str(code), 0) + 1
            return

        self._diag["video_ok"] += 1
        total = self._diag["video_ok"] + self._diag["video_fail"]
        # 每 10 条输出汇总（仅日志，不跳过处理）
        if total % 10 == 0:
            fail_summary = ", ".join(
                f"code {c}: {n}" for c, n in self._diag["fail_codes"].items()
            ) if self._diag["fail_codes"] else "none"
            self.logger.info(
                f"[VIDEO] {total} requests: OK={self._diag['video_ok']}, "
                f"FAIL={self._diag['video_fail']} ({fail_summary})"
            )

        bvid = resp_data.get("bvid") or response.meta.get("bvid")
        aid = resp_data.get("aid", 0)
        stat = resp_data.get("stat", {})

        item = VideoItem()
        item["bvid"] = bvid
        item["aid"] = aid
        item["title"] = resp_data.get("title", "")
        item["desc"] = resp_data.get("desc", "")
        item["duration"] = resp_data.get("duration", 0)
        item["pubdate"] = resp_data.get("pubdate", 0)
        item["cid"] = resp_data.get("cid", 0)
        item["owner_name"] = (resp_data.get("owner") or {}).get("name", "")
        item["owner_mid"] = (resp_data.get("owner") or {}).get("mid", 0)
        item["view_count"] = stat.get("view", 0)
        item["danmaku_count"] = stat.get("danmaku", 0)
        item["reply_count"] = stat.get("reply", 0)
        item["favorite_count"] = stat.get("favorite", 0)
        item["coin_count"] = stat.get("coin", 0)
        item["share_count"] = stat.get("share", 0)
        item["like_count"] = stat.get("like", 0)
        item["tname"] = resp_data.get("tname", "")
        # B 站图片 CDN 支持 HTTPS，统一转换避免浏览器混合内容警告
        _pic = resp_data.get("pic", "")
        item["pic"] = _pic.replace("http://", "https://", 1) if _pic.startswith("http://") else _pic
        item["tags"] = [t.get("tag_name", "") for t in resp_data.get("tags", [])]
        item["crawl_time"] = datetime.now().isoformat()
        item["source"] = response.meta.get("source", "unknown")
        yield item

        # ---- 富信息日志: 视频产出 ----
        self._item_count += 1
        view_w = item["view_count"] / 10000 if item["view_count"] else 0
        title_short = item["title"][:40] if item["title"] else "?"
        self.logger.info(
            f"[VIDEO #{self._item_count}] {bvid} \"{title_short}\" "
            f"by {item['owner_name']} | 播放:{view_w:.1f}w "
            f"评论:{item['reply_count']} 点赞:{item['like_count']}"
        )

        # ---- 联动: 将视频 bvid 注入评论爬虫队列 ----
        if bvid and aid:
            self._push_comment_seed(bvid, aid, item.get("reply_count", 0))

    def _push_comment_seed(self, bvid: str, aid: int, reply_count: int):
        """
        将视频 bvid 注入评论爬虫的 Redis 队列。

        如果评论数过高(>10000), 限制抓取页数以节省时间。
        """
        # Only push if there are comments to crawl
        if reply_count <= 0:
            return

        task = json.dumps({
            "bvid": bvid,
            "aid": aid,
            "reply_count": reply_count,
        })

        self._redis.lpush("bilibili_crawler:comment_seeds", task)
        self.logger.info(f"Seeded comment task: {bvid} (aid={aid}, replies={reply_count})")

    # ================================================================
    #  Spider Lifecycle
    # ================================================================

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        # 注意: spider_idle 已由 RedisMixin.setup_redis() 订阅，
        # 方法覆盖后 MRO 会自动调用本类的 spider_idle，无需重复订阅。
        return spider

    def spider_idle(self, spider):
        """RedisMixin 的空闲处理 + 自定义种子队列检查。

        核心: 先调用 schedule_next_requests() 从 Redis 弹出种子并调度，
        再检查队列状态决定是否继续等待。
        """
        import time

        # 1. RedisMixin 核心：调度 Redis 中的新请求
        if self.server is not None and self.count_size(self.redis_key) > 0:
            self.spider_idle_start_time = int(time.time())
        self.schedule_next_requests()

        # 2. 自定义检查：用独立 Redis 客户端检测种子余量
        try:
            pending = self._redis.llen(self.redis_key)
        except Exception as e:
            self.logger.warning(f"[spider_idle] Redis 检查失败: {e}")
            pending = 0

        if pending > 0:
            self.logger.info(
                f"[spider_idle] Redis 队列中有 {pending} 个待处理种子，继续保持运行"
            )
            raise DontCloseSpider(
                f"发现 {pending} 个视频种子，继续运行"
            )

        # 3. 超时检查 (来自 RedisMixin.spider_idle)
        idle_time = int(time.time()) - self.spider_idle_start_time
        if self.max_idle_time != 0 and idle_time >= self.max_idle_time:
            self.logger.info("[spider_idle] 无待处理种子，允许正常关闭")
            return

        self.logger.info(f"[spider_idle] 等待新种子... (空闲 {idle_time}s)")
        raise DontCloseSpider

    def spider_closed(self, spider, reason):
        """爬虫关闭时输出汇总统计"""
        fail_codes = ", ".join(
            f"{c}={n}" for c, n in self._diag["fail_codes"].items()
        ) if self._diag["fail_codes"] else "无"
        logger.info(
            f"[CLOSED] 视频爬虫结束 (reason={reason}): "
            f"成功={self._diag['video_ok']}, 失败={self._diag['video_fail']} "
            f"({fail_codes}), 产出={self._item_count} 条 VideoItem"
        )
