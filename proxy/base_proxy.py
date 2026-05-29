"""
代理供应商抽象基类

参考: MediaCrawler proxy/providers/ 中的 BaseProxy
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class BaseProxy(ABC):
    """
    代理供应商抽象基类。

    所有代理供应商（免费/付费）都需要实现此接口。
    """

    @abstractmethod
    async def get_proxies(self, count: int = 5) -> List[Dict[str, str]]:
        """
        获取代理IP列表。

        Args:
            count: 需要的代理数量

        Returns:
            代理字典列表，每项格式: {
                "http": "http://ip:port",
                "https": "https://ip:port",
                "source": "provider_name",
                "expire_at": timestamp,
            }
        """
        pass

    @abstractmethod
    async def validate_proxy(self, proxy: Dict[str, str], timeout: int = 5) -> bool:
        """
        验证代理是否可用。

        Args:
            proxy: 代理字典
            timeout: 验证超时(秒)

        Returns:
            True 表示可用，False 表示失效
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """供应商名称"""
        pass
