"""
JSON存储实现

将现有 BilibiliJsonPipeline 逻辑重构为 AbstractStore 实现。
完全向后兼容旧数据格式。

参考: MediaCrawler store/ 各平台 JSON 存储实现
"""

import json
import os
from collections import OrderedDict
from typing import Dict, List

from store.abstract_store import AbstractStore


class JsonStore(AbstractStore):
    """
    JSON 文件存储实现。

    存储策略 (保持与 BilibiliJsonPipeline 完全兼容):
      data/videos/{bvid}.json              → 视频信息
      data/comments/{bvid}_comments.json   → 该视频所有评论
      data/users/{mid}.json                → 用户信息

    设计原则: 每个视频一个评论文件，便于分析引擎按视频粒度处理。
    """

    def __init__(self, data_dir: str = None):
        """
        Args:
            data_dir: 数据根目录，默认使用项目根下的 data/
        """
        if data_dir is None:
            # 默认路径
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(root, "data")

        self._data_dir = data_dir
        self._video_dir = os.path.join(data_dir, "videos")
        self._comment_dir = os.path.join(data_dir, "comments")
        self._user_dir = os.path.join(data_dir, "users")

        for d in [self._video_dir, self._comment_dir, self._user_dir]:
            os.makedirs(d, exist_ok=True)

        # 评论缓冲: {bvid: [comments]}
        self._comment_buf: Dict[str, List[Dict]] = OrderedDict()
        self._buf_size = 10  # flush every 10 items

        # 统计
        self._counts = {"videos": 0, "comments": 0, "users": 0}

    async def store_content(self, content_item: Dict) -> None:
        """存储视频内容"""
        bvid = content_item.get("bvid", "")
        if not bvid:
            return

        path = os.path.join(self._video_dir, f"{bvid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content_item, f, ensure_ascii=False, indent=2)

        self._counts["videos"] += 1

    async def store_comment(self, comment_item: Dict) -> None:
        """存储评论（含缓冲+批量写入+文件级去重）"""
        bvid = comment_item.get("bvid", "")
        if not bvid:
            return

        if bvid not in self._comment_buf:
            self._comment_buf[bvid] = []
        self._comment_buf[bvid].append(comment_item)

        if len(self._comment_buf[bvid]) >= self._buf_size:
            self._flush_comments(bvid)

        self._counts["comments"] += 1

    async def batch_store_comments(self, comments: List[Dict]) -> None:
        """批量存储评论"""
        for comment in comments:
            await self.store_comment(comment)

    async def store_creator(self, creator: Dict) -> None:
        """存储创作者信息"""
        mid = creator.get("mid", "")
        if not mid:
            return

        path = os.path.join(self._user_dir, f"{mid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(creator, f, ensure_ascii=False, indent=2)

        self._counts["users"] += 1

    async def close(self) -> None:
        """Flush 所有缓冲评论"""
        for bvid in list(self._comment_buf.keys()):
            if self._comment_buf[bvid]:
                self._flush_comments(bvid)

    def _load_existing_comments(self, bvid: str) -> List[Dict]:
        """加载已有评论列表"""
        path = os.path.join(self._comment_dir, f"{bvid}_comments.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def _flush_comments(self, bvid: str) -> None:
        """将缓冲评论写入文件（附加+去重）"""
        path = os.path.join(self._comment_dir, f"{bvid}_comments.json")
        existing = self._load_existing_comments(bvid)
        existing_rpids = {c.get("rpid") for c in existing}

        new_comments = self._comment_buf.get(bvid, [])
        if not new_comments:
            return

        # 按 rpid 去重
        unique_new = [c for c in new_comments if c.get("rpid") not in existing_rpids]
        existing.extend(unique_new)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        self._comment_buf[bvid] = []

    @property
    def stats(self) -> Dict:
        """存储统计"""
        return dict(self._counts)
