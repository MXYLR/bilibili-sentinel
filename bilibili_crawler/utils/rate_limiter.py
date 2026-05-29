"""
B站 API 请求频率控制器

API 模式爬虫不经过 Scrapy AutoThrottle，需要手动限速。
在此中间件中实现精确的请求间隔控制。
"""

import time
import random

from config import REQUEST_INTERVAL


class RateLimiter:
    """
    简单的请求频率控制器。

    用法:
        limiter = RateLimiter(interval=0.34)
        for url in urls:
            limiter.wait()
            response = requests.get(url)
    """

    def __init__(self, interval: float = None):
        self.interval = interval or REQUEST_INTERVAL
        self._last_request = 0.0

    def wait(self):
        """等待直到可以发送下一个请求"""
        elapsed = time.time() - self._last_request
        if elapsed < self.interval:
            # 添加随机抖动 ±20%
            jitter = random.uniform(-0.2, 0.2) * self.interval
            sleep_time = max(0, self.interval - elapsed + jitter)
            time.sleep(sleep_time)
        self._last_request = time.time()

    def reset(self):
        """重置计时器"""
        self._last_request = 0.0


# 全局单例 (非线程安全，但 Scrapy 的 Downloader 在主线程请求)
_global_limiter = RateLimiter()


def wait_for_rate_limit():
    """全局限速入口"""
    _global_limiter.wait()
