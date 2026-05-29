"""
B站弹幕采集 Spider

数据源:
  主: XML API (/x/v1/dm/list.so) — 全量弹幕(上限~8000条), XML格式, 不需要protobuf
  备: 分段 protobuf API (/x/v2/dm/web/seg.so) — 弹幕量 > 8000 时按6分钟分段采集

核心流程:
  1. 从 Redis (db=1) 读取视频种子 (复用 bilibili_crawler:start_urls)
  2. 从视频 JSON (data/videos/{bvid}.json) 中读取 cid 和 duration
  3. XML API 获取弹幕 → 如果 count=8000 说明可能被截断 → 切换分段 API
  4. yield DanmakuItem (含 mid_hash 发送者标识)

运行方式:
  scrapy crawl bilibili_danmaku
"""

import json
import logging
import os
import re
import struct
from datetime import datetime

import redis
import scrapy
from scrapy import signals
from scrapy.exceptions import DontCloseSpider

from bilibili_crawler.items import DanmakuItem

logger = logging.getLogger("bilibili_danmaku")

# ---- 安全限流 ----
MAX_SEGMENTS = 1000       # 最多 1000 个分段 (覆盖 ~100 小时视频)
SEGMENT_INTERVAL = 0.5    # 分段请求间隔 (秒)
XML_DANMAKU_LIMIT = 8000  # XML API 单次返回上限，超过则启用分段模式

# ---- Redis 配置 ----
_REDIS_HOST = "localhost"
_REDIS_PORT = 6379
_REDIS_DB = 1
_REDIS_KEY = "bilibili_crawler:start_urls"  # 复用视频种子队列

# ---- 弹幕颜色映射 ----
COLOR_MAP = {
    "16777215": "#FFFFFF",  # 白色 (默认)
    "16711680": "#FF0000",  # 红色
    "16777215": "#FFFFFF",
    "16776960": "#FFFF00",  # 黄色
    "65280": "#00FF00",     # 绿色
    "255": "#0000FF",       # 蓝色
    "16711935": "#FF00FF",  # 紫色
    "65535": "#00FFFF",     # 青色
}


def _parse_protobuf_seg(data: bytes) -> list:
    """手动解析 B站 DmSegMobileReply protobuf 二进制。

    DanmakuElem 字段:
      1: id (int64), 2: progress (int32), 3: mode (int32), 4: fontsize (int32),
      5: color (uint32), 6: midHash (string), 7: content (string), 8: ctime (int64),
      9: weight (int32), 10: action (string), 11: pool (int32), 12: idStr (string), 13: attr (int32)
    """
    def read_varint(buf, pos):
        result = 0
        shift = 0
        while pos < len(buf):
            byte = buf[pos]
            result |= (byte & 0x7F) << shift
            pos += 1
            if not (byte & 0x80):
                break
            shift += 7
        return result, pos

    def parse_elem(buf, start, end):
        result = {}
        pos = start
        while pos < end:
            tag, pos = read_varint(buf, pos)
            field_num = tag >> 3
            wire_type = tag & 0x7
            if wire_type == 0:
                value, pos = read_varint(buf, pos)
            elif wire_type == 2:
                length, pos = read_varint(buf, pos)
                value = buf[pos:pos + length]
                pos += length
            else:
                if wire_type == 0:
                    _, pos = read_varint(buf, pos)
                elif wire_type == 2:
                    length, pos = read_varint(buf, pos)
                    pos += length
                else:
                    break
                continue

            if field_num == 1:
                result["id"] = value
            elif field_num == 2:
                result["progress"] = value
            elif field_num == 3:
                result["mode"] = value
            elif field_num == 4:
                result["fontsize"] = value
            elif field_num == 5:
                result["color"] = value
            elif field_num == 6:
                result["midHash"] = value.decode("utf-8", errors="replace")
            elif field_num == 7:
                result["content"] = value.decode("utf-8", errors="replace")
            elif field_num == 8:
                result["ctime"] = value
            elif field_num == 9:
                result["weight"] = value
            elif field_num == 10:
                result["action"] = value.decode("utf-8", errors="replace")
            elif field_num == 11:
                result["pool"] = value
            elif field_num == 12:
                result["idStr"] = value.decode("utf-8", errors="replace")
            elif field_num == 13:
                result["attr"] = value
        return result

    elems = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 2 and field_num == 1:
            length, pos = read_varint(data, pos)
            parsed = parse_elem(data, pos, pos + length)
            pos += length
            if parsed:
                elems.append(parsed)
        elif wire_type == 0:
            _, pos = read_varint(data, pos)
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            pos += length
        else:
            break
    return elems


class BilibiliDanmakuSpider(scrapy.Spider):
    name = "bilibili_danmaku"
    custom_settings = {
        "SCHEDULER_IDLE_BEFORE_CLOSE": 0,
        "CONCURRENT_REQUESTS": 4,
        "DOWNLOAD_DELAY": 0.4,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._processed_videos = set()
        self._total_count = 0
        self._idle_start_time = None
        self._data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "videos",
        )

        # 从 Redis 读取种子
        self._seeds = []
        try:
            r = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
                            decode_responses=True)
            r.ping()
            while True:
                raw = r.lpop(_REDIS_KEY)
                if raw is None:
                    break
                try:
                    task = json.loads(raw) if raw.startswith("{") else {"bvid": raw}
                except Exception:
                    task = {"bvid": raw}
                bvid = task.get("bvid", "").strip()
                if bvid and bvid.startswith("BV"):
                    self._seeds.append(bvid)
        except Exception as e:
            logger.warning(f"[__init__] Redis 不可用: {e}")

        logger.info(f"[__init__] 从 Redis 读取 {len(self._seeds)} 个视频种子")

    def start_requests(self):
        for bvid in self._seeds:
            if bvid in self._processed_videos:
                continue
            self._processed_videos.add(bvid)
            # 本地 JSON 中读取 cid
            cid = self._load_cid(bvid)
            if not cid:
                logger.warning(f"[{bvid}] 本地无视频数据，跳过弹幕采集")
                continue
            # 先用 XML API (全量, 简单)
            from bilibili_crawler.utils.bilibili_api import get_danmaku_xml_url
            url = get_danmaku_xml_url(cid)
            logger.info(f"[{bvid}] 弹幕采集开始: cid={cid}")
            yield scrapy.Request(
                url,
                callback=self.parse_xml_danmaku,
                meta={"bvid": bvid, "cid": cid, "danmaku_count": 0},
            )

    def parse_xml_danmaku(self, response):
        """解析 XML 弹幕 (旧版全量 API)。"""
        bvid = response.meta["bvid"]
        cid = response.meta["cid"]

        body = response.body
        if not body:
            logger.warning(f"[{bvid}] XML 弹幕响应为空")
            return

        # 解析 XML: <d p="...">text</d>
        text = body.decode("utf-8", errors="replace")
        pattern = re.compile(r'<d p="([^"]*)"[^>]*>(.*?)</d>', re.DOTALL)
        matches = pattern.findall(text)

        logger.info(f"[{bvid}] XML 弹幕: 解析到 {len(matches)} 条")

        if len(matches) >= XML_DANMAKU_LIMIT:
            logger.warning(
                f"[{bvid}] 弹幕数达到 XML API 上限 ({XML_DANMAKU_LIMIT})，"
                f"启用分段 protobuf API 补充采集"
            )
            self._start_segmented_crawl(response, bvid, cid, len(matches))
            # 继续 yield 已解析的 XML 弹幕

        for p_attr, content in matches:
            item = self._parse_xml_danmaku(p_attr, content, bvid, cid)
            if item:
                yield item

        total_batch = response.meta.get("danmaku_count", 0) + len(matches)
        logger.info(f"[{bvid}] XML 弹幕采集完成: {total_batch} 条")

    def _start_segmented_crawl(self, response, bvid: str, cid: int, xml_count: int):
        """当 XML 不足以覆盖时，启动分段 protobuf API 采集。"""
        from bilibili_crawler.utils.bilibili_api import get_danmaku_url

        # 估算分段数: 弹幕密度约 200条/分钟, XML已覆盖约40分钟
        # 保守估计用 full duration 计算
        duration = self._load_duration(bvid)
        segment_duration = 6 * 60  # 6 分钟/段
        total_segments = min(
            (duration // segment_duration) + 2,
            MAX_SEGMENTS,
        ) if duration > 0 else 50

        logger.info(
            f"[{bvid}] 分段采集: duration={duration}s, segments={total_segments}"
        )

        for seg in range(1, total_segments + 1):
            url = get_danmaku_url(cid, segment_index=seg)
            yield scrapy.Request(
                url,
                callback=self.parse_seg_danmaku,
                meta={
                    "bvid": bvid,
                    "cid": cid,
                    "segment": seg,
                    "xml_count": xml_count,
                },
            )

    def parse_seg_danmaku(self, response):
        """解析 protobuf 分段弹幕。"""
        bvid = response.meta["bvid"]
        cid = response.meta["cid"]
        segment = response.meta["segment"]

        body = response.body
        if not body:
            return

        try:
            elems = _parse_protobuf_seg(body)
        except Exception as e:
            logger.error(f"[{bvid}] 分段 {segment} protobuf 解析失败: {e}")
            return

        for e in elems:
            item = DanmakuItem()
            item["bvid"] = bvid
            item["cid"] = cid
            item["danmaku_id"] = e.get("id", 0)
            item["content"] = e.get("content", "")
            item["progress"] = e.get("progress", 0)
            item["mode"] = e.get("mode", 1)
            item["fontsize"] = e.get("fontsize", 25)
            item["color"] = e.get("color", 16777215)
            item["send_time"] = e.get("ctime", 0)
            item["mid_hash"] = e.get("midHash", "")
            item["pool"] = e.get("pool", 0)
            item["crawl_time"] = datetime.now().isoformat()
            yield item
            self._total_count += 1

        if self._total_count % 500 == 0:
            logger.info(
                f"[{bvid}] 分段采集进度: {self._total_count} 条 "
                f"(seg={segment}/{response.meta.get('xml_count', 0)} XML)"
            )

    def _parse_xml_danmaku(self, p_attr: str, content: str, bvid: str, cid: int):
        """解析单条 XML 弹幕 p 属性。"""
        parts = p_attr.split(",")
        if len(parts) < 7:
            return None

        try:
            progress = int(float(parts[0]) * 1000)  # 秒 → 毫秒
            mode = int(parts[1])
            fontsize = int(parts[2])
            color = int(parts[3])
            send_time = int(parts[4])
            pool = int(parts[5])
            mid_hash = parts[6]
            danmaku_id = int(parts[7]) if len(parts) > 7 else 0
        except (ValueError, IndexError):
            return None

        content = content.strip()

        item = DanmakuItem()
        item["bvid"] = bvid
        item["cid"] = cid
        item["danmaku_id"] = danmaku_id
        item["content"] = content
        item["progress"] = progress
        item["mode"] = mode
        item["fontsize"] = fontsize
        item["color"] = color
        item["send_time"] = send_time
        item["mid_hash"] = mid_hash
        item["pool"] = pool
        item["crawl_time"] = datetime.now().isoformat()

        self._total_count += 1
        return item

    def _load_cid(self, bvid: str) -> int:
        """从本地 video JSON 读取 cid。"""
        path = os.path.join(self._data_dir, f"{bvid}.json")
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("cid", 0)
        except Exception:
            return 0

    def _load_duration(self, bvid: str) -> int:
        """从本地 video JSON 读取视频时长(秒)。"""
        path = os.path.join(self._data_dir, f"{bvid}.json")
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("duration", 0)
        except Exception:
            return 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_idle, signal=signals.spider_idle)
        return spider

    def spider_idle(self, spider):
        """空闲时检查 Redis 新种子。"""
        try:
            r = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=_REDIS_DB,
                            decode_responses=True)
            r.ping()
        except Exception:
            logger.info("[spider_idle] Redis 不可用，关闭弹幕爬虫")
            return

        new_seeds = 0
        while True:
            raw = r.lpop(_REDIS_KEY)
            if raw is None:
                break
            try:
                task = json.loads(raw) if raw.startswith("{") else {"bvid": raw}
            except Exception:
                task = {"bvid": raw}
            bvid = task.get("bvid", "").strip()
            if not bvid or not bvid.startswith("BV") or bvid in self._processed_videos:
                continue
            cid = self._load_cid(bvid)
            if not cid:
                continue

            self._processed_videos.add(bvid)
            new_seeds += 1

            from bilibili_crawler.utils.bilibili_api import get_danmaku_xml_url
            url = get_danmaku_xml_url(cid)
            self.crawler.engine.schedule(
                scrapy.Request(
                    url,
                    callback=self.parse_xml_danmaku,
                    meta={"bvid": bvid, "cid": cid},
                ),
                spider,
            )

        if new_seeds > 0:
            logger.info(f"[spider_idle] 处理 {new_seeds} 个新种子")
            raise DontCloseSpider
        else:
            logger.info(f"[spider_idle] 无新种子，关闭 (总计 {self._total_count} 条弹幕)")
