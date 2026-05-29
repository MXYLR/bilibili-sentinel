"""
B站爬虫专用中间件

参考 MediaCrawler 架构，支持可插拔中间件（代理/缓存/反检测）。

Seven middlewares (按 settings.py 中 DOWNLOADER_MIDDLEWARES 优先级排序):
1. BilibiliCookieMiddleware / BilibiliCookiePoolMiddleware — Cookie 注入 (priority 25/26)
2. BilibiliHeaderMiddleware — 所有请求自动添加 Referer + 随机UA (priority 50)
3. BilibiliRateLimitMiddleware — 精确频率控制 (priority 75)
4. BilibiliCurlCffiMiddleware — curl_cffi TLS 指纹伪装 (priority 89)
   ⚠️ 替换原来的 RequestsFallback，解决 B站 WAF JA3/JA4 检测（返回 412）
5. BilibiliPlaywrightFallbackMiddleware — Playwright 真实浏览器兜底 (priority 88)
   ⚠️ 当 curl_cffi 仍然 412 时，用真实 Chromium 发送（412 率 ≈ 0）
6. BilibiliWbiRefreshMiddleware — 每次请求前刷新 WBI 签名时间戳 (priority 100)
7. BilibiliProxyMiddleware — IP代理自动切换（可选，参考 MediaCrawler，priority 150）
8. BilibiliResponseMiddleware — 响应码/风控处理（含指数退避，priority 550）
"""

import json
import time
import random
import os
import logging

logger = logging.getLogger(__name__)

_BILIBILI_UAS = [
    # Chrome on Windows
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/123.0.0.0 Safari/537.36"),
    # Chrome on Mac
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36"),
    # Edge on Windows
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"),
]

# Cookie file path (relative to project root)
_COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "cookies.json",
)


class BilibiliWbiRefreshMiddleware:
    """
    每次请求发出前，动态刷新 WBI 签名中的 wts 时间戳。

    B站 WBI 签名要求 wts 与请求到达服务器的时间差不能太大（约 ±120s）。
    Scrapy 是异步框架，URL 可能在生成后很久才真正发出，
    导致 wts 过期，B站返回 -352。

    此中间件在 process_request 中检测 URL 是否含 wts= 参数，
    如果有则替换为当前时间戳并重新计算 w_rid。

    ⚠️ 重要: 此中间件返回 request.replace(url=new_url)，
    在 Scrapy 中返回 Request 会跳过后续中间件直接发给下载器。
    因此必须排在 CookieMiddleware / HeaderMiddleware / RateLimitMiddleware
    之后运行 (即 priority 值更高)，这样 replace() 才能继承前面的
    cookies / headers / rate-limit 状态。

    此外，当检测到正在使用回退密钥时，会异步尝试获取最新密钥。
    """

    # 已知的回退密钥前缀，用于检测是否需要在线刷新
    _FALLBACK_IMG_PREFIX = "653657f5"

    def __init__(self):
        self._async_refresh_pending = False

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_request(self, request, spider):
        url = request.url
        # 只处理含 WBI 签名的 B站 API 请求
        if "wts=" not in url or "w_rid=" not in url:
            return

        # 防止无限循环：已处理过的请求直接放行
        if request.meta.get("_wbi_refreshed"):
            return

        try:
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            from bilibili_crawler.utils.bilibili_api import enc_wbi, _wbi_keys_cache

            parsed = urlparse(url)
            qs = parse_qs(parsed.query)

            # 移除旧的 wts / w_rid，用当前时间戳重新签名
            qs.pop("wts", None)
            qs.pop("w_rid", None)

            params = {k: v[0] for k, v in qs.items()}

            # _wbi_keys_cache 永不为 None（模块加载时已用回退密钥初始化）
            img_key, sub_key = _wbi_keys_cache
            new_params = enc_wbi(params.copy(), img_key=img_key, sub_key=sub_key)
            new_query = urlencode(new_params)
            scheme = parsed.scheme
            netloc = parsed.netloc
            new_url = f"{scheme}://{netloc}{parsed.path}?{new_query}"
            new_req = request.replace(url=new_url)
            new_req.meta["_wbi_refreshed"] = True

            # 异步刷新: 如果正在使用回退密钥，且未在刷新中，触发异步获取
            if (img_key.startswith(self._FALLBACK_IMG_PREFIX)
                    and not self._async_refresh_pending):
                self._async_refresh_pending = True
                self._async_fetch_wbi_keys(spider)

            return new_req

        except Exception as e:
            spider.logger.warning(f"WBI 时间戳刷新失败（已跳过）: {e}")

    def _async_fetch_wbi_keys(self, spider):
        """
        异步获取最新 WBI 密钥。

        使用 Scrapy 的 Crawler.engine.download() 方法发出异步请求，
        不阻塞 Twisted 事件循环。
        """
        from bilibili_crawler.utils.bilibili_api import _wbi_keys_cache as global_cache
        from twisted.internet import reactor
        try:
            # 延迟导入避免循环依赖
            import scrapy
            from scrapy.utils.reactor import is_asyncio_reactor_installed
            from bilibili_crawler.middlewares import BilibiliWbiRefreshMiddleware
        except ImportError:
            pass

        url = "https://api.bilibili.com/x/web-interface/nav"
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Referer": "https://www.bilibili.com",
        }

        def _handle_wbi_response(response):
            """处理异步 WBI 密钥获取的响应"""
            self._async_refresh_pending = False
            try:
                data = response.json()
                wbi_img = data.get("data", {}).get("wbi_img", {})
                img_url = wbi_img.get("img_url", "")
                sub_url = wbi_img.get("sub_url", "")
                img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
                sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""

                if img_key and sub_key:
                    import bilibili_crawler.utils.bilibili_api as bili_api
                    import time
                    bili_api._wbi_keys_cache = (img_key, sub_key)
                    bili_api._wbi_keys_fetched_at = time.time()
                    spider.logger.info(
                        f"WBI keys refreshed asynchronously: "
                        f"img={img_key[:8]}..., sub={sub_key[:8]}..."
                    )
                else:
                    spider.logger.warning(
                        f"Async WBI refresh: keys not found in response"
                    )
            except Exception as e:
                spider.logger.warning(f"Async WBI refresh: failed to parse response: {e}")

        def _handle_wbi_error(failure):
            """处理异步 WBI 密钥获取的错误"""
            self._async_refresh_pending = False
            spider.logger.debug(f"Async WBI refresh failed (non-critical): {failure}")

        # 使用 Scrapy 引擎发出异步请求
        try:
            req = scrapy.Request(url, headers=headers, dont_filter=True,
                                 callback=_handle_wbi_response,
                                 errback=_handle_wbi_error)
            # 通过 crawler.engine 提交请求（不经过 scheduler）
            if hasattr(spider, 'crawler') and hasattr(spider.crawler, 'engine'):
                if hasattr(spider.crawler.engine, 'download_async'):
                    spider.crawler.engine.download_async(req)
                else:
                    spider.crawler.engine.download(req)
            else:
                self._async_refresh_pending = False
        except Exception as e:
            self._async_refresh_pending = False
            spider.logger.debug(f"Async WBI refresh initiation failed: {e}")


class BilibiliCookieMiddleware:
    """
    自动注入 B站 登录 Cookie。

    B站 评论 API 现在要求登录态（即使WBI签名正确，无Cookie也会返回-352）。

    关键发现 (2026-05-27):
    Scrapy 内置 CookiesMiddleware 将 request.cookies 转换为 Cookie header 时，
    与 Python requests 库的格式存在差异，导致 B站 API 翻页请求返回 -352。
    解决方案: 直接设置 Cookie HTTP Header，绕过 Scrapy 的 Cookie 管理。
    """

    def __init__(self):
        self._cookies = {}
        self._cookie_file = _COOKIE_FILE
        self._cookie_str = ""  # 预格式化的 Cookie header 值
        self._loaded = False

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def _ensure_cookies(self):
        """延迟加载Cookie（避免初始化时文件不存在导致失败）"""
        if self._loaded:
            return
        self._loaded = True
        try:
            if os.path.exists(self._cookie_file):
                with open(self._cookie_file, "r", encoding="utf-8") as f:
                    self._cookies = json.load(f)
                if self._cookies:
                    # 预格式化为标准 Cookie header (与 requests 库行为一致)
                    self._cookie_str = "; ".join(
                        f"{k}={v}" for k, v in self._cookies.items()
                    )
                    logger.info(
                        f"已加载 Cookie ({len(self._cookies)} 键) → Cookie header, "
                        f"SESSDATA={'***' if 'SESSDATA' in self._cookies else 'N/A'}"
                    )
                else:
                    logger.warning("Cookie 文件为空")
            else:
                logger.warning(f"Cookie 文件不存在: {self._cookie_file}")
        except Exception as e:
            logger.warning(f"加载Cookie失败: {e}")

    def process_request(self, request, spider):
        self._ensure_cookies()
        if self._cookie_str:
            # 直接设置 Cookie HTTP header，绕过 Scrapy 的 Cookie 管理系统
            # 这种方式与 Python requests 库行为一致
            request.headers["Cookie"] = self._cookie_str
            # 标记跳过 Scrapy 内置 CookiesMiddleware 的 cookie 合并
            request.meta["dont_merge_cookies"] = True
            # 同时保留 request.cookies 用于兼容 WbiRefreshMiddleware 的 replace()
            for key, value in self._cookies.items():
                request.cookies[key] = value


class BilibiliHeaderMiddleware:
    """
    自动添加 B站 必要请求头。

    B站 API 要求:
    - Referer: https://www.bilibili.com
    - User-Agent: 需包含 Chrome 标识
    """

    def process_request(self, request, spider):
        # 使用直接赋值而非 setdefault，确保覆盖 Scrapy 可能预设的 Referer
        request.headers["Referer"] = "https://www.bilibili.com"

        # v2.4: Referer 链伪造 — 如果请求 meta 中含 bvid，用真实视频页URL作为 Referer
        bvid = request.meta.get("bvid")
        if bvid and ("api.bilibili.com/x/v2/reply" in request.url
                     or "api.bilibili.com/x/v2/dm" in request.url):
            request.headers["Referer"] = f"https://www.bilibili.com/video/{bvid}"

        request.headers.setdefault(
            "User-Agent",
            random.choice(_BILIBILI_UAS),
        )
        request.headers["Accept"] = "application/json, text/plain, */*"
        request.headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7"
        # 模拟 Python requests 库的关键 header，避免 TLS 指纹差异导致 -352
        request.headers["Accept-Encoding"] = "gzip, deflate, br"
        request.headers["Connection"] = "keep-alive"

        # v2.4: 浏览器级安全头 (sec-ch-ua / sec-fetch) — 绕过 B站 WAF Client Hints 检测
        # 只在 B站 API 请求中添加，避免干扰非 B站请求
        if "api.bilibili.com" in request.url:
            request.headers["sec-ch-ua"] = (
                '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
            )
            request.headers["sec-ch-ua-mobile"] = "?0"
            request.headers["sec-ch-ua-platform"] = '"Windows"'
            request.headers["sec-fetch-dest"] = "empty"
            request.headers["sec-fetch-mode"] = "cors"
            request.headers["sec-fetch-site"] = "same-site"
            # 去掉可能暴露爬虫身份的 header
            request.headers.pop("X-Requested-With", None)


class BilibiliProxyMiddleware:
    """
    IP代理中间件（参考 MediaCrawler proxy_mixin.py）。

    为每个请求自动设置代理，并从代理池中轮询IP。
    在 Scrapy Request 中将代理信息注入 meta，供下载器使用。

    Usage:
        在 settings.py 中启用 ENABLE_IP_PROXY = True 后自动注册。
    """

    def __init__(self, proxy_pool=None):
        self._proxy_pool = proxy_pool

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        # 尝试从 crawler 获取代理池（如果已初始化）
        if hasattr(crawler.spider, "proxy_pool"):
            mw._proxy_pool = crawler.spider.proxy_pool
        return mw

    def process_request(self, request, spider):
        if self._proxy_pool is None:
            return

        try:
            # 简单的轮询模式：取当前代理
            if hasattr(self._proxy_pool, "get_proxy_nowait"):
                proxy = self._proxy_pool.get_proxy_nowait()
                if proxy:
                    request.meta["proxy"] = proxy.get("http", "")
                    self._proxy_pool.put_proxy(proxy)  # 放回队尾
        except Exception:
            pass

    def process_response(self, request, response, spider):
        """响应处理：检测代理是否失效"""
        if response.status in (403, 407, 429):
            if self._proxy_pool and hasattr(self._proxy_pool, "mark_proxy_bad"):
                proxy_used = request.meta.get("proxy", "")
                if proxy_used:
                    self._proxy_pool.mark_proxy_bad(proxy_used)
                    logger.info(f"代理失效，已标记: {proxy_used}")
        return response

    def process_exception(self, request, exception, spider):
        """请求异常：代理可能失效"""
        proxy_used = request.meta.get("proxy", "")
        if proxy_used and self._proxy_pool and hasattr(self._proxy_pool, "mark_proxy_bad"):
            self._proxy_pool.mark_proxy_bad(proxy_used)
            logger.warning(f"代理异常，已标记: {proxy_used}")


class BilibiliRateLimitMiddleware:
    """
    精确请求频率控制。

    在请求发送前检查距上次请求的时间间隔，
    不足则 sleep 补齐，保证稳定的请求频率。

    v2.4 增强: 自适应延迟
    - 正常: ~3 req/s (interval=0.34s)
    - 412 风控中: ~1 req/s (interval=1.0s) — 降低 B站 WAF 警觉
    - 恢复: 指数衰减回到正常速度
    """

    def __init__(self):
        self._last_request = 0.0
        self._interval = 0.34          # 正常: ~3 req/s
        self._slow_interval = 1.0      # 降速: ~1 req/s
        self._current_interval = 0.34  # 当前生效间隔
        self._slow_until = 0.0         # 降速截止时间戳
        self._slow_duration = 60.0     # 降速持续 60 秒

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        # Can be overridden via settings
        mw._interval = crawler.settings.getfloat("BILIBILI_REQUEST_INTERVAL", 0.34)
        mw._current_interval = mw._interval
        return mw

    def process_request(self, request, spider):
        now = time.time()

        # v2.4: 自适应降速 — 检测 spider 风控标志
        if getattr(spider, "_412_active", False):
            self._current_interval = self._slow_interval
            self._slow_until = now + self._slow_duration
            spider.logger.info(
                f"[RateLimit] 检测到 412 风控，自动降速至 "
                f"~{1/self._slow_interval:.1f} req/s (持续 {self._slow_duration}s)"
            )
            spider._412_active = False  # 只触发一次
        elif now < self._slow_until:
            # 仍在降速期
            self._current_interval = self._slow_interval
        else:
            # 恢复正常速度
            if self._current_interval != self._interval:
                spider.logger.info(
                    f"[RateLimit] 风控恢复 — 速率恢复至 "
                    f"~{1/self._interval:.1f} req/s"
                )
            self._current_interval = self._interval

        elapsed = now - self._last_request
        if elapsed < self._current_interval:
            jitter = random.uniform(-0.1, 0.1) * self._current_interval
            sleep_time = max(0, self._current_interval - elapsed + jitter)
            time.sleep(sleep_time)
        self._last_request = time.time()


class BilibiliRequestsFallbackMiddleware:
    """
    B站 API 请求使用 Python requests 库替代 Scrapy 下载器。

    根因分析 (2026-05-27):
    Scrapy/Twisted 的 TLS 指纹与 Python requests/urllib3 不同，
    导致 B站 WAF 对 Scrapy 发出的 Page 2+ 翻页请求返回 -352
    （请求校验失败 — Cookie缺失或WBI签名无效）。

    此中间件拦截所有 api.bilibili.com 请求，通过 deferToThread
    在后台线程中使用 requests 库执行实际的 HTTP 调用，
    完全绕过 Scrapy 的 Twisted 下载器和 TLS 指纹问题。

    注意: 此中间件排在 WbiRefreshMiddleware (priority 100) 之前，
    因此 URL 中的 wts 是请求构造时的值。
    B站 WBI 签名允许 wts 与服务器时间差 ±120s，足够覆盖正常延迟。
    """

    def __init__(self):
        self._session = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def _get_session(self):
        """延迟创建 requests.Session（线程安全），自动接入 Clash Verge 代理"""
        import requests as _requests
        if self._session is None:
            self._session = _requests.Session()
            self._session.headers.update({
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            })
            # --- Clash Verge 代理配置 ---
            try:
                from config.base_config import CLASH_PROXY_ENABLED, CLASH_PROXY_URL
                if CLASH_PROXY_ENABLED and CLASH_PROXY_URL:
                    self._session.proxies = {
                        "http": CLASH_PROXY_URL,
                        "https": CLASH_PROXY_URL,
                    }
                    logger.info(
                        f"[RequestsFallback] 已启用 Clash 代理: {CLASH_PROXY_URL}"
                    )
            except ImportError:
                pass
        return self._session

    @staticmethod
    def _build_headers(request):
        """从 Scrapy Request 提取 headers 为 requests 兼容格式"""
        headers = {}
        for k, v in request.headers.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            if isinstance(v, (list, tuple)):
                val = v[0]
            else:
                val = v
            if isinstance(val, bytes):
                val = val.decode()
            headers[key] = str(val)
        return headers

    def _make_request_sync(self, url, headers, timeout=30):
        """同步执行 HTTP GET 请求（在后台线程中运行）"""
        session = self._get_session()
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            return {
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.content,
                "url": resp.url,
            }
        except Exception as e:
            logger.error(
                f"[RequestsFallback] HTTP 请求失败: {url[:120]} — {e}"
            )
            raise

    @staticmethod
    def _build_scrapy_response(result, request):
        """将 requests 响应转换为 Scrapy Response 对象"""
        from scrapy.http import HtmlResponse

        # 转换 headers 为 Scrapy 兼容格式
        # CRITICAL: requests 库已自动解压 gzip/deflate，resp.content 是原始数据
        # 必须移除 Content-Encoding，否则 Scrapy 会二次解压导致 BadGzipFile
        scrapy_headers = {}
        for k, v in result["headers"].items():
            k_lower = k.lower() if isinstance(k, str) else k.decode().lower()
            if k_lower in ("content-encoding", "transfer-encoding"):
                continue  # 跳过压缩相关 header
            if isinstance(v, str):
                scrapy_headers[k.encode()] = [v.encode()]
            elif isinstance(v, bytes):
                key_b = k.encode() if isinstance(k, str) else k
                scrapy_headers[key_b] = [v]

        return HtmlResponse(
            url=result["url"],
            status=result["status"],
            headers=scrapy_headers,
            body=result["body"],
            request=request,
            encoding="utf-8",
        )

    async def process_request(self, request, spider):
        """
        拦截 B站 API 请求，使用 requests 库执行真正的 HTTP 调用。

        使用 asyncio.to_thread (Python 3.9+) 在线程池中执行 requests.get()。
        Scrapy 2.6+ 原生支持 async process_request。
        """
        # 只处理 B站 API 请求
        if "api.bilibili.com" not in request.url:
            return None

        import asyncio

        headers = self._build_headers(request)

        spider.logger.info(
            f"[RequestsFallback] 接管请求: {request.url[:120]} "
            f"(headers={list(headers.keys())})"
        )

        try:
            # 在线程池中执行同步 requests.get()，不阻塞事件循环
            result = await asyncio.to_thread(
                self._make_request_sync, request.url, headers
            )
            return self._build_scrapy_response(result, request)
        except Exception as e:
            spider.logger.error(
                f"[RequestsFallback] 请求失败: {request.url[:120]} — {e}"
            )
            from scrapy.http import HtmlResponse
            return HtmlResponse(
                url=request.url,
                status=500,
                body=b"",
                request=request,
            )


class BilibiliResponseMiddleware:
    """
    响应处理中间件（参考 MediaCrawler 自动降级 + 指数退避）。

    处理 B站 API 返回的特殊状态:
    - code=-412: 被风控 → 指数退避等待后重试 → 连续N次后触发Playwright兜底
    - code=-101: 需要登录 → 跳过当前请求
    - HTTP 429: 限流 → 根据 Retry-After 头等待

    v2.4 增强:
    - 成功响应自动清零 412 计数器（避免误触发 Playwright）
    - 自适应延迟反馈: 412 后通知 RateLimitMiddleware 降速
    """

    def __init__(self):
        self._rate_limit_counts: dict = {}       # URL级风控计数（指数退避用）
        self._consecutive_412: int = 0           # 全局连续 412 计数（Playwright触发用）
        self._412_urls: set = set()              # 已触发 412 的 URL（避免成功重置误判）

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_response(self, request, response, spider):
        # Check if response is JSON and has B站 error code
        content_type = response.headers.get("Content-Type", b"").decode()
        if "application/json" not in content_type:
            return response

        try:
            data = response.json()
        except Exception:
            return response

        code = data.get("code", 0)

        if code == -412:
            # 指数退避计算等待时间（参考 MediaCrawler 的退避策略）
            url_key = request.url.split("?")[0]
            retry_count = self._rate_limit_counts.get(url_key, 0) + 1
            self._rate_limit_counts[url_key] = retry_count

            # 全局连续 412 计数 (v2.4: 统一管理，避免双重计数)
            self._consecutive_412 += 1
            spider._412_count = self._consecutive_412
            self._412_urls.add(url_key)

            # 自适应延迟: 通知 RateLimitMiddleware 降速
            if hasattr(spider, "_412_active"):
                spider._412_active = True

            # 12s → 24s → 48s → 96s (上限)
            wait_time = min(12 * (2 ** (retry_count - 1)), 120)

            spider.logger.warning(
                f"B站风控拦截 (412): {data.get('message', '')} — "
                f"第{retry_count}次(全局连续{self._consecutive_412}次), "
                f"等待 {wait_time}s 后重试..."
            )

            # v2.4: 连续 N 次 412 → 触发 Playwright 兜底
            try:
                from config.accounts import PLAYWRIGHT_TRIGGER_412_COUNT
                trigger_count = PLAYWRIGHT_TRIGGER_412_COUNT
            except ImportError:
                trigger_count = 5

            if self._consecutive_412 >= trigger_count:
                spider._use_playwright = True
                spider.logger.warning(
                    f"[PlaywrightFallback] Triggered! "
                    f"({self._consecutive_412} consecutive 412s) — "
                    f"Switching to real browser mode (zero 412 rate)"
                )

            if retry_count > 5:
                spider.logger.error("风控重试次数超标，放弃请求")
                self._rate_limit_counts.pop(url_key, None)
                return response

            time.sleep(wait_time)
            return request.copy()  # trigger retry

        if code == -101:
            spider.logger.error(
                f"B站需要登录 (-101): {data.get('message', '')} — "
                "请配置Cookie或启用登录模块"
            )
            return response

        if code == -352:
            spider.logger.error(
                f"B站请求校验失败 (-352): {data.get('message', '')} — "
                "Cookie 缺失或 WBI 签名无效，请检查登录态"
            )
            return response

        # 成功响应，重置风控计数 (v2.4: 同时清零全局连续 412 计数)
        url_key = request.url.split("?")[0]
        self._rate_limit_counts.pop(url_key, None)
        if url_key not in self._412_urls:
            # 非风控URL的成功响应 → 全局清零
            if self._consecutive_412 > 0:
                logger.info(
                    f"[412Monitor] 正常响应恢复 — 清零连续 412 计数 "
                    f"(曾累计 {self._consecutive_412} 次)"
                )
            self._consecutive_412 = 0
            spider._412_count = 0
            # 通知 RateLimitMiddleware 恢复正常速度
            if hasattr(spider, "_412_active"):
                spider._412_active = False
        else:
            # 风控URL的成功响应 → 只清该URL，保留全局计数
            self._412_urls.discard(url_key)

        return response
