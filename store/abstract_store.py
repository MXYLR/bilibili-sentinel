"""
存储抽象基类

参考: MediaCrawler base/base_crawler.py AbstractStore

定义内容、评论、创作者数据的持久化接口。
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class AbstractStore(ABC):
    """
    数据存储抽象基类。

    参考 MediaCrawler 的三个核心存储接口:
    - store_content: 内容项（视频/帖子）
    - store_comment: 评论项
    - store_creator: 创作者信息
    """

    @abstractmethod
    async def store_content(self, content_item: Dict) -> None:
        """
        存储内容项。

        Args:
            content_item: 内容数据字典（VideoItem.to_dict()）
        """
        pass

    @abstractmethod
    async def store_comment(self, comment_item: Dict) -> None:
        """
        存储评论项。

        Args:
            comment_item: 评论数据字典（CommentItem.to_dict()）
        """
        pass

    @abstractmethod
    async def store_creator(self, creator: Dict) -> None:
        """
        存储创作者信息。

        Args:
            creator: 创作者数据字典（UserInfoItem.to_dict()）
        """
        pass

    async def batch_store_comments(self, comments: List[Dict]) -> None:
        """
        批量存储评论（默认逐个调用，子类可覆盖优化）。

        Args:
            comments: 评论列表
        """
        for comment in comments:
            await self.store_comment(comment)

    async def close(self) -> None:
        """关闭存储连接，flush缓冲区。子类可覆盖。"""
        pass
