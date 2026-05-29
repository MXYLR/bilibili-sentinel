"""
curl_cffi Download Handler — 替代 Scrapy 默认下载器，伪装 Chrome TLS 指纹

B站 WAF 通过 JA3/JA4 TLS 指纹识别 Scrapy/Twisted 请求，返回 412。
curl_cffi 可以完美模拟 Chrome 的 TLS ClientHello，绕过指纹检测。

使用方式:
    在 settings.py 中注册:
    DOWNLOAD_HANDLERS = {
        "http": "bilibili_crawler.handlers.curl_cffi_handler.CurlCffiDownloadHandler",
        "https": "bilibili_crawler.handlers.curl_cffi_handler.CurlCffiDownloadHandler",
    }

    然后 BilibiliRequestsFallbackMiddleware 可以直接删除（curl_cffi 在下载器层工作，
    Scrapy 原生的 Request/Response 对象保持不变，所有中间件继续生效）。
"""

import logging
from urllib.parse import urlparse

from scrapy.http import HtmlResponse, Request
from scrapy.crawler import Crawler
from scrapy.settings import Settings

logger = logging.getLogger(__name__)

# curl_cffi 的 requests 兼容 API — 会自动伪装 TLS 指纹
try:
    from curl_cffi import requests as cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    _CURL_CFFI_AVAILABLE = False
    logger.warning("curl_cffi not installed; falling back to default download handler")


class CurlCffiDownloadHandler:
    """
    自定义 Scrapy DownloadHandler，使用 curl_cffi 发送 HTTP 请求。

    特点:
    - TLS 指纹 = Chrome 124（通过 impersonate 参数）
    - 与 Scrapy 原生 Response 对象完全兼容
    - 支持 SOCKS5 代理（通过 Clash）
    - 支持 Cookie、Header、超时等所有 Scrapy Request 属性
    """

    def __init__(self, settings: Settings, crawler: Crawler | None = None):
        self._settings = settings
        self._crawler = crawler
        self._session: cffi_requests.Session | None = None
        self._proxies: dict[str, str] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler):
        return cls(crawler.settings, crawler)

    def _init_session(self):
        """延迟初始化 Session（读取代理配置）"""
        if self._session is not None:
            return

        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError(
                "curl_cffi not installed. pip install curl_cffi"
            )

        self._session = cffi_requests.Session(
            # 关键：伪装成 Chrome 124 的 TLS 指纹
            impersonate="chrome124",
            verify=False,  # 中文路径下 libcurl 无法读取 CA bundle
        )

        # 应用 Clash SOCKS5 代理
        try:
            from config.base_config import CLASH_PROXY_ENABLED, CLASH_PROXY_URL
            if CLASH_PROXY_ENABLED and CLASH_PROXY_URL:
                self._proxies = {
                    "http": CLASH_PROXY_URL,
                    "https": CLASH_PROXY_URL,
                }
                self._session.proxies = self._proxies
                logger.info(
                    f"[curl_cffi] Proxy enabled: {CLASH_PROXY_URL}"
                )
        except ImportError:
            pass

        logger.info(
            f"[curl_cffi] Session initialized "
            f"(impersonate=chrome124, proxy={self._proxies is not None})"
        )

    def download_request(self, request: Request, spider):
        """
        拦截 Scrapy 下载请求，用 curl_cffi 发送。

        注意: 这个方法是 Scrapy DownloadHandler 的标准接口，
        但 curl_cffi 是同步的，需要在 Twisted 线程池中运行。
        实际上更简单的方式是在中间件层 intercept。
        """
        # 由于 Scrapy 的 DownloadHandler 接口要求返回 deferred/deferred，
        # 而 curl_cffi 是同步的，这里采用另一种方案：
        # 不在 DownloadHandler 层替换，而是在中间件层用 asyncio.to_thread
        # 因此这个类保留作为备用，主要方案在 BilibiliCurlCffiMiddleware 中
        raise NotImplementedError(
            "Use BilibiliCurlCffiMiddleware instead. "
            "See bilibili_crawler/middlewares.py"
        )


# ============================================================
# 中间件方案：在 process_request 中用 curl_cffi 发送请求
# （比替换 DownloadHandler 更简单、更兼容）
# ============================================================

class BilibiliCurlCffiMiddleware:
    """
    使用 curl_cffi 替换 BilibiliRequestsFallbackMiddleware。

    优势:
    1. TLS 指纹 = Chrome 124（JA3/JA4 完全一致）
    2. 保留 Scrapy 原生 Request/Response 流程
    3. 所有下游中间件（WBI刷新、Cookie注入、风控处理）继续正常工作
    4. 比 requests 库更接近真实浏览器

    优先级: 89（与原来的 RequestsFallbackMiddleware 相同）
    替换: BilibiliRequestsFallbackMiddleware
    """

    def __init__(self):
        self._session = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def _get_session(self):
        """延迟创建 curl_cffi Session"""
        if self._session is not None:
            return self._session

        if not _CURL_CFFI_AVAILABLE:
            logger.error(
                "[CurlCffi] curl_cffi not installed! "
                "pip install curl_cffi"
            )
            return None

        self._session = cffi_requests.Session(
            impersonate="chrome124",
            # Windows 中文路径下 libcurl 无法正确读取 certifi 的 CA bundle，
            # 设置 verify=False 绕过 SSL 验证（爬虫场景可接受）。
            # 如需启用验证：将 cacert.pem 复制到纯 ASCII 路径并设 CURL_CA_BUNDLE 环境变量。
            verify=False,
        )

        # 基础 headers（模拟 Chrome）
        self._session.headers.update({
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

        # Clash SOCKS5 代理
        try:
            from config.base_config import CLASH_PROXY_ENABLED, CLASH_PROXY_URL
            if CLASH_PROXY_ENABLED and CLASH_PROXY_URL:
                self._session.proxies = {
                    "http": CLASH_PROXY_URL,
                    "https": CLASH_PROXY_URL,
                }
                logger.info(
                    f"[CurlCffi] Clash proxy: {CLASH_PROXY_URL}"
                )
        except ImportError:
            pass

        logger.info(
            "[CurlCffi] Session ready "
            "(TLS=chrome124, PySocks OK)"
        )
        return self._session

    @staticmethod
    def _scrapy_headers_to_dict(request) -> dict:
        """将 Scrapy Headers 对象转换为普通 dict"""
        headers = {}
        for k, v in request.headers.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            # curl_cffi/requests 每个 header  value 是 str，不是 list
            if isinstance(v, (list, tuple)):
                val = v[0]
            else:
                val = v
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="replace")
            headers[key] = str(val)
        return headers

    def _make_request(self, url: str, headers: dict, timeout: int = 30):
        """用 curl_cffi Session 发送 GET 请求"""
        session = self._get_session()
        if session is None:
            raise RuntimeError("curl_cffi session not available")
        resp = session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        return resp

    @staticmethod
    def _build_scrapy_response(resp, request):
        """将 curl_cffi Response 转换为 Scrapy HtmlResponse"""
        from scrapy.http import HtmlResponse

        # curl_cffi 已自动解压 gzip/br/zstd，
        # resp.content 是原始响应 body（已解压）
        # 必须移除 Content-Encoding，否则 Scrapy 会二次解压
        scrapy_headers = {}
        for k, v in resp.headers.items():
            k_str = k.decode() if isinstance(k, bytes) else str(k)
            k_lower = k_str.lower()
            if k_lower in ("content-encoding", "transfer-encoding"):
                continue  # 跳过压缩相关 header（curl_cffi 已自动解压）
            key_b = k.encode() if isinstance(k, str) else k
            val = v.encode() if isinstance(v, str) else v
            scrapy_headers[key_b] = [val]

        return HtmlResponse(
            url=resp.url,
            status=resp.status_code,
            headers=scrapy_headers,
            body=resp.content,
            request=request,
            encoding="utf-8",
        )

    async def process_request(self, request, spider):
        """
        拦截 B站 API 请求，用 curl_cffi 发送（绕过 TLS 指纹检测）。

        只处理 api.bilibili.com 的请求；
        其他请求（如静态资源）交给 Scrapy 默认下载器。
        """
        if "api.bilibili.com" not in request.url:
            return None  # 交给默认下载器

        import asyncio

        headers = self._scrapy_headers_to_dict(request)
        spider.logger.debug(
            f"[CurlCffi] Fetching: {request.url[:100]}"
        )

        try:
            resp = await asyncio.to_thread(
                self._make_request, request.url, headers,
            )
            scrapy_resp = self._build_scrapy_response(resp, request)

            spider.logger.info(
                f"[CurlCffi] {resp.status_code} "
                f"{request.url[:80]} "
                f"(TLS=chrome124, "
                f"size={len(scrapy_resp.body)} bytes)"
            )
            return scrapy_resp

        except Exception as e:
            spider.logger.error(
                f"[CurlCffi] Request failed: {request.url[:80]} — {e}"
            )
            from scrapy.http import HtmlResponse
            return HtmlResponse(
                url=request.url,
                status=500,
                body=b"",
                request=request,
            )
