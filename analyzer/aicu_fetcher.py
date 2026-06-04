"""
AICU 数据抓取器 — 获取 B站用户的历史评论和弹幕

通过 api.aicu.cc 公开接口获取用户历史评论、弹幕、设备标记，
为 LLM 深度分析提供额外的行为画像数据。

接口来源: 参考 https://github.com/Initsnow/bilibili-comment-clean-ing 的 Rust 实现
  - 评论接口: GET https://apibackup2.aicu.cc:88/api/v3/search/getreply?uid={}&pn={}&ps={}&mode=0&keyword=
  - 弹幕接口: GET https://apibackup2.aicu.cc:88/api/v3/search/getvideodm?uid={}&pn={}&ps={}&mode=0&keyword=
  - 用户标记: GET https://apibackup2.aicu.cc:88/api/v3/user/getusermark?uid={}

依赖: requests (标准 HTTP 库), 无特殊依赖

用法:
    fetcher = AicuFetcher(cookie="bilibili_cookie_string")
    data = fetcher.fetch_all(mid=123456)
    comments = fetcher.fetch_user_comments(mid=123456)
    danmus = fetcher.fetch_user_danmu(mid=123456)
"""

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

try:
    from curl_cffi import requests as cr_requests
    from curl_cffi.requests.errors import RequestsError as CR_Error
    _HAS_CURL_CFFI = True
except ImportError:
    cr_requests = None
    CR_Error = Exception  # 兜底，避免 NameError
    _HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

# ★ Playwright 浏览器实例（懒加载，全局复用）
_playwright_browser = None
_playwright_context = None

# ============================================================
#  Playwright 辅助函数
# ============================================================

def _detect_cloudflare(page) -> bool:
    """检测页面是否出现 CloudFlare 验证。"""
    try:
        title = page.title()
        body = page.inner_text("body")
        cf_indicators = [
            "Checking your browser",
            "Just a moment",
            "DDoS protection",
            "Cloudflare",
            "cf-browser-verify",
            "cf_challenge",
        ]
        for indicator in cf_indicators:
            if indicator.lower() in title.lower() or indicator.lower() in body.lower():
                return True
        # 检测 cf 相关元素
        if page.locator("#challenge-form, #cf-challenge, .cf-browser-verify").count():
            return True
    except Exception:
        pass
    return False


def _extract_aicu_comments_from_page(page) -> list:
    """从 AICU reply 页面 DOM 中提取评论数据（精准版 v2）。

    真实结构（来自用户提供的样本）:
        <div class="card">           ← 可能是导航/用户信息/评论
            <div class="time">2025/9/11 18:07:46 1</div>  ← 日期格式=评论
            <div class="message">评论内容</div>
            <div class="z">当前查询uid:27683704 爱来自aicu.cc</div>  ← 每个card都有
            <div class="buttons">...</div>
        </div>

    过滤规则:
        - 保留：有 .message + .time 匹配日期格式
        - 跳过：.time 含"相关链接"/"用户信息"（导航card）
        - 注意：.z 元素每个 card 都有，不能作为过滤条件
    """
    comments = []
    try:
        # 方法1: 用 JS 直接遍历 .card 容器，精准提取
        result = page.evaluate("""() => {
            const items = [];
            const cards = document.querySelectorAll('.card');
            cards.forEach(card => {
                // 必须有 .message 元素（评论内容）
                const msgEl = card.querySelector('.message');
                if (!msgEl) return;  // 无评论内容，跳过

                const message = msgEl.textContent.trim();
                if (!message || message.length < 2) return;

                // 必须有 .time 元素，且内容是日期格式
                const timeEl = card.querySelector('.time');
                if (!timeEl) return;  // 无时间，跳过

                const timeText = (timeEl.textContent || '').trim();
                // 过滤导航 card：.time 内容是"相关链接"或"用户信息"
                if (timeText.includes('相关链接') || timeText.includes('用户信息')) {
                    return;  // 导航 card，跳过
                }

                // 解析时间: "2025/9/11 18:07:46 1" (末尾数字=点赞数)
                let timestamp = 0;
                let readableTime = '';
                const dateMatch = timeText.match(/^(\\d{4})[\\/](\\d{1,2})[\\/](\\d{1,2})\\s+(\\d{1,2}):(\\d{1,2}):(\\d{1,2})/);
                if (dateMatch) {
                    try {
                        // JS Date: month 是 0-based
                        const dt = new Date(dateMatch[1], dateMatch[2]-1, dateMatch[3], dateMatch[4], dateMatch[5], dateMatch[6]);
                        timestamp = Math.floor(dt.getTime() / 1000);
                        readableTime = dateMatch[1] + '/' + dateMatch[2] + '/' + dateMatch[3] + ' ' + dateMatch[4] + ':' + dateMatch[5];
                    } catch(e) {}
                }

                // 提取 oid (视频AV号) — 从 buttons 里的链接获取
                let oid = '';
                const links = card.querySelectorAll('a[href*="bilibili.com"]');
                links.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    // 匹配 /video/av115183758349626 或 oid=115183758349626
                    const avMatch = href.match(/\\/av(\\d+)/);
                    const oidMatch = href.match(/[?&]oid=(\\d+)/);
                    if (avMatch) oid = avMatch[1];
                    else if (oidMatch) oid = oidMatch[1];
                });

                // 提取点赞数 — 从 .time 末尾的数字
                let rank = 0;
                const rankMatch = timeText.match(/\\s+(\\d+)$/);
                if (rankMatch) rank = parseInt(rankMatch[1]);

                items.push({
                    message: message.slice(0, 500),
                    oid: oid,
                    time: timestamp > 0 ? timestamp : '',
                    readable_time: readableTime,
                    type: 1,  // 评论
                    rank: rank,
                });
            });
            return items;
        }""")
        comments = result or []

        # 方法2: Playwright locator 兜底（如果 JS 没拿到）
        if not comments:
            cards = page.locator('.card')
            count = cards.count()
            for i in range(min(count, 200)):
                try:
                    card = cards.nth(i)
                    # 跳过页脚 card
                    z_text = card.locator('.z').first.inner_text() if card.locator('.z').count() > 0 else ''
                    if '当前查询uid' in z_text or '爱来自aicu.cc' in z_text:
                        continue
                    msg_el = card.locator('.message').first
                    if msg_el.count() == 0:
                        continue
                    message = msg_el.inner_text().strip()
                    if not message or len(message) < 2:
                        continue
                    time_text = card.locator('.time').first.inner_text().strip() if card.locator('.time').count() > 0 else ''
                    comments.append({
                        "message": message[:500],
                        "oid": "",
                        "time": "",
                        "readable_time": time_text[:19] if time_text else '',
                        "type": 1,
                        "rank": 0,
                    })
                except Exception:
                    continue

        logger.info(f"[AICU:Web] 当前页提取到 {len(comments)} 条评论")

    except Exception as e:
        logger.warning(f"[AICU:Web] 评论提取异常: {e}")

    return comments


# ============================================================
#  API 端点 (from Initsnow/bilibili-comment-clean-ing)
# ============================================================

# AICU 双端点自动回退：主端点 → 备用端点的顺序
AICU_PRIMARY = "https://api.aicu.cc"          # 主端点（HTTPS 443）
AICU_BACKUP = "https://apibackup2.aicu.cc:88"  # 备用端点（8888）
AICU_ACTIVE_BASE = AICU_PRIMARY                # 当前活跃端点（运行时动态切换）

AICU_REPLY_API_TEMPLATE = "{base}/api/v3/search/getreply"
AICU_DANMU_API_TEMPLATE = "{base}/api/v3/search/getvideodm"
AICU_USERMARK_API_TEMPLATE = "{base}/api/v3/user/getusermark"

# 当前活跃的实际 API 端点（函数形式，始终使用最新 AICU_ACTIVE_BASE）
def _reply_api():    return AICU_REPLY_API_TEMPLATE.format(base=AICU_ACTIVE_BASE)
def _danmu_api():    return AICU_DANMU_API_TEMPLATE.format(base=AICU_ACTIVE_BASE)
def _usermark_api(): return AICU_USERMARK_API_TEMPLATE.format(base=AICU_ACTIVE_BASE)

# 向后兼容的模块级常量（首次导入时绑定主端点）
AICU_REPLY_API = _reply_api()
AICU_DANMU_API = _danmu_api()
AICU_USERMARK_API = _usermark_api()
# Bilibili 官方 API（用于获取用户空间信息，需要登录 Cookie）
BILI_SPACE_API = "https://api.bilibili.com/x/space/acc/info"

# 北京时区 (UTC+8)
_BEIJING_TZ = timezone(timedelta(hours=8))

# ============================================================
#  数据模型
# ============================================================


@dataclass
class AicuUserData:
    """AICU 返回的用户综合数据"""
    mid: int
    profile: dict = field(default_factory=dict)
    marks: dict = field(default_factory=dict)
    comments: list = field(default_factory=list)
    danmus: list = field(default_factory=list)
    comment_count: int = 0
    danmu_count: int = 0
    stats: dict = field(default_factory=dict)
    fetch_ok: bool = False
    fetch_error: str = ""
    waf_blocked: bool = False   # API 被 WAF 拦截（HTTP 468）

    def __bool__(self):
        return self.fetch_ok

    @property
    def device_name(self) -> str:
        """设备名"""
        return self.marks.get("device_name", "")

    @property
    def history_names(self) -> list:
        """历史昵称"""
        return self.marks.get("history_names", [])

    @property
    def active_hour(self) -> Optional[int]:
        """最活跃小时"""
        return self.stats.get("active_hour")

    @property
    def avg_comment_length(self) -> float:
        """平均评论长度"""
        return self.stats.get("avg_length", 0.0)

    @property
    def profile_summary(self) -> str:
        """单行画像摘要"""
        p = self.profile
        parts = []
        if p.get("name"):
            parts.append(f"昵称: {p['name']}")
        if p.get("level"):
            parts.append(f"Lv{p['level']}")
        if p.get("fans"):
            parts.append(f"粉丝: {p['fans']}")
        if p.get("sign"):
            parts.append(f"签名: {p['sign'][:30]}")
        return " | ".join(parts) if parts else "无画像数据"


# ============================================================
#  核心类
# ============================================================


class AicuFetcher:
    """
    AICU 数据抓取器。

    使用方式:
        fetcher = AicuFetcher(cookie="your_bilibili_cookie", timeout=15)
        data = fetcher.fetch_all(mid=123456789)

    错误处理:
        所有 fetch_* 方法在出错时返回空结果，不抛异常。
        通过 AicuUserData.fetch_ok 判断成功与否。
    """

    def __init__(self, cookie: str = "", timeout: int = 15, log_callback=None):
        self.cookie = cookie
        self.timeout = timeout
        self._session = None
        self._request_count = 0
        self._last_request_time = 0.0
        self._waf_detected = False  # 当前 fetcher 是否遇到 WAF 拦截
        self._log = log_callback    # (level, msg) -> None, 用于前端实时日志

    def _get_session(self):
        """懒初始化 curl_cffi session（Chrome 131 TLS 指纹伪装）"""
        if self._session is None:
            self._session = cr_requests.Session(impersonate="chrome131")
            # curl_cffi 默认不受系统代理影响，无需强制直连
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://apibackup2.aicu.cc:88",
                "Referer": "https://apibackup2.aicu.cc:88/",
            })
            if self.cookie:
                self._session.headers["Cookie"] = self._normalize_cookie(self.cookie)
        return self._session

    def _normalize_cookie(self, cookie_str: str) -> str:
        """
        标准化 Cookie 字符串。
        支持格式:
        1. Netscape cookie 文件 (以 # Netscape 开头)
        2. 简单 key=value; 格式
        """
        cookie_str = cookie_str.strip()

        # 格式1: Netscape cookie 文件
        if cookie_str.startswith("# Netscape"):
            parts = []
            for line in cookie_str.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:
                    parts.append(f"{fields[5]}={fields[6]}")
            return "; ".join(parts)

        # 格式2: 已经是 key=value; 格式（处理 \n 转义）
        # llm_config.json 中 Cookie 可能被 JSON 转义，\n 变成字面字符
        if "\\n" in cookie_str and "\t" in cookie_str:
            # 看起来是 Netscape 格式但 \n 被转义了
            return self._normalize_cookie(cookie_str.replace("\\n", "\n").replace("\\t", "\t"))

        return cookie_str

    def _rate_limit(self):
        """简单限速：两次请求间隔 >= 3 秒（AICU 有限流）"""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)
        self._last_request_time = time.monotonic()
        self._request_count += 1

    def _get(self, url: str, params: dict) -> Optional[dict]:
        """通用 GET 请求，返回 JSON dict 或 None（curl_cffi + Chrome131 TLS 指纹）"""
        self._rate_limit()

        for attempt in range(2):  # 最多 1 次重试
            try:
                resp = self._get_session().get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    verify=False,       # 跳过 SSL 证书验证（AICU 用 Cloudflare）
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        return data
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(
                            f"[AICU] JSON 解析失败: {url}?{urlencode(params)} — {e}"
                        )
                        return None

                # 502 Bad Gateway: AICU 源站问题，重试一次
                if resp.status_code == 502:
                    if attempt == 0:
                        logger.warning(f"[AICU] 502 Bad Gateway，3秒后重试: {url}")
                        time.sleep(3.0)
                        continue
                    logger.warning(f"[AICU] 502 重试失败: {url}")
                    return None

                if resp.status_code == 404:
                    logger.warning(f"[AICU] 404: {url}?{urlencode(params)}")
                    return None

                if resp.status_code in (429, 503):
                    logger.warning(
                        f"[AICU] {resp.status_code} 限流，3秒后重试..."
                    )
                    time.sleep(3.0)
                    continue

                # HTTP 468: SafeLine WAF JS Challenge — 标记并快速返回
                if resp.status_code == 468:
                    self._waf_detected = True
                    logger.warning(f"[AICU] WAF 拦截 (HTTP 468): {url}")
                    return None

                logger.warning(
                    f"[AICU] HTTP {resp.status_code}: {url}?{urlencode(params)}"
                )
                return None

            except CR_Error as e:
                # curl_cffi 错误：超时、连接失败等
                err_msg = str(e)[:120]
                if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                    if attempt == 0:
                        logger.warning(f"[AICU] 超时重试: {url}")
                        time.sleep(3.0)
                        continue
                    logger.error(f"[AICU] 超时放弃: {url}")
                elif "connect" in err_msg.lower() or "resolve" in err_msg.lower():
                    logger.error(f"[AICU] 连接失败: {url} — {err_msg}")
                else:
                    logger.error(f"[AICU] curl 错误: {url} — {err_msg}")
                return None

            except Exception as e:
                logger.error(f"[AICU] 请求异常: {url} — {e}", exc_info=True)
                return None

        return None

    def _get_via_playwright_html(self, mid: int, max_pages: int = 50) -> Optional[list]:
        """通过 Playwright 真实浏览器访问 AICU 评论查询页，提取评论数据。

        流程（v3 - 直接URL方案，绕过首页输入框）:
          1. 直接打开 https://www.aicu.cc/reply?uid={mid}
          2. 等待页面加载完成
          3. 滚动触发懒加载
          4. 提取当前页评论
          5. 点击翻页按钮获取更多
          6. 从中提取评论内容

        Args:
            mid: B站用户 UID
            max_pages: 最大翻页次数（默认50，测试时可设为1）

        Returns:
            [{time, message, readable_time, oid, type, rank}, ...] 或 None
        """
        global _playwright_browser, _playwright_context

        # v2.29: 线程安全检查 — Playwright 不能在后台线程中使用
        import threading
        if threading.current_thread() != threading.main_thread():
            logger.warning(f"[AICU:Web] 后台线程中禁用 Playwright (mid={mid})")
            if self._log:
                self._log("warn", f"  后台线程中跳过 Playwright 浏览器抓取 (mid={mid})")
            return None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[AICU:Web] Playwright 未安装")
            return None

        logger.info(f"[AICU:Web] 启动浏览器抓取 mid={mid}...")
        if self._log:
            self._log("info", f"  启动 Playwright 浏览器查询 AICU...")

        pw = None
        page = None
        need_user_cf = False

        try:
            if _playwright_browser is None:
                pw = sync_playwright().start()
                _playwright_browser = pw.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                _playwright_context = _playwright_browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 800},
                )

            page = _playwright_context.new_page()

            # ---- Step 1: 直接打开评论查询 URL ----
            target_url = f"https://www.aicu.cc/reply?uid={mid}"
            logger.info(f"[AICU:Web] 访问 {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # 检测 CloudFlare
            if _detect_cloudflare(page):
                need_user_cf = True
                logger.warning("[AICU:Web] 检测到 CloudFlare 验证，等待60秒...")
                if self._log:
                    self._log("warn", "  ⚠️ CloudFlare 验证 — 请手动完成浏览器中的验证，60秒超时")
                try:
                    page.wait_for_function(
                        "document.body.innerText.includes('评论') || document.body.innerText.includes('回复')",
                        timeout=60000,
                    )
                except Exception:
                    logger.error("[AICU:Web] CloudFlare 验证超时")
                    return None

            # 等待评论数据加载
            try:
                page.wait_for_function(
                    """() => {
                        const text = document.body.innerText || '';
                    return /评论数\\s*\\d+/.test(text) || /\\d{4}\\/\\d{1,2}\\/\\d{1,2}\\s+\\d{1,2}:\\d{1,2}/.test(text);
                    }""",
                    timeout=20000,
                )
                logger.info("[AICU:Web] 评论数据已加载")
            except Exception:
                logger.warning("[AICU:Web] 等待评论数据超时，继续尝试提取...")

            page.wait_for_timeout(2000)

            # ---- Step 2: 滚动加载 + 翻页获取全部评论 ----
            all_comments = []
            seen_ids = set()

            # 先滚动到底部触发懒加载
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)

            # 翻页逻辑
            current_page = 1
            pages_limit = max_pages or 50

            while current_page <= pages_limit:
                # 提取当前页评论
                comments = _extract_aicu_comments_from_page(page)
                new_count = 0
                for c in comments:
                    oid = c.get("oid", "") or c.get("message", "")[:50]
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        all_comments.append(c)
                        new_count += 1

                logger.info(f"[AICU:Web] 第{current_page}页提取 {len(comments)} 条 (新增{new_count}, 累计{len(all_comments)})")

                # 如果这一页没有新评论，说明已到末页
                if new_count == 0 and current_page > 1:
                    logger.info("[AICU:Web] 当前页无新评论，停止翻页")
                    break

                # ---- 翻页：多种策略 ----
                clicked = False

                # 策略1: 找"下一页"箭头 / 文字按钮
                for next_sel in [
                    "a:has-text('>')",
                    "a:has-text('下一页')",
                    "a.pagination-next",
                    "button:has-text('下一页')",
                    ".pagination a:last-child",
                ]:
                    btn = page.locator(next_sel)
                    if btn.count() > 0 and btn.first.is_visible():
                        try:
                            btn.first.click()
                            clicked = True
                            current_page += 1
                            break
                        except Exception:
                            continue

                # 策略2: 找数字按钮（比当前页大）
                if not clicked:
                    all_btns = page.locator("#pagination a, .pagination a, nav a")
                    btn_count = all_btns.count()
                    for i in range(btn_count):
                        try:
                            btn = all_btns.nth(i)
                            text = btn.inner_text().strip()
                            page_num = int(text)
                            if page_num == current_page + 1:
                                btn.click()
                                current_page = page_num
                                clicked = True
                                break
                        except (ValueError, Exception):
                            continue

                if not clicked:
                    logger.info(f"[AICU:Web] 无下一页按钮，停止翻页 (共{current_page}页)")
                    break

                # 等待页面加载
                page.wait_for_timeout(2500)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)

            logger.info(f"[AICU:Web] 完成: {len(all_comments)} 条评论 (mid={mid})")
            if self._log:
                self._log("success", f"  Playwright 抓取完成: {len(all_comments)} 条评论")
            return all_comments

        except Exception as e:
            logger.error(f"[AICU:Web] 异常: {e}", exc_info=True)
            if self._log:
                self._log("error", f"  Playwright 抓取异常: {str(e)[:80]}")
            if need_user_cf:
                logger.warning("[AICU:Web] 建议: 请自行在浏览器打开 aicu.cc 手动查询")
            return None

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    def _get_via_playwright(self, url: str, params: dict) -> Optional[dict]:
        """通过 Playwright 真实浏览器请求 AICU API，绕过 CloudFlare WAF。"""
        global _playwright_browser, _playwright_context

        # v2.29: 线程安全检查 — Playwright 不能在后台线程中使用
        import threading
        if threading.current_thread() != threading.main_thread():
            logger.warning(f"[AICU:Playwright] 后台线程中禁用 Playwright: {url[:60]}")
            return None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[AICU] Playwright 未安装，跳过浏览器兜底")
            return None

        full_url = f"{url}?{urlencode(params)}"
        logger.info(f"[AICU:Playwright] 请求 {full_url[:100]}...")

        try:
            if _playwright_browser is None:
                pw = sync_playwright().start()
                _playwright_browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                _playwright_context = _playwright_browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                )

            page = _playwright_context.new_page()
            try:
                resp = page.goto(full_url, wait_until="domcontentloaded", timeout=20000)
                if resp and resp.status == 200:
                    body = page.content()
                    # 提取 JSON（可能在 <pre> 标签或纯文本中）
                    import re
                    match = re.search(r'<pre[^>]*>(.*?)</pre>', body, re.DOTALL)
                    if match:
                        raw_text = match.group(1)
                    else:
                        raw_text = page.inner_text("body")

                    data = json.loads(raw_text)
                    return data
                else:
                    status = resp.status if resp else "N/A"
                    logger.warning(f"[AICU:Playwright] HTTP {status}: {full_url[:80]}")
                    return None
            finally:
                page.close()

        except Exception as e:
            logger.error(f"[AICU:Playwright] 异常: {e}")
            return None

    # ============================================================
    #  单项抓取
    # ============================================================

    def fetch_user_profile(self, mid: int) -> dict:
        """
        获取用户空间信息（Bilibili 官方 API，需要登录 Cookie）。

        Returns:
            {name, avatar, sign, fans, following, level, vip_label}
        """
        try:
            raw = self._get(BILI_SPACE_API, {"mid": mid})
            if not raw or raw.get("code") != 0:
                logger.debug(f"[AICU] 空间信息获取失败: mid={mid}")
                return {}

            data = raw.get("data", {})
            return {
                "name": data.get("name", ""),
                "avatar": data.get("face", ""),
                "sign": data.get("sign", ""),
                "fans": data.get("follower", 0),
                "following": data.get("follow", 0),
                "level": data.get("level", 0),
                "vip_label": data.get("vip", {}).get("label", {}).get("text", ""),
            }
        except Exception as e:
            logger.error(f"[AICU] fetch_user_profile({mid}) 异常: {e}")
            return {}

    def fetch_user_marks(self, mid: int) -> dict:
        """
        获取用户设备标记 + 历史昵称（AICU API）。

        Returns:
            {device_name, history_names: [str]}
        """
        try:
            if self._log:
                self._log("info", f"  AICU API: 获取设备标记 mid={mid}")
            raw = self._get(AICU_USERMARK_API, {"uid": mid})
            if not raw or raw.get("code") != 0:
                if self._log:
                    self._log("warn", f"  设备标记获取失败 mid={mid}, code={raw.get('code') if raw else 'N/A'}")
                logger.debug(f"[AICU] 设备标记获取失败: mid={mid}")
                return {}

            data = raw.get("data", {})
            device = ""
            devices = data.get("device", [])
            if devices and isinstance(devices, list):
                first = devices[0]
                if isinstance(first, dict):
                    device = first.get("name") or first.get("type", "")
                else:
                    device = str(first)

            hnames = data.get("hname", [])
            if not isinstance(hnames, list):
                hnames = []

            if self._log:
                extra = []
                if device: extra.append(f"设备:{device}")
                if hnames: extra.append(f"曾用名:{len(hnames)}个")
                self._log("success", f"  设备标记完成 mid={mid}: {', '.join(extra) if extra else '无数据'}")

            return {
                "device_name": device,
                "history_names": hnames,
            }
        except Exception as e:
            logger.error(f"[AICU] fetch_user_marks({mid}) 异常: {e}")
            if self._log:
                self._log("error", f"  设备标记异常 mid={mid}: {e}")
            return {}

    def fetch_user_comments(self, mid: int, known_count: int = None) -> dict:
        """
        通过 Playwright 网页抓取获取用户历史评论（放弃 AICU API）。

        直接访问 aicu.cc 网页，模拟点击翻页，提取评论内容。
        known_count 参数仅用于日志参考（AICU 探测结果），不影响抓取逻辑。

        Args:
            mid: B站用户 UID
            known_count: 已知评论总数（仅用于日志，可选）

        Returns:
            {
                comments: [{time, message, rank, readable_time, oid, type}],
                count: int,
                stats: {active_hour, avg_length, hour_dist, source}
            }
        """
        if known_count:
            logger.info(f"[AICU] 开始网页抓取评论: mid={mid} (已知{known_count}条)")
            if self._log:
                self._log("info", f"  放弃 API，启动网页抓取: mid={mid}, 已知{known_count}条评论")
        else:
            logger.info(f"[AICU] 开始网页抓取评论: mid={mid}")
            if self._log:
                self._log("info", f"  启动网页抓取: mid={mid}")

        # 直接调用 Playwright 网页抓取（默认最多50页）
        html_comments = self._get_via_playwright_html(mid, max_pages=50)

        if not html_comments:
            logger.warning(f"[AICU] 网页抓取评论失败: mid={mid}")
            if self._log:
                self._log("warn", f"  网页抓取失败: mid={mid}")
            return {"comments": [], "count": 0, "stats": {}}

        # 统计
        parsed = html_comments
        all_lengths = []
        hour_counter = Counter()

        for c in parsed:
            msg = c.get("message", "")
            if msg:
                all_lengths.append(len(msg))
            # 尝试从 time 字段提取小时
            ts = c.get("time", "")
            if ts and isinstance(ts, (int, float)) and ts > 0:
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=_BEIJING_TZ)
                    hour_counter[dt.hour] += 1
                except (OSError, ValueError, OverflowError):
                    pass

        active_hour = hour_counter.most_common(1)[0][0] if hour_counter else None
        avg_length = round(sum(all_lengths) / len(all_lengths), 1) if all_lengths else 0.0
        hour_dist = dict(hour_counter.most_common(5))

        logger.info(f"[AICU] 网页抓取评论完成: mid={mid}, 实际={len(parsed)}条")
        if self._log:
            self._log("success", f"  网页抓取完成 mid={mid}: {len(parsed)}条, 活跃时段={active_hour}点, 均长={avg_length}字")
        return {
            "comments": parsed,
            "count": len(parsed),
            "stats": {
                "active_hour": active_hour,
                "avg_length": avg_length,
                "hour_dist": hour_dist,
                "source": "playwright_web",
            },
        }

    def fetch_user_danmu(self, mid: int) -> dict:
        """
        分页获取用户历史弹幕（AICU API）。

        先请求 ps=0 获取 all_count，然后分页获取全部弹幕（每页最多 500 条）。

        Args:
            mid: B站用户 UID

        Returns:
            {
                danmus: [{id, content, oid}],
                count: int,
                stats: {}
            }
        """
        # 第一步：获取弹幕总数
        try:
            count_params = {
                "uid": mid,
                "pn": 1,
                "ps": 0,  # ps=0 只返回总数
                "mode": 0,
                "keyword": "",
            }
            count_raw = self._get(AICU_DANMU_API, count_params)
            if not count_raw or count_raw.get("code") != 0:
                logger.debug(f"[AICU] 弹幕总数获取失败: mid={mid}, code={count_raw.get('code') if count_raw else 'N/A'}")
                return {"danmus": [], "count": 0, "stats": {}}

            all_count = count_raw.get("data", {}).get("cursor", {}).get("all_count", 0)
            if all_count == 0:
                logger.info(f"[AICU] 用户 {mid} 无历史弹幕")
                return {"danmus": [], "count": 0, "stats": {}}

            logger.info(f"[AICU] 用户 {mid} 共有 {all_count} 条历史弹幕，开始分页获取...")
            if self._log:
                self._log("info", f"  AICU API: 分页获取弹幕 mid={mid}, 共{all_count}条")

        except Exception as e:
            logger.error(f"[AICU] fetch_user_danmu({mid}) 获取总数异常: {e}")
            return {"danmus": [], "count": 0, "stats": {}}

        # 第二步：分页获取弹幕
        parsed = []
        page = 1
        page_size = 500

        while len(parsed) < all_count:
            params = {
                "uid": mid,
                "pn": page,
                "ps": page_size,
                "mode": 0,
                "keyword": "",
            }

            try:
                raw = self._get(AICU_DANMU_API, params)
                if not raw or raw.get("code") != 0:
                    logger.warning(f"[AICU] 弹幕分页获取失败: mid={mid}, page={page}, code={raw.get('code') if raw else 'N/A'}")
                    break

                data = raw.get("data", {})
                danmu_list = data.get("videodmlist", [])

                if not danmu_list:
                    if not data.get("cursor", {}).get("is_end", True):
                        logger.warning(f"[AICU] 弹幕页 {page} 为空但 is_end=false，继续...")
                        page += 1
                        continue
                    break

                for item in danmu_list:
                    if not isinstance(item, dict):
                        continue

                    parsed.append({
                        "id": item.get("id", 0),
                        "content": item.get("content", ""),
                        "oid": item.get("oid", 0),
                    })

                # 检查是否结束
                if data.get("cursor", {}).get("is_end", False):
                    logger.info(f"[AICU] 弹幕分页获取完成 (cursor.is_end=True): 获取 {len(parsed)}/{all_count}")
                    break

                page += 1

                if page > (all_count // page_size) + 10:
                    logger.warning(f"[AICU] 弹幕分页页码异常，强制退出: page={page}, fetched={len(parsed)}, total={all_count}")
                    break

            except Exception as e:
                logger.error(f"[AICU] 弹幕分页异常: mid={mid}, page={page}, error={e}")
                break

        logger.info(f"[AICU] 弹幕获取完成: mid={mid}, 实际={len(parsed)}, 总数={all_count}")
        if self._log:
            self._log("success", f"  弹幕获取完成 mid={mid}: {len(parsed)}条")
        return {
            "danmus": parsed,
            "count": len(parsed),
            "stats": {},
        }

    def _get_fast(self, url: str, params: dict) -> Optional[dict]:
        """探测请求：12s超时 + 1次重试，用于检查端点是否可用"""
        for attempt in range(2):
            try:
                resp = self._get_session().get(url, params=params, timeout=12, verify=False)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (502, 503, 429) and attempt == 0:
                    time.sleep(1.5)
                    continue
                return None
            except Exception:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
        return None

    # ============================================================
    #  聚合抓取
    # ============================================================

    def fetch_all(self, mid: int) -> AicuUserData:
        """
        聚合抓取用户全部 AICU 数据。

        策略：优先抓 marks（getusermark 稳定可用），
        getreply/getvideodm 快速探测（5s 超时），不可用则跳过。
        """
        result = AicuUserData(mid=mid)

        try:
            # 1. marks 优先（getusermark 最稳定，设备+历史昵称价值高）
            marks = self.fetch_user_marks(mid)
            result.marks = marks

            # 2. profile（B站官方API，依赖Cookie）
            profile = self.fetch_user_profile(mid)
            result.profile = profile

            # 3. 快速探测 getreply/getvideodm（5s 超时，不重试）
            count_params = {"uid": mid, "pn": 1, "ps": 0, "mode": 0, "keyword": ""}

            if self._log:
                self._log("info", f"  AICU API: 探测评论/弹幕总数 mid={mid}")

            reply_fast = self._get_fast(AICU_REPLY_API, count_params)
            comment_total = 0
            if reply_fast and reply_fast.get("code") == 0:
                comment_total = reply_fast.get("data", {}).get("cursor", {}).get("all_count", 0)

            danmu_fast = self._get_fast(AICU_DANMU_API, count_params)
            danmu_total = 0
            if danmu_fast and danmu_fast.get("code") == 0:
                danmu_total = danmu_fast.get("data", {}).get("cursor", {}).get("all_count", 0)

            if self._log:
                self._log("info", f"  探测结果 mid={mid}: 评论={comment_total}条, 弹幕={danmu_total}条")

            logger.info(f"[AICU] mid={mid} 探测: marks={'OK' if marks else 'EMPTY'}, "
                       f"comments={comment_total}, danmus={danmu_total}")

            # 4. 分页抓取（仅当探测成功 + 有数据时）
            comment_data = {"comments": [], "count": 0, "stats": {}}
            danmu_data = {"danmus": [], "count": 0, "stats": {}}

            if comment_total > 0:
                if self._log:
                    self._log("info", f"  AICU API: 开始分页获取评论 mid={mid}, 共{comment_total}条")
                comment_data = self.fetch_user_comments(mid, known_count=comment_total)
                if self._log:
                    self._log("info", f"  评论抓取结果 mid={mid}: 获得{comment_data.get('count', 0)}条")
            if danmu_total > 0:
                danmu_data = self.fetch_user_danmu(mid)

            result.comments = comment_data.get("comments", [])
            result.danmus = danmu_data.get("danmus", [])
            result.comment_count = comment_data.get("count", 0)
            result.danmu_count = danmu_data.get("count", 0)
            result.stats = comment_data.get("stats", {})
            result.waf_blocked = self._waf_detected
            # marks 成功即视为有效抓取（设备+历史昵称对深度分析最有价值）
            result.fetch_ok = bool(marks) and not self._waf_detected

            logger.info(
                f"[AICU] mid={mid} 抓取完成: "
                f"profile={'OK' if profile else 'EMPTY'}, "
                f"marks={'OK' if marks else 'EMPTY'}, "
                f"comments={result.comment_count}/{comment_total}, "
                f"danmus={result.danmu_count}/{danmu_total}"
            )

        except Exception as e:
            result.fetch_error = str(e)
            logger.error(f"[AICU] fetch_all({mid}) 异常: {e}", exc_info=True)

        return result

    def __del__(self):
        """清理 session"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
