"""
Cookie 池中间件 — 多账号 Cookie 轮换，规避 B站 412 单账号限速

工作原理:
1. 从 config/accounts.py 的 BILIBILI_COOKIE_POOL 读取多个账号 Cookie
2. 按策略（round_robin / random）轮换 Cookie
3. 某账号被 412 限速后，自动标记冷却（5分钟），跳过该账号
4. 与 BilibiliCookieMiddleware 互斥：
   - ENABLE_COOKIE_POOL=True   → 使用 Cookie 池（多账号轮换）
   - ENABLE_COOKIE_POOL=False  → 使用原 CookieMiddleware（单账号）

优先级: 26（紧跟在 CookieMiddleware=25 之后，在 HeaderMiddleware=50 之前）
"""

import time
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

# 导入 Cookie 池配置（延迟导入，避免循环依赖）
_cookie_pool: list[dict] = []
_strategy: str = "round_robin"
_switch_every: int = 5
_cooldown_sec: int = 300
_412_trigger_playwright: int = 3

# round_robin 计数器
_rr_index: int = 0
_rr_counter: int = 0


def _load_cookie_pool():
    """从 config/accounts.py 加载 Cookie 池配置"""
    global _cookie_pool, _strategy, _switch_every
    global _cooldown_sec, _412_trigger_playwright

    try:
        from config.accounts import (
            ENABLE_COOKIE_POOL,
            BILIBILI_COOKIE_POOL,
            COOKIE_ROTATE_STRATEGY,
            COOKIE_SWITCH_EVERY_N_REQUESTS,
            COOKIE_COOLDOWN_SECONDS,
            PLAYWRIGHT_TRIGGER_412_COUNT,
        )
        _strategy = COOKIE_ROTATE_STRATEGY
        _switch_every = COOKIE_SWITCH_EVERY_N_REQUESTS
        _cooldown_sec = COOKIE_COOLDOWN_SECONDS
        _412_trigger_playwright = PLAYWRIGHT_TRIGGER_412_COUNT

        if ENABLE_COOKIE_POOL and BILIBILI_COOKIE_POOL:
            _cookie_pool = list(BILIBILI_COOKIE_POOL)
            active = [c for c in _cookie_pool if not _is_in_cooldown(c)]
            logger.info(
                f"[CookiePool] Loaded {len(_cookie_pool)} accounts "
                f"({len(active)} active, strategy={_strategy})"
            )
        else:
            _cookie_pool = []
    except ImportError:
        _cookie_pool = []


def _is_in_cooldown(account: dict) -> bool:
    """检查账号是否在冷却期内"""
    cooldown_until = account.get("cooldown_until", 0)
    return cooldown_until > time.time()


def _get_next_cookie() -> Optional[str]:
    """
    按策略选择下一个 Cookie 字符串。

    Returns:
        Cookie header 字符串（分号分隔），或 None（无可用账号）
    """
    global _rr_index, _rr_counter

    if not _cookie_pool:
        return None

    active_accounts = [c for c in _cookie_pool if not _is_in_cooldown(c)]
    if not active_accounts:
        logger.warning("[CookiePool] All accounts in cooldown!")
        return None

    if _strategy == "random":
        chosen = random.choice(active_accounts)
    else:  # round_robin
        # 只从 active 里轮询
        _rr_counter += 1
        if _rr_counter >= _switch_every:
            _rr_counter = 0
            _rr_index = (_rr_index + 1) % max(len(active_accounts), 1)
        chosen = active_accounts[_rr_index % max(len(active_accounts), 1)]

    chosen["_last_used"] = time.time()
    return chosen["cookie"], chosen.get("tag", "unknown")


def _mark_412(account_tag: str):
    """标记某账号触发 412，进入冷却"""
    for acc in _cookie_pool:
        if acc.get("tag") == account_tag:
            acc["cooldown_until"] = time.time() + _cooldown_sec
            logger.warning(
                f"[CookiePool] Account '{account_tag}' "
                f"hit 412 — cooldown {_cooldown_sec}s"
            )
            break


# ============================================================
# Scrapy 中间件类
# ============================================================

class BilibiliCookiePoolMiddleware:
    """
    Cookie 池中间件 — 多账号轮换

    优先级: 26（在 CookieMiddleware=25 之后，HeaderMiddleware=50 之前）
    当 ENABLE_COOKIE_POOL=True 时生效，同时禁用原 CookieMiddleware。

    v2.12 风控策略:
    - 主评论 API (/x/v2/reply/main): 不注入 Cookie（避免账号维度风控）
    - 子评论 API (/x/v2/reply/reply): 注入 Cookie（获取 IP 属地）
    - 使用 request.cookies dict（非 headers["Cookie"]），
      由 curl_cffi handler 以 session.cookies={...} 传递
    """

    def __init__(self):
        self._enabled = False

    @classmethod
    def from_crawler(cls, crawler):
        obj = cls()
        try:
            from config.accounts import ENABLE_COOKIE_POOL
            obj._enabled = ENABLE_COOKIE_POOL
        except ImportError:
            obj._enabled = False

        if obj._enabled:
            _load_cookie_pool()
            if not _cookie_pool:
                logger.warning(
                    "[CookiePool] ENABLE_COOKIE_POOL=True "
                    "but BILIBILI_COOKIE_POOL is empty! "
                    "Please edit config/accounts_local.py"
                )
        return obj

    def process_request(self, request, spider):
        if not self._enabled:
            return
        if not _cookie_pool:
            return

        # v2.12 规则4: 主评论 API 不带 Cookie
        # 主评论和楼中楼 IP 风控独立，不带 Cookie 可避免账号维度风控
        if "/x/v2/reply/main" in request.url:
            spider.logger.debug(
                f"[CookiePool] 跳过主评论 Cookie 注入: {request.url[:80]}"
            )
            return

        result = _get_next_cookie()
        if result is None:
            return

        cookie_str, tag = result

        # v2.12 规则5+6: 子评论及其他 B站 API 通过 request.cookies 注入
        # 使用 request.cookies dict，由 curl_cffi handler 以
        # session.cookies={...} 方式传递（非 headers 方式）
        if cookie_str:
            for pair in cookie_str.split("; "):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    request.cookies[k] = v

        request.meta["dont_merge_cookies"] = True
        request.meta["_cookie_pool_tag"] = tag
        request.meta["_cookie_pool_time"] = time.time()

        if "/x/v2/reply/reply" in request.url:
            spider.logger.debug(
                f"[CookiePool] 子评论注入 Cookie (tag={tag}): {request.url[:80]}"
            )

    def process_response(self, request, response, spider):
        """检测 412，标记账号冷却"""
        tag = request.meta.get("_cookie_pool_tag")
        if not tag:
            return response

        # 检查 B站 JSON 错误码
        try:
            if response.headers.get("Content-Type", b"").decode().startswith("application/json"):
                data = response.json()
                if data.get("code") == -412:
                    _mark_412(tag)
                    # 触发 Playwright 兜底（如果启用）
                    self._maybe_trigger_playwright(spider)
        except Exception:
            pass

        return response

    def process_exception(self, request, exception, spider):
        pass

    def _maybe_trigger_playwright(self, spider):
        """连续 412 触发 Playwright 兜底（在 spider 上记录计数）"""
        if not hasattr(spider, "_412_count"):
            spider._412_count = 0
        spider._412_count += 1

        if spider._412_count >= _412_trigger_playwright:
            spider.logger.warning(
                f"[CookiePool] {spider._412_count} consecutive 412s — "
                f"consider enabling Playwright fallback"
            )
            # 设置标志，让 BilibiliPlaywrightFallbackMiddleware 接管
            spider._use_playwright = True
