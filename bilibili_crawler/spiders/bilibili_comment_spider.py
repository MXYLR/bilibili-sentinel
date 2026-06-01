"""
B站评论采集 Spider — 手动从 Redis 读取种子

核心流程:
  1. start() 手动从 Redis (db=1) 读取评论种子
  2. 分页获取主评论 API (按时间排序, sort=0)
  3. 对每条高回复数的主评论, 获取子评论 (楼中楼)
  4. yield CommentItem (含用户基础信息, API内嵌)

评论 API:
  主评论: GET /x/v2/reply/main?type=1&oid={aid}&mode=3&pn={page}&ps=20
  子评论: GET /x/v2/reply/reply?type=1&oid={aid}&root={rpid}&pn={page}&ps=20

运行方式:
  scrapy crawl bilibili_comment
"""

import json
import logging
import time
import random

import redis
import scrapy
from scrapy import signals
from scrapy.exceptions import DontCloseSpider

from bilibili_crawler.items import CommentItem
from bilibili_crawler.utils.bilibili_api import (
    get_comments_url,
    get_sub_replies_url,
    parse_bilibili_response,
    prewarm_wbi_cache,
)

# 必须用爬虫名 "bilibili_comment" 作为 logger 名，
# 而非 __name__（"bilibili_crawler.spiders.bilibili_comment_spider"）。
# Dashboard 的 _read_spider_log() 用 [bilibili_comment] 过滤日志行。
logger = logging.getLogger("bilibili_comment")

# Safety limits
MAX_COMMENT_PAGES = 100     # 100 x 20 = 2000 条主评论 (v2.2: 从25提升到100)
MAX_SUB_REPLIES = 5         # 每个主评论最多翻5页子评论 (v2.2: 从3提升到5)
MAX_COMMENTS_TOTAL = 10000  # 全局上限 (v2.2: 从2000提升到10000)
MAX_IDLE_TIME = 300         # 空闲超时(s): 等待视频爬虫注入种子的最大时间
# v2.2: 双排序模式采集
ENABLE_DUAL_SORT = True     # 时间排序结束后自动切换热度排序，覆盖更多评论
SORT_SWITCH_RATIO = 0.3     # 采集量 < 预期量 * 30% 时触发模式切换
# v2.7: 失败重试
MAX_API_RETRIES = 2         # API 空响应/错误时的最大重试次数

# Redis 配置 (与 inject_seeds 一致)
_REDIS_HOST = "localhost"
_REDIS_PORT = 6379
_REDIS_DB = 1
_REDIS_KEY = "bilibili_crawler:comment_seeds"


class BilibiliCommentSpider(scrapy.Spider):
    """
    B站评论爬虫。

    从 Redis 队列接收种子 (JSON 格式):
      {"bvid": "BV1xx411c7mD", "aid": 170001, "reply_count": 500}

    然后自动:
    - 分页抓取所有主评论
    - 对热门评论抓取子评论 (楼中楼)
    - 每条 CommentItem 内含评论者等级/会员状态
    """

    name = "bilibili_comment"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 3,
        "DOWNLOAD_DELAY": 0.35,
        "DOWNLOAD_TIMEOUT": 30,
        # 不覆盖 LOG_FILE，让日志统一写入 settings.py 配置的 bilibili_crawler.log。
        # Dashboard 的 _read_spider_log() 从此文件按 [bilibili_comment] 标签过滤。
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._video_counters = {}
        self._max_pages = {}       # 每个 bvid 的 max_pages
        self._sort_mode = {}       # v2.2: 每个 bvid 当前排序模式 (0=时间, 2=热度)
        self._hot_started = {}     # v2.2: 热度模式是否已启动
        self._prepared_requests = []  # 预构建的 Request 列表
        self._idle_start_time = None  # 首次进入空闲的时间戳
        # max_idle_time: 允许从外部配置覆盖
        self.max_idle_time = getattr(self, "max_idle_time", None) or MAX_IDLE_TIME
        # v2.9: 分块爬取 — 参考 ManiaAmadevo 反风控策略
        # 每 8-15 页为一块，块间长暂停 15-30s，模拟真人行为
        self._page_chunk_counter = {}   # bvid -> 当前块内页计数
        self._chunk_size = {}         # bvid -> 当前块大小（随机 8-15）
        self._chunk_paused = {}       # bvid -> 是否正在块间暂停中
        self._seed_timer = None       # v2.16: 定时轮询 Redis 种子的 timer handle

        logger.info("[__init__] 初始化评论爬虫...")

        # ---- 预热 WBI 签名缓存 ----
        # 注意: prewarm_wbi_cache() 会强制忽略 TTL 缓存，实时获取最新密钥
        # 不能用 _fetch_wbi_keys() — 该函数在模块加载时设置 _wbi_keys_fetched_at=time.time()，
        # 导致新进程中 1 小时内永远返回回退密钥
        try:
            prewarm_wbi_cache()
        except Exception as _e:
            logger.warning(f"[__init__] WBI 签名预热失败: {_e}")

        # ---- 连接 Redis 并读取种子 ----
        try:
            import redis as _redis
            r = _redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
                             decode_responses=True)
            r.ping()
            logger.info(f"[__init__] Redis 连接成功 db={_REDIS_DB}")
        except Exception as e:
            logger.error(f"[__init__] Redis 连接失败: {e}")
            return

        seed_count = 0
        while True:
            raw = r.lpop(_REDIS_KEY)
            if raw is None:
                break
            seed_count += 1
            logger.info(f"[__init__] 种子 #{seed_count}: {raw[:100]}")

            try:
                task = json.loads(raw)
            except Exception:
                logger.warning(f"[__init__] 无效种子 JSON: {raw[:80]}")
                continue

            bvid = task.get("bvid", "")
            aid = task.get("aid", 0)
            reply_count = task.get("reply_count", 0)
            max_pages = task.get("max_pages", MAX_COMMENT_PAGES)
            sort = task.get("sort", 0)

            if not bvid:
                logger.error(f"[__init__] 种子缺少 bvid: {task}")
                continue

            # 如果只有 bvid 没有 aid，通过 API 查询 aid
            if bvid and not aid:
                try:
                    import requests
                    video_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/120.0.0.0 Safari/537.36",
                        "Referer": "https://www.bilibili.com/",
                    }
                    resp = requests.get(video_url, headers=headers, timeout=10)
                    video_data = resp.json().get("data", {})
                    aid = video_data.get("aid", 0)
                    reply_count = reply_count or video_data.get("stat", {}).get("reply", 0)
                    logger.info(f"[__init__] BVID→AID: {bvid} → aid={aid}, replies={reply_count}")
                except Exception as e:
                    logger.error(f"[__init__] BVID→AID 查询失败: {bvid}: {e}")
                    continue

            if not aid:
                logger.error(f"[__init__] 无法获取 aid: {task}")
                continue

            logger.info(f"[__init__] 准备采集 {bvid} aid={aid} max_pages={max_pages} sort={sort}")
            self._video_counters[bvid] = 0
            self._max_pages[bvid] = max_pages
            self._sort_mode[bvid] = sort  # v2.2: track active sort mode
            self._hot_started[bvid] = False  # v2.2: track if hot sort has been tried

            url = get_comments_url(aid, page=1, sort=sort)
            self.start_urls.append(url)
            self._prepared_requests.append(
                scrapy.Request(
                    url=url,
                    callback=self.parse_comments_page,
                    meta={"bvid": bvid, "aid": aid, "page": 1,
                          "reply_count": reply_count, "max_pages": max_pages,
                          "sort": sort},
                    dont_filter=True,
                )
            )

        logger.info(f"[__init__] 预构建 {len(self._prepared_requests)} 个请求（共读取 {seed_count} 个种子）")

    async def start(self):
        """
        Scrapy 2.13+ 入口。遍历预构建请求并 yield。
        """
        bvids = [req.meta.get("bvid", "?") for req in self._prepared_requests]
        bv_summary = ", ".join(bvids[:5]) + (f" ... (+{len(bvids)-5})" if len(bvids) > 5 else "")
        logger.info(
            f"[start] 处理 {len(self._prepared_requests)} 个视频的评论: {bv_summary}"
        )
        for req in self._prepared_requests:
            logger.info(f"[start] 调度: {req.meta.get('bvid', '?')} page={req.meta['page']}")
            yield req

    def parse_comments_page(self, response):
        """
        解析一页评论数据。

        v2.7: 添加失败重试机制。API 返回错误码（-352/-412）或空 replies
        时不再直接跳过，而是重试最多 MAX_API_RETRIES 次，避免因短暂
        风控/网络抖动导致整页评论永久丢失。
        """
        import time as _time

        bvid = response.meta["bvid"]
        aid = response.meta["aid"]
        page = response.meta["page"]
        sort = response.meta.get("sort", 0)
        retry_count = response.meta.get("_retry_count", 0)

        self.logger.info(
            f"[parse_comments_page] {bvid} page={page} sort={sort} "
            f"retry={retry_count}, status={response.status}"
        )
        self.logger.debug(f"[parse_comments_page] URL: {response.url[:120]}")

        # ---- Step 1: JSON 解析 ----
        try:
            resp_json = response.json()
        except Exception as e:
            self.logger.error(f"[parse_comments_page] JSON 解析失败: {e}")
            retry_req = self._retry_or_abort(response, bvid, aid, page, sort,
                                             retry_count, f"JSON解析失败: {e}")
            if retry_req:
                yield retry_req
            return

        # ---- Step 2: API 响应码检查 ----
        resp_data = parse_bilibili_response(resp_json)
        if not resp_data:
            api_code = resp_json.get("code", "?")
            api_msg = resp_json.get("message", "")
            self.logger.warning(
                f"[{bvid}] API 错误 code={api_code} page={page}: {api_msg}"
            )
            retry_req = self._retry_or_abort(response, bvid, aid, page, sort,
                                             retry_count,
                                             f"API code={api_code} msg={api_msg}")
            if retry_req:
                yield retry_req
            return

        # ---- Step 3: replies 检查 ----
        replies = resp_data.get("replies", [])
        if not replies:
            # 检查 cursor 是否真的 is_end（区分"真结束"和"空页异常"）
            cursor = resp_data.get("cursor", {})
            is_end = cursor.get("is_end", True)
            if is_end:
                self.logger.info(
                    f"[{bvid}] 第 {page} 页无评论且 API 标记 is_end=true，"
                    f"采集完毕 (共 {self._video_counters.get(bvid, 0)} 条)"
                )
                return
            # is_end=false 但 replies 为空 → 异常空页，重试
            self.logger.warning(
                f"[{bvid}] 第 {page} 页 replies 为空但 is_end=false，疑似风控"
            )
            retry_req = self._retry_or_abort(response, bvid, aid, page, sort,
                                             retry_count, "replies为空但is_end=false")
            if retry_req:
                yield retry_req
            return

        self.logger.info(f"[{bvid}] 第 {page} 页: {len(replies)} 条评论 (累计: {self._video_counters.get(bvid, 0)} 条)")

        for reply in replies:
            # ---- Global safety limit ----
            if self._video_counters.get(bvid, 0) >= MAX_COMMENTS_TOTAL:
                self.logger.warning(
                    f"[{bvid}] 达到全局上限 ({MAX_COMMENTS_TOTAL})，停止"
                )
                return

            # ---- Construct CommentItem ----
            item = CommentItem()
            item["rpid"] = reply["rpid"]
            item["oid"] = reply.get("oid", aid)
            item["type_id"] = reply.get("type", 1)
            item["bvid"] = bvid
            item["root"] = reply.get("root", 0)
            item["parent"] = reply.get("parent", 0)
            _content = reply.get("content", {})
            item["content"] = _content.get("message", "")
            item["pictures"] = _content.get("pictures", [])  # v2.16: 评论图片
            item["ctime"] = reply.get("ctime", 0)
            item["like_count"] = reply.get("like", 0)
            item["rcount"] = reply.get("rcount", 0)  # sub-reply count

            # User info (embedded in reply)
            member = reply.get("member", {})
            item["mid"] = member.get("mid", reply.get("mid", 0))
            item["uname"] = member.get("uname", "")
            item["avatar"] = member.get("avatar", "")
            item["level"] = (member.get("level_info") or {}).get("current_level", 0)
            item["sex"] = member.get("sex", "")

            # VIP info
            vip = member.get("vip", {})
            item["vip_status"] = vip.get("vipStatus", 0)
            item["vip_type"] = vip.get("vipType", 0)
            item["is_senior_member"] = 1 if vip.get("isSeniorMember", 0) else 0

            item["crawl_time"] = reply.get("ctime", 0)
            yield item
            self._video_counters[bvid] = self._video_counters.get(bvid, 0) + 1
            cur = self._video_counters[bvid]

            # ---- 内容预览日志 ----
            content = item["content"] or ""
            preview = content[:45].replace("\n", " ") + ("..." if len(content) > 45 else "")
            self.logger.info(
                f"[#{cur}] @{item['uname']}(Lv.{item['level']}) "
                f"\"{preview}\" {item['like_count']}赞"
            )

            # ---- Fetch sub-replies (楼中楼) ----
            rcount = reply.get("rcount", 0)
            if rcount > 0:
                sub_url = get_sub_replies_url(aid, reply["rpid"], page=1)
                self.logger.debug(f"[{bvid}] 抓取子评论: rpid={reply['rpid']}")
                yield scrapy.Request(
                    sub_url,
                    callback=self.parse_sub_replies,
                    meta={
                        "bvid": bvid,
                        "aid": aid,
                        "root_rpid": reply["rpid"],
                        "sub_page": 1,
                    },
                )

        # ---- v2.9: 分块爬取（参考 ManiaAmadevo 反风控策略） ----
        # 每个 bvid 独立维护块计数器，块大小为随机 8-15 页
        if bvid not in self._page_chunk_counter:
            self._page_chunk_counter[bvid] = 0
            self._chunk_size[bvid] = random.randint(8, 15)
            self._chunk_paused[bvid] = False

        self._page_chunk_counter[bvid] += 1
        chunk_page = self._page_chunk_counter[bvid]
        chunk_size = self._chunk_size[bvid]

        # ---- Pagination: next page via cursor ----
        cursor = resp_data.get("cursor", {})
        is_end = cursor.get("is_end", True)
        next_cursor = cursor.get("next", 0)

        max_pages = response.meta.get("max_pages", MAX_COMMENT_PAGES)
        reply_count = response.meta.get("reply_count", 0)
        if not is_end and page < max_pages:
            # v2.9: 块间长暂停 — 当前块页数达到随机块大小时触发
            if chunk_page >= chunk_size and not self._chunk_paused.get(bvid, False):
                long_sleep = random.uniform(15, 30)
                self.logger.warning(
                    f"[{bvid}] 块完成 ({chunk_page}/{chunk_size}页), "
                    f"暂停 {long_sleep:.1f}s 模拟真人阅读..."
                )
                self._chunk_paused[bvid] = True
                time.sleep(long_sleep)
                # 重置块计数器和块大小
                self._page_chunk_counter[bvid] = 0
                self._chunk_size[bvid] = random.randint(8, 15)
                self._chunk_paused[bvid] = False
                self.logger.info(f"[{bvid}] 块间暂停结束，下一块大小: {self._chunk_size[bvid]}页")

            # 使用 cursor (next=) + pn 双参数翻页: pn 作为兜底，next 保障连续性
            next_page = page + 1
            next_url = get_comments_url(aid, page=next_page, sort=sort, next_cursor=next_cursor)
            self.logger.info(
                f"[{bvid}] 翻页: page {page} -> {next_page} "
                f"(cursor={next_cursor}, sort={sort}, max_pages={max_pages})"
            )
            yield scrapy.Request(
                next_url,
                callback=self.parse_comments_page,
                meta={
                    "bvid": bvid,
                    "aid": aid,
                    "page": page + 1,
                    "sort": sort,
                    "max_pages": max_pages,
                    "reply_count": reply_count,
                },
                dont_filter=True,  # v2.7: 防止翻页请求被 dupefilter 误杀
            )
        else:
            # ---- v2.2: 双排序模式切换 ----
            # 时间排序 (sort=0) 耗尽后，若采集量未达标，自动切换热度排序 (sort=2)
            current_count = self._video_counters.get(bvid, 0)
            if (ENABLE_DUAL_SORT
                    and sort == 0
                    and not self._hot_started.get(bvid, False)
                    and reply_count > 0
                    and current_count < reply_count * SORT_SWITCH_RATIO):
                self._hot_started[bvid] = True
                self._sort_mode[bvid] = 2
                self.logger.info(
                    f"[{bvid}] 时间排序已结束 ({current_count}/{reply_count}条, "
                    f"{current_count/reply_count*100:.1f}%) → 切换热度排序继续采集"
                )
                hot_url = get_comments_url(aid, page=1, sort=2)
                yield scrapy.Request(
                    hot_url,
                    callback=self.parse_comments_page,
                    meta={
                        "bvid": bvid,
                        "aid": aid,
                        "page": 1,
                        "sort": 2,
                        "max_pages": max_pages,
                        "reply_count": reply_count,
                    },
                    dont_filter=True,  # v2.7: 防止热度排序请求被 dupefilter 误杀
                )
            else:
                final_count = self._video_counters.get(bvid, 0)
                pct = f"{final_count/reply_count*100:.1f}%" if reply_count > 0 else "N/A"
                self.logger.info(
                    f"[{bvid}] 采集完成 (time={self._hot_started.get(bvid, 'no')}, "
                    f"共 {final_count}/{reply_count} 条评论, 覆盖率 {pct})"
                )

    def parse_sub_replies(self, response):
        """
        解析子评论 (楼中楼)。

        子评论的结构与主评论相同, 但 root != 0, parent != 0。
        """
        bvid = response.meta["bvid"]
        aid = response.meta["aid"]
        root_rpid = response.meta["root_rpid"]
        sub_page = response.meta["sub_page"]

        try:
            resp_data = parse_bilibili_response(response.json())
        except Exception as e:
            self.logger.error(f"[parse_sub_replies] 解析失败: {e}")
            return

        if not resp_data:
            return

        replies = resp_data.get("replies", [])
        if not replies:
            return

        self.logger.info(
            f"[{bvid}] 楼中楼 root={root_rpid} p.{sub_page}: "
            f"{len(replies)} 条 (累计: {self._video_counters.get(bvid, 0)} 条)"
        )

        for reply_data in replies:
            reply = reply_data

            item = CommentItem()
            item["rpid"] = reply["rpid"]
            item["oid"] = reply.get("oid", aid)
            item["type_id"] = reply.get("type", 1)
            item["bvid"] = bvid
            item["root"] = reply.get("root", root_rpid)
            item["parent"] = reply.get("parent", reply.get("root", 0))
            _content = reply.get("content", {})
            item["content"] = _content.get("message", "")
            item["pictures"] = _content.get("pictures", [])  # v2.16
            item["ctime"] = reply.get("ctime", 0)
            item["like_count"] = reply.get("like", 0)
            item["rcount"] = reply.get("rcount", 0)

            member = reply.get("member", {})
            item["mid"] = member.get("mid", reply.get("mid", 0))
            item["uname"] = member.get("uname", "")
            item["avatar"] = member.get("avatar", "")
            item["level"] = (member.get("level_info") or {}).get("current_level", 0)
            item["sex"] = member.get("sex", "")

            vip = member.get("vip", {})
            item["vip_status"] = vip.get("vipStatus", 0)
            item["vip_type"] = vip.get("vipType", 0)
            item["is_senior_member"] = 1 if vip.get("isSeniorMember", 0) else 0

            item["crawl_time"] = reply.get("ctime", 0)
            yield item
            self._video_counters[bvid] = self._video_counters.get(bvid, 0) + 1
            cur = self._video_counters[bvid]

            # ---- 子评论内容预览 ----
            content = item["content"] or ""
            preview = content[:40].replace("\n", " ") + ("..." if len(content) > 40 else "")
            self.logger.info(
                f"[#{cur}] ↳ @{item['uname']}(Lv.{item['level']}) "
                f"\"{preview}\""
            )

        # Sub-reply pagination
        cursor = resp_data.get("cursor", {})
        if not cursor.get("is_end", True) and sub_page < MAX_SUB_REPLIES:
            next_sub_url = get_sub_replies_url(aid, root_rpid, page=sub_page + 1)
            yield scrapy.Request(
                next_sub_url,
                callback=self.parse_sub_replies,
                meta={
                    "bvid": bvid,
                    "aid": aid,
                    "root_rpid": root_rpid,
                    "sub_page": sub_page + 1,
                },
                dont_filter=True,  # v2.7: 防止子评论翻页被 dupefilter 误杀
            )

    def _retry_or_abort(self, response, bvid, aid, page, sort,
                        retry_count, reason):
        """
        v2.7: API 失败时的重试决策。

        当 B站 API 返回错误码（-352/-412）或空响应时，不直接放弃，
        而是重试最多 MAX_API_RETRIES 次。

        注意：返回的 Request 由调用方 yield 给 Scrapy 引擎；
        不在此处 sleep — Scrapy 的 DOWNLOAD_DELAY 会自然间隔请求。

        Returns:
            scrapy.Request (需 yield) 或 None (放弃)
        """
        if retry_count < MAX_API_RETRIES:
            next_retry = retry_count + 1
            self.logger.warning(
                f"[{bvid}] 第 {page} 页失败({reason})，"
                f"将重试 ({next_retry}/{MAX_API_RETRIES})"
            )
            # 重新构建请求 — 用新的 WBI 签名，避免旧的签名过期
            retry_url = get_comments_url(aid, page=page, sort=sort)
            from scrapy import Request
            return Request(
                retry_url,
                callback=self.parse_comments_page,
                meta={
                    "bvid": bvid, "aid": aid, "page": page,
                    "sort": sort,
                    "max_pages": response.meta.get("max_pages", MAX_COMMENT_PAGES),
                    "reply_count": response.meta.get("reply_count", 0),
                    "_retry_count": next_retry,
                },
                dont_filter=True,
            )
        else:
            self.logger.error(
                f"[{bvid}] 第 {page} 页重试 {MAX_API_RETRIES} 次后仍失败({reason})，"
                f"放弃该页及后续"
            )
            return None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(spider.spider_idle, signal=signals.spider_idle)
        return spider

    def spider_idle(self, spider):
        """空闲时检查 Redis 新种子，找到则继续爬，无则等定时器重试。"""
        found = self._check_and_consume_seeds(from_idle=True)
        if found:
            raise DontCloseSpider("New comment seeds consumed, continue crawling")

    def _check_and_consume_seeds(self, from_idle=False):
        """轮询 Redis 消费新种子。找到→重置空闲, 无→5s后重试, 超时→关闭。(v2.16)

        Args:
            from_idle: True=从 spider_idle 信号调用(可抛 DontCloseSpider)
                       False=从 reactor.callLater 定时器调用(不可抛,会炸 Twisted)
        """
        from twisted.internet import reactor
        import redis as _rds

        if self._seed_timer and self._seed_timer.active():
            self._seed_timer.cancel()
        self._seed_timer = None

        try:
            r = _rds.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
                           decode_responses=True)
            r.ping()
        except Exception:
            logger.warning("[_check_seeds] Redis 不可用, 5s 后重试")
            self._seed_timer = reactor.callLater(5, self._check_and_consume_seeds)
            return

        new_count = 0
        while True:
            raw = r.lpop(_REDIS_KEY)
            if raw is None:
                break
            new_count += 1
            try:
                task = json.loads(raw)
            except Exception:
                logger.warning(f"[_check_seeds] 无效种子: {raw[:80]}")
                continue
            bvid = task.get("bvid", "")
            aid = task.get("aid", 0)
            if not bvid or not aid:
                continue
            if bvid in self._video_counters and self._video_counters[bvid] > 0:
                continue
            self._video_counters[bvid] = 0
            self._max_pages[bvid] = task.get("max_pages", MAX_COMMENT_PAGES)
            req = scrapy.Request(
                url=get_comments_url(aid, page=1, sort=task.get("sort", 0)),
                callback=self.parse_comments_page,
                meta={"bvid": bvid, "aid": aid, "page": 1,
                      "reply_count": task.get("reply_count", 0),
                      "max_pages": task.get("max_pages", MAX_COMMENT_PAGES),
                      "sort": task.get("sort", 0)},
                dont_filter=True,
            )
            self.crawler.engine.crawl(req)
            logger.info(f"[_check_seeds] +{bvid} aid={aid}")

        if new_count > 0:
            self._idle_start_time = None
            return True  # 找到了种子，通知调用方

        now = time.time()
        if self._idle_start_time is None:
            self._idle_start_time = now
        elapsed = now - self._idle_start_time
        if elapsed < self.max_idle_time:
            logger.info(f"[_check_seeds] 无种子 ({elapsed:.0f}s/{self.max_idle_time}s), 5s 后重试")
            self._seed_timer = reactor.callLater(5, self._check_and_consume_seeds)
            if from_idle:
                raise DontCloseSpider("等待视频爬虫注入新种子...")
            # 定时器回调中不抛 DontCloseSpider——会炸 Twisted reactor
        else:
            logger.info(f"[_check_seeds] 空闲超时 ({elapsed:.0f}s), 允许关闭")
        return False

    def spider_closed(self, spider, reason):
        """爬虫关闭时输出汇总统计"""
        total = sum(self._video_counters.values())
        video_summary = ", ".join(
            f"{b}:{n}" for b, n in self._video_counters.items()
        ) if self._video_counters else "无"
        logger.info(
            f"[CLOSED] 评论爬虫结束 (reason={reason}): "
            f"共 {total} 条评论, 分布: {video_summary}"
        )
