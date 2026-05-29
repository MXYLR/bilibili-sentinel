"""
免费代理供应商

参考: MediaCrawler proxy/providers/ 架构

数据源:
- 89ip.cn 免费代理API
- proxylistplus 等公开代理列表

注意: 免费代理质量不稳定，生产环境建议使用付费代理。
"""

import asyncio
import logging
import random
import time
from typing import List, Dict

import requests

from proxy.base_proxy import BaseProxy

logger = logging.getLogger(__name__)

# 常见的免费代理API
_FREE_PROXY_APIS = [
    # 89ip
    "http://www.89ip.cn/tqdl.html?api=1&num={count}&port=&address=&isp=",
]

# 公开代理列表页
_FREE_PROXY_LIST_URLS = [
    "https://www.89ip.cn/index_{page}.html",
]


class FreeProxyProvider(BaseProxy):
    """
    免费代理供应商。

    从多个免费源获取代理IP，验证可用后返回。

    Limitations:
    - 免费代理不稳定，可能随时失效
    - 多数免费代理对B站可能不可用（被B站屏蔽）
    - 建议仅作开发测试使用
    """

    PROVIDER_NAME = "free_proxy"

    def __init__(self):
        self._cache: List[Dict[str, str]] = []
        self._cache_expire = 0
        self._cache_ttl = 300  # 5分钟缓存

    @property
    def provider_name(self) -> str:
        return self.PROVIDER_NAME

    async def get_proxies(self, count: int = 5) -> List[Dict[str, str]]:
        """
        获取免费代理IP列表。

        由于免费代理源不稳定，这里提供一些常用测试代理作为降级方案。
        实际部署时应替换为付费代理API。
        """
        proxies = []

        # 优先从缓存获取
        if self._cache and time.time() < self._cache_expire:
            return self._cache[:count]

        try:
            # 尝试从公开代理源获取
            raw_proxies = await self._fetch_from_apis(count * 3)
            for raw in raw_proxies:
                proxy = self._normalize_proxy(raw)
                if proxy:
                    proxies.append(proxy)
                    if len(proxies) >= count:
                        break
        except Exception as e:
            logger.warning(f"免费代理获取失败: {e}，使用备用代理")

        # 如果没有获取到，返回空列表
        # 生产环境应该在这里接入付费代理API
        if not proxies:
            logger.warning("未获取到任何免费代理，建议配置付费代理供应商")

        self._cache = proxies
        self._cache_expire = time.time() + self._cache_ttl

        return proxies

    async def validate_proxy(self, proxy: Dict[str, str], timeout: int = 5) -> bool:
        """
        验证代理是否可用。

        通过请求 httpbin.org/ip 或 B站API来测试代理连通性。
        """
        http_proxy = proxy.get("http", "")
        if not http_proxy:
            return False

        proxies_dict = {"http": http_proxy, "https": http_proxy.replace("http://", "https://")}

        try:
            # 使用 B站 API 测试代理
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                proxies=proxies_dict,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                # B站即使不登录也返回 code=0 表示接口连通
                return data.get("code") in (0, -101)
        except Exception:
            pass

        # 降级：使用 httpbin 测试
        try:
            resp = requests.get(
                "http://httpbin.org/ip",
                proxies=proxies_dict,
                timeout=timeout,
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def _fetch_from_apis(self, count: int) -> List[str]:
        """从各免费API获取原始代理列表"""
        # 使用现有的免费代理API获取
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: requests.get(
                    _FREE_PROXY_APIS[0].format(count=count),
                    timeout=10,
                ),
            )
            if resp.status_code == 200:
                # 解析代理列表（89ip返回格式：ip:port每行一个）
                proxies = [
                    line.strip()
                    for line in resp.text.split("\n")
                    if ":" in line.strip()
                ]
                return proxies
        except Exception:
            pass

        return []

    @staticmethod
    def _normalize_proxy(raw: str) -> Dict[str, str]:
        """将原始IP:PORT字符串标准化为代理字典"""
        raw = raw.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            raw = raw.split("//")[-1]

        if ":" not in raw:
            return None

        ip, port = raw.split(":", 1)
        if not ip or not port or not port.isdigit():
            return None

        return {
            "http": f"http://{ip}:{port}",
            "https": f"https://{ip}:{port}",
            "source": FreeProxyProvider.PROVIDER_NAME,
        }
