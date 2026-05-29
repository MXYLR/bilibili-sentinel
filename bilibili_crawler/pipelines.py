"""
B站爬虫 Pipeline

Five pipelines:
1. BilibiliDedupPipeline — 去重 (基于ID, 内存去重)
2. BilibiliCleanPipeline — 数据清洗
3. BilibiliJsonPipeline — JSON文件存储 (分文件, 每视频一个)
4. UserPostsPipeline — 用户动态存储 (data/users/{mid}_posts.json)
5. UserCachePipeline — 评论者 UID 收集 + 自动注入用户种子
"""

import json
import os
import re
from collections import OrderedDict
from datetime import datetime

from bilibili_crawler.items import VideoItem, CommentItem, UserInfoItem, UserPostItem, DanmakuItem


class BilibiliDedupPipeline:
    """
    去重 Pipeline — 基于ID内存去重。

    VideoItem: 基于 bvid
    CommentItem: 基于 rpid
    UserInfoItem: 基于 mid
    UserPostItem: 基于 dynamic_id
    DanmakuItem: 基于 danmaku_id
    """

    def open_spider(self, spider):
        self._seen_videos = set()
        self._seen_comments = set()
        self._seen_users = set()
        self._seen_posts = set()
        self._seen_danmaku = set()
        spider.logger.info("Dedup pipeline opened")

    def process_item(self, item, spider):
        if isinstance(item, VideoItem):
            bvid = item.get("bvid")
            if bvid in self._seen_videos:
                return None
            self._seen_videos.add(bvid)

        elif isinstance(item, CommentItem):
            rpid = item.get("rpid")
            if rpid in self._seen_comments:
                return None
            self._seen_comments.add(rpid)

        elif isinstance(item, UserInfoItem):
            mid = item.get("mid")
            if mid in self._seen_users:
                return None
            self._seen_users.add(mid)

        elif isinstance(item, UserPostItem):
            dyn_id = item.get("dynamic_id")
            if dyn_id in self._seen_posts:
                return None
            self._seen_posts.add(dyn_id)

        elif isinstance(item, DanmakuItem):
            d_id = item.get("danmaku_id")
            if d_id in self._seen_danmaku:
                return None
            self._seen_danmaku.add(d_id)

        return item

    def close_spider(self, spider):
        spider.logger.info(
            f"Dedup stats — "
            f"Videos: {len(self._seen_videos)}, "
            f"Comments: {len(self._seen_comments)}, "
            f"Users: {len(self._seen_users)}, "
            f"Posts: {len(self._seen_posts)}, "
            f"Danmaku: {len(self._seen_danmaku)}"
        )


class BilibiliCleanPipeline:
    """
    数据清洗 Pipeline。

    对 CommentItem:
    - 去除 @提及 标签
    - 去除 B站表情符号 [xxx]
    - 规范化空白字符
    - 过滤空评论

    对 VideoItem:
    - 截断过长 desc
    """

    # B站表情正则: [doge], [笑哭], etc.
    _EMOJI_RE = re.compile(r"\[.*?\]")

    def process_item(self, item, spider):
        if isinstance(item, CommentItem):
            content = item.get("content", "")
            if content:
                # Remove @ mentions
                content = re.sub(r"@\S+\s*", "", content)
                # Remove emoji tags
                content = self._EMOJI_RE.sub("", content)
                # Normalize whitespace
                content = re.sub(r"\s+", " ", content).strip()
                item["content"] = content
                # Discard truly empty comments
                if not content:
                    return None

        elif isinstance(item, VideoItem):
            desc = item.get("desc", "")
            if desc and len(desc) > 2000:
                item["desc"] = desc[:2000] + "..."

        return item


class BilibiliJsonPipeline:
    """
    JSON 文件存储 Pipeline — 核心存储层。

    存储策略:
      data/videos/{bvid}.json          → 视频信息
      data/comments/{bvid}_comments.json → 该视频所有评论 (@append模式)
      data/users/{mid}.json            → 用户信息

    设计原则: 每个视频一个评论文件，便于分析引擎按视频粒度处理。
    """

    def open_spider(self, spider):
        # Determine project root
        self._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._video_dir = os.path.join(self._root, "data", "videos")
        self._comment_dir = os.path.join(self._root, "data", "comments")
        self._user_dir = os.path.join(self._root, "data", "users")

        for d in [self._video_dir, self._comment_dir, self._user_dir]:
            os.makedirs(d, exist_ok=True)

        # Comment buffer: {bvid: [comments]} — batch write
        self._comment_buf = OrderedDict()
        self._buf_size = 10  # flush every 10 items per video (降低以防强杀时丢失)

        # Stats
        self._counts = {"videos": 0, "comments": 0, "users": 0}

        spider.logger.info(
            f"JSON storage initialized:\n"
            f"  Videos: {self._video_dir}\n"
            f"  Comments: {self._comment_dir}\n"
            f"  Users: {self._user_dir}"
        )

    def process_item(self, item, spider):
        if isinstance(item, VideoItem):
            self._save_video(item)
            self._counts["videos"] += 1

        elif isinstance(item, CommentItem):
            self._save_comment(item, spider)
            self._counts["comments"] += 1

        elif isinstance(item, UserInfoItem):
            self._save_user(item)
            self._counts["users"] += 1

        return item

    # ---- Video ----
    def _save_video(self, item):
        path = os.path.join(self._video_dir, f"{item['bvid']}.json")
        data = dict(item)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- Comment (append to per-video file) ----
    def _save_comment(self, item, spider):
        bvid = item.get("bvid", "")
        if not bvid:
            # Try to determine bvid from oid if missing
            return

        # Buffer
        if bvid not in self._comment_buf:
            self._comment_buf[bvid] = []
        self._comment_buf[bvid].append(dict(item))

        if len(self._comment_buf[bvid]) >= self._buf_size:
            self._flush_comments(bvid)

    def _load_existing_comments(self, bvid):
        """Load existing comment list from file. Returns [] on corruption."""
        path = os.path.join(self._comment_dir, f"{bvid}_comments.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
                logger.warning(f"Comment file for {bvid} is not a list, resetting")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"Comment file for {bvid} is corrupted ({e}), "
                    f"resetting to empty. Backup kept as .corrupted"
                )
                # Keep corrupted file for forensics, write fresh one next flush
                try:
                    os.rename(path, path + ".corrupted")
                except OSError:
                    pass
        return []

    def _flush_comments(self, bvid):
        """Write buffered comments to file (append+dedup by rpid) — atomic write."""
        path = os.path.join(self._comment_dir, f"{bvid}_comments.json")
        existing = self._load_existing_comments(bvid)
        existing_rpids = {c.get("rpid") for c in existing if isinstance(c, dict)}

        new_comments = self._comment_buf.get(bvid, [])
        new_comments = [c for c in new_comments if c.get("rpid") not in existing_rpids]

        if not new_comments:
            self._comment_buf[bvid] = []
            return

        existing.extend(new_comments)

        # Atomic write: write to temp file first, then rename
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)  # atomic on same filesystem
        except Exception:
            # Clean up temp file on failure
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        self._comment_buf[bvid] = []

    # ---- User ----
    def _save_user(self, item):
        path = os.path.join(self._user_dir, f"{item['mid']}.json")
        data = dict(item)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def close_spider(self, spider):
        # Flush any remaining buffered comments
        for bvid in list(self._comment_buf.keys()):
            if self._comment_buf[bvid]:
                self._flush_comments(bvid)

        spider.logger.info(
            f"Storage complete — "
            f"Videos: {self._counts['videos']}, "
            f"Comments: {self._counts['comments']}, "
            f"Users: {self._counts['users']}"
        )


class UserPostsPipeline:
    """
    用户动态存储 Pipeline — v2.1 新增。

    存储路径: data/users/{mid}_posts.json
    格式: [{dynamic_id, content, timestamp, is_repost, post_type}, ...]

    追加模式 + 去重 (按 dynamic_id)，缓冲区 10 条 flush。
    用于 F13(转发抽奖) 和 F14(敏感内容) 检测。
    """

    def open_spider(self, spider):
        self._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._user_dir = os.path.join(self._root, "data", "users")
        os.makedirs(self._user_dir, exist_ok=True)

        # Buffer: {mid: [posts]}
        self._buf = OrderedDict()
        self._buf_size = 10
        self._count = 0

        spider.logger.info(f"UserPostsPipeline initialized: {self._user_dir}")

    def process_item(self, item, spider):
        if not isinstance(item, UserPostItem):
            return item

        mid = str(item.get("mid", ""))
        if not mid:
            return item

        if mid not in self._buf:
            self._buf[mid] = []
        self._buf[mid].append(dict(item))
        self._count += 1

        if len(self._buf[mid]) >= self._buf_size:
            self._flush_posts(mid)

        return item

    def _load_existing_posts(self, mid: str) -> list:
        path = os.path.join(self._user_dir, f"{mid}_posts.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _flush_posts(self, mid: str):
        path = os.path.join(self._user_dir, f"{mid}_posts.json")
        existing = self._load_existing_posts(mid)
        existing_ids = {p.get("dynamic_id") for p in existing if isinstance(p, dict)}

        new_posts = self._buf.get(mid, [])
        new_posts = [p for p in new_posts if p.get("dynamic_id") not in existing_ids]

        if not new_posts:
            self._buf[mid] = []
            return

        existing.extend(new_posts)

        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        self._buf[mid] = []

    def close_spider(self, spider):
        for mid in list(self._buf.keys()):
            if self._buf[mid]:
                self._flush_posts(mid)
        spider.logger.info(f"UserPostsPipeline complete — {self._count} posts stored")


class DanmakuPipeline:
    """弹幕存储 Pipeline — v2.2 新增。

    路径: data/danmaku/{bvid}_danmaku.json
    追加模式 + 去重 (按 danmaku_id)，缓冲区 200 条 flush。
    """

    def open_spider(self, spider):
        self._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._dm_dir = os.path.join(self._root, "data", "danmaku")
        os.makedirs(self._dm_dir, exist_ok=True)

        # Buffer: {bvid: [danmaku]}
        self._buf = OrderedDict()
        self._buf_size = 200
        self._count = 0

        spider.logger.info(f"DanmakuPipeline initialized: {self._dm_dir}")

    def process_item(self, item, spider):
        if not isinstance(item, DanmakuItem):
            return item

        bvid = str(item.get("bvid", ""))
        if not bvid:
            return item

        if bvid not in self._buf:
            self._buf[bvid] = []
        self._buf[bvid].append(dict(item))
        self._count += 1

        if len(self._buf[bvid]) >= self._buf_size:
            self._flush_danmaku(bvid)

        return item

    def _load_existing(self, bvid: str) -> list:
        path = os.path.join(self._dm_dir, f"{bvid}_danmaku.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _flush_danmaku(self, bvid: str):
        path = os.path.join(self._dm_dir, f"{bvid}_danmaku.json")
        existing = self._load_existing(bvid)
        existing_ids = {p.get("danmaku_id") for p in existing if isinstance(p, dict)}

        new_items = self._buf.get(bvid, [])
        new_items = [d for d in new_items
                     if d.get("danmaku_id") not in existing_ids]

        if not new_items:
            self._buf[bvid] = []
            return

        existing.extend(new_items)

        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        self._buf[bvid] = []

    def close_spider(self, spider):
        for bvid in list(self._buf.keys()):
            if self._buf[bvid]:
                self._flush_danmaku(bvid)
        spider.logger.info(
            f"DanmakuPipeline complete — {self._count} danmaku stored"
        )


class UpVideosPipeline:
    """UP主投稿视频存储 Pipeline (v2.14)。

    将 UpVideoItem 按 UP主 MID 聚合存储到 data/up_videos/{mid}_videos.json。
    支持追加写入 + aid 去重 + 原子替换。
    """

    def __init__(self):
        self._buf: dict[int, list[dict]] = {}    # mid → videos list
        self._seen: set[int] = set()             # (mid, aid) 去重
        self._up_names: dict[int, str] = {}      # mid → up_name
        self._count = 0

    def open_spider(self, spider):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._output_dir = os.path.join(root, "data", "up_videos")
        os.makedirs(self._output_dir, exist_ok=True)

    def process_item(self, item, spider):
        if not isinstance(item, UpVideoItem):
            return item

        mid = item.get("up_mid", 0)
        aid = item.get("aid", 0)
        if not mid or not aid:
            return item

        # 去重 (per mid)
        dedup_key = mid * 10_000_000_000 + aid
        if dedup_key in self._seen:
            return item
        self._seen.add(dedup_key)

        # 记录 UP主昵称
        up_name = item.get("up_name", "") or f"UID{mid}"
        self._up_names[mid] = up_name

        video = dict(item)
        self._buf.setdefault(mid, []).append(video)
        self._count += 1

        # 每收集 200 条 flush 一次
        if len(self._buf.get(mid, [])) >= 200:
            self._flush_mid(mid)

        return item

    def close_spider(self, spider):
        for mid in list(self._buf.keys()):
            self._flush_mid(mid)
        spider.logger.info(
            f"UpVideosPipeline complete — {self._count} videos across {len(self._up_names)} UPs"
        )

    def _flush_mid(self, mid: int):
        """原子写入单个 UP主 的视频列表。"""
        videos = self._buf.pop(mid, [])
        if not videos:
            return

        # 按发布时间倒序排列
        videos.sort(key=lambda v: v.get("created", 0), reverse=True)

        file_path = os.path.join(self._output_dir, f"{mid}_videos.json")

        # 原子写入
        tmp_path = file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(videos, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, file_path)

            up_name = self._up_names.get(mid, f"UID{mid}")
            spider_log = logging.getLogger(f"{__name__}.flush")
            spider_log.debug(f"Saved {len(videos)} videos for {up_name} ({mid})")
        except Exception as e:
            spider_log = logging.getLogger(f"{__name__}.flush")
            spider_log.error(f"Flush failed for mid={mid}: {e}")


class UserCachePipeline:
    """
    用户ID收集 Pipeline。

    从 CommentItem 中收集所有 distinct 的评论者 mid，
    最终写入 data/users/unique_mids.json，并自动注入 Redis 用户种子队列
    供 bilibili_user spider 消费。
    """

    def open_spider(self, spider):
        self._mids = set()
        self._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._output = os.path.join(self._root, "data", "users", "unique_mids.json")

    def process_item(self, item, spider):
        if isinstance(item, CommentItem):
            mid = item.get("mid")
            if mid:
                self._mids.add(mid)
        if isinstance(item, UserInfoItem):
            mid = item.get("mid")
            if mid:
                self._mids.add(mid)

        return item

    def close_spider(self, spider):
        spider.logger.info(f"Collected {len(self._mids)} unique user IDs")
        mids_sorted = sorted(list(self._mids))
        with open(self._output, "w", encoding="utf-8") as f:
            json.dump(mids_sorted, f)

        # ---- 自动注入用户种子到 Redis ----
        self._inject_user_seeds(mids_sorted, spider)

    def _inject_user_seeds(self, mids: list, spider):
        """将收集到的 MIDs 注入 Redis 用户种子队列。"""
        if not mids:
            return
        try:
            import redis as _redis
            r = _redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            injected = 0
            for mid in mids:
                try:
                    r.rpush("bilibili_crawler:user_seeds", json.dumps({"mid": mid}))
                    injected += 1
                except Exception:
                    break
            spider.logger.info(
                f"Auto-injected {injected}/{len(mids)} user seeds into Redis "
                f"(bilibili_crawler:user_seeds)"
            )
        except ImportError:
            spider.logger.warning("redis module not installed, skip user seed injection")
        except Exception as e:
            spider.logger.warning(f"Failed to inject user seeds: {e}")


class BilibiliSqlitePipeline:
    """
    SQLite 存储 Pipeline（新增，参考 MediaCrawler 多存储后端设计）。

    当 SAVE_DATA_OPTION = "sqlite" 时启用，替代 BilibiliJsonPipeline。
    使用 store/sqlite_store.py 的 SqliteStore 作为底层存储引擎。
    """

    def open_spider(self, spider):
        from store.sqlite_store import SqliteStore
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(root, "data", "bilibili_sentinel.db")
        self._store = SqliteStore(db_path=db_path)
        spider.logger.info(f"SQLite storage initialized: {db_path}")

    def process_item(self, item, spider):
        if isinstance(item, VideoItem):
            self._store.store_content_sync(dict(item))
        elif isinstance(item, CommentItem):
            self._store.store_comment_sync(dict(item))
        elif isinstance(item, UserInfoItem):
            self._store.store_creator_sync(dict(item))
        return item

    def close_spider(self, spider):
        stats = self._store.stats
        self._store.close_sync()
        spider.logger.info(
            f"SQLite storage complete — "
            f"Videos: {stats['videos']}, "
            f"Comments: {stats['comments']}, "
            f"Users: {stats['users']}"
        )
