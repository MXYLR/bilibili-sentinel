"""
SQLite存储实现

参考: MediaCrawler database/ 设计，使用 aiosqlite 异步操作。

数据表设计:
- videos: B站视频基本信息
- comments: 评论数据（含用户维度的水军检测字段）
- users: 用户画像信息

优势:
1. 结构化查询（SQL筛选、聚合、排序）
2. 内置去重（PRIMARY KEY约束）
3. 事务支持（批量写入原子性）
4. 比JSON文件更高效的查询
"""

import json
import os
import sqlite3
from typing import Dict, List, Optional

from store.abstract_store import AbstractStore

# SQLite DDL
CREATE_VIDEOS_TABLE = """
CREATE TABLE IF NOT EXISTS videos (
    bvid TEXT PRIMARY KEY,
    aid INTEGER,
    title TEXT,
    description TEXT,
    duration INTEGER,
    pubdate INTEGER,
    owner_mid INTEGER,
    owner_name TEXT,
    view_count INTEGER DEFAULT 0,
    danmaku_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    favorite_count INTEGER DEFAULT 0,
    coin_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    tid INTEGER,
    tname TEXT,
    tags TEXT,              -- JSON array
    pic_url TEXT,
    crawl_time TEXT
);
"""

CREATE_COMMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS comments (
    rpid INTEGER PRIMARY KEY,
    bvid TEXT NOT NULL,
    oid INTEGER,
    parent_rpid INTEGER,
    root_rpid INTEGER,
    content TEXT,
    ctime INTEGER,
    like_count INTEGER DEFAULT 0,
    mid INTEGER,
    uname TEXT,
    avatar TEXT,
    level INTEGER DEFAULT 0,
    sex TEXT,
    vip_status INTEGER DEFAULT 0,
    vip_type INTEGER DEFAULT 0,
    is_senior_member INTEGER DEFAULT 0,
    crawl_time TEXT,
    FOREIGN KEY (bvid) REFERENCES videos(bvid)
);
"""

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    mid INTEGER PRIMARY KEY,
    name TEXT,
    sex TEXT,
    face_url TEXT,
    sign TEXT,
    level INTEGER DEFAULT 0,
    birthday TEXT,
    vip_status INTEGER DEFAULT 0,
    vip_type INTEGER DEFAULT 0,
    official_verify INTEGER DEFAULT 0,
    follower_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    crawl_time TEXT
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_comments_bvid ON comments(bvid);",
    "CREATE INDEX IF NOT EXISTS idx_comments_mid ON comments(mid);",
    "CREATE INDEX IF NOT EXISTS idx_comments_ctime ON comments(ctime);",
    "CREATE INDEX IF NOT EXISTS idx_comments_level ON comments(level);",
    "CREATE INDEX IF NOT EXISTS idx_users_level ON users(level);",
]


class SqliteStore(AbstractStore):
    """
    SQLite 存储实现。

    使用标准 sqlite3 库（同步），在 Scrapy 同步 Pipeline 中直接调用。
    """

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: SQLite 数据库路径
        """
        if db_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(root, "data", "bilibili_sentinel.db")

        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

        # 初始化数据库
        self._init_db()

        # 统计
        self._counts = {"videos": 0, "comments": 0, "users": 0}

    def _init_db(self):
        """初始化数据库表"""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA cache_size=-65536;")  # 64MB

        self._conn.execute(CREATE_VIDEOS_TABLE)
        self._conn.execute(CREATE_COMMENTS_TABLE)
        self._conn.execute(CREATE_USERS_TABLE)

        for idx_sql in CREATE_INDEXES:
            self._conn.execute(idx_sql)

        self._conn.commit()

    async def store_content(self, content_item: Dict) -> None:
        """存储视频内容"""
        if not self._conn:
            self._init_db()

        try:
            tags_json = json.dumps(content_item.get("tags", []), ensure_ascii=False)

            self._conn.execute(
                """INSERT OR REPLACE INTO videos
                (bvid, aid, title, description, duration, pubdate,
                 owner_mid, owner_name, view_count, danmaku_count,
                 reply_count, favorite_count, coin_count, share_count,
                 like_count, tid, tname, tags, pic_url, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_item.get("bvid"),
                    content_item.get("aid"),
                    content_item.get("title"),
                    content_item.get("desc", ""),
                    content_item.get("duration"),
                    content_item.get("pubdate"),
                    content_item.get("owner_mid"),
                    content_item.get("owner_name"),
                    content_item.get("view_count", 0),
                    content_item.get("danmaku_count", 0),
                    content_item.get("reply_count", 0),
                    content_item.get("favorite_count", 0),
                    content_item.get("coin_count", 0),
                    content_item.get("share_count", 0),
                    content_item.get("like_count", 0),
                    content_item.get("tid"),
                    content_item.get("tname"),
                    tags_json,
                    content_item.get("pic_url"),
                    content_item.get("crawl_time"),
                ),
            )
            self._conn.commit()
            self._counts["videos"] += 1

        except sqlite3.IntegrityError:
            pass  # 重复数据跳过

    async def store_comment(self, comment_item: Dict) -> None:
        """存储评论"""
        if not self._conn:
            self._init_db()

        try:
            self._conn.execute(
                """INSERT OR IGNORE INTO comments
                (rpid, bvid, oid, parent_rpid, root_rpid,
                 content, ctime, like_count, mid, uname,
                 avatar, level, sex, vip_status, vip_type,
                 is_senior_member, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    comment_item.get("rpid"),
                    comment_item.get("bvid"),
                    comment_item.get("oid"),
                    comment_item.get("parent_rpid"),
                    comment_item.get("root_rpid"),
                    comment_item.get("content"),
                    comment_item.get("ctime"),
                    comment_item.get("like_count", 0),
                    comment_item.get("mid"),
                    comment_item.get("uname"),
                    comment_item.get("avatar"),
                    comment_item.get("level", 0),
                    comment_item.get("sex"),
                    comment_item.get("vip_status", 0),
                    comment_item.get("vip_type", 0),
                    comment_item.get("is_senior_member", 0),
                    comment_item.get("crawl_time"),
                ),
            )
            self._counts["comments"] += 1

            # 批量提交（每10条）
            if self._counts["comments"] % 10 == 0:
                self._conn.commit()

        except sqlite3.IntegrityError:
            pass  # 重复评论跳过

    async def batch_store_comments(self, comments: List[Dict]) -> None:
        """批量存储评论（事务优化）"""
        if not self._conn:
            self._init_db()

        try:
            self._conn.execute("BEGIN TRANSACTION;")
            for comment in comments:
                await self.store_comment(comment)
            self._conn.commit()
        except Exception:
            self._conn.rollback()

    async def store_creator(self, creator: Dict) -> None:
        """存储创作者信息"""
        if not self._conn:
            self._init_db()

        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO users
                (mid, name, sex, face_url, sign, level, birthday,
                 vip_status, vip_type, official_verify,
                 follower_count, following_count, video_count, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    creator.get("mid"),
                    creator.get("name"),
                    creator.get("sex"),
                    creator.get("face_url"),
                    creator.get("sign"),
                    creator.get("level", 0),
                    creator.get("birthday"),
                    creator.get("vip_status", 0),
                    creator.get("vip_type", 0),
                    creator.get("official_verify", 0),
                    creator.get("follower_count", 0),
                    creator.get("following_count", 0),
                    creator.get("video_count", 0),
                    creator.get("crawl_time"),
                ),
            )
            self._conn.commit()
            self._counts["users"] += 1

        except sqlite3.IntegrityError:
            pass

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def get_comments_by_bvid(self, bvid: str, limit: int = 100) -> List[Dict]:
        """查询指定视频的评论（SQL查询示例）"""
        if not self._conn:
            self._init_db()

        cursor = self._conn.execute(
            "SELECT * FROM comments WHERE bvid = ? ORDER BY ctime DESC LIMIT ?",
            (bvid, limit),
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @property
    def stats(self) -> Dict:
        """存储统计"""
        return dict(self._counts)

    # ============================================================
    #  同步方法（兼容 Scrapy 同步 Pipeline）
    # ============================================================

    def store_content_sync(self, content_item: Dict) -> None:
        """同步存储视频内容"""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环，直接使用同步方式
            self._store_content_direct(content_item)
        else:
            loop.create_task(self.store_content(content_item))

    def store_comment_sync(self, comment_item: Dict) -> None:
        """同步存储评论"""
        self._store_comment_direct(comment_item)

    def store_creator_sync(self, creator: Dict) -> None:
        """同步存储创作者"""
        self._store_creator_direct(creator)

    def close_sync(self) -> None:
        """同步关闭连接"""
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _store_content_direct(self, content_item: Dict) -> None:
        """直接同步存储视频（无事件循环时使用）"""
        if not self._conn:
            self._init_db()

        try:
            tags_json = json.dumps(content_item.get("tags", []), ensure_ascii=False)
            self._conn.execute(
                """INSERT OR REPLACE INTO videos
                (bvid, aid, title, description, duration, pubdate,
                 owner_mid, owner_name, view_count, danmaku_count,
                 reply_count, favorite_count, coin_count, share_count,
                 like_count, tid, tname, tags, pic_url, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_item.get("bvid"), content_item.get("aid"),
                    content_item.get("title"), content_item.get("desc", ""),
                    content_item.get("duration"), content_item.get("pubdate"),
                    content_item.get("owner_mid"), content_item.get("owner_name"),
                    content_item.get("view_count", 0), content_item.get("danmaku_count", 0),
                    content_item.get("reply_count", 0), content_item.get("favorite_count", 0),
                    content_item.get("coin_count", 0), content_item.get("share_count", 0),
                    content_item.get("like_count", 0), content_item.get("tid"),
                    content_item.get("tname"), tags_json,
                    content_item.get("pic_url"), content_item.get("crawl_time"),
                ),
            )
            self._conn.commit()
            self._counts["videos"] += 1
        except sqlite3.IntegrityError:
            pass

    def _store_comment_direct(self, comment_item: Dict) -> None:
        """直接同步存储评论"""
        if not self._conn:
            self._init_db()

        try:
            self._conn.execute(
                """INSERT OR IGNORE INTO comments
                (rpid, bvid, oid, parent_rpid, root_rpid,
                 content, ctime, like_count, mid, uname,
                 avatar, level, sex, vip_status, vip_type,
                 is_senior_member, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    comment_item.get("rpid"), comment_item.get("bvid"),
                    comment_item.get("oid"), comment_item.get("parent_rpid"),
                    comment_item.get("root_rpid"), comment_item.get("content"),
                    comment_item.get("ctime"), comment_item.get("like_count", 0),
                    comment_item.get("mid"), comment_item.get("uname"),
                    comment_item.get("avatar"), comment_item.get("level", 0),
                    comment_item.get("sex"), comment_item.get("vip_status", 0),
                    comment_item.get("vip_type", 0), comment_item.get("is_senior_member", 0),
                    comment_item.get("crawl_time"),
                ),
            )
            self._counts["comments"] += 1

            if self._counts["comments"] % 10 == 0:
                self._conn.commit()
        except sqlite3.IntegrityError:
            pass

    def _store_creator_direct(self, creator: Dict) -> None:
        """直接同步存储创作者"""
        if not self._conn:
            self._init_db()

        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO users
                (mid, name, sex, face_url, sign, level, birthday,
                 vip_status, vip_type, official_verify,
                 follower_count, following_count, video_count, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    creator.get("mid"), creator.get("name"),
                    creator.get("sex"), creator.get("face_url"),
                    creator.get("sign"), creator.get("level", 0),
                    creator.get("birthday"), creator.get("vip_status", 0),
                    creator.get("vip_type", 0), creator.get("official_verify", 0),
                    creator.get("follower_count", 0), creator.get("following_count", 0),
                    creator.get("video_count", 0), creator.get("crawl_time"),
                ),
            )
            self._conn.commit()
            self._counts["users"] += 1
        except sqlite3.IntegrityError:
            pass
