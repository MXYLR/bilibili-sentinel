"""
B站空间页 Playwright 爬取器 v3 — 2026年6月新版 DOM 适配

v3 变更:
  - B站已移除 __INITIAL_STATE__, 纯 DOM 提取
  - CSS 类名全面适配新版 (2026-06 B站 UI)
  - Profile 页可以直接提取（服务端渲染, 无需登录）
  - 视频/动态页会被 geetest 拦截, 改用 SPA 导航或 API 方式

实测验证: UID 27683704, 无需登录即可获取:
  name=MXYLR, follower=266, following=301, video_count=258, level=5
  sign=小号幻痛..., birthday=03-02, face=//i1.hdslb.com/...

目标页面:
  - https://space.bilibili.com/[UID]               → 用户画像
  - https://space.bilibili.com/[UID]/upload/video   → 投稿视频 (SPA导航)
  - https://space.bilibili.com/[UID]/dynamic        → 动态列表 (SPA导航)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_thread_local = threading.local()

_DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _get_thread_browser():
    if not hasattr(_thread_local, "browser"):
        _thread_local.browser = None
        _thread_local.context = None
    return _thread_local.browser, _thread_local.context


def _set_thread_browser(browser, context):
    _thread_local.browser = browser
    _thread_local.context = context


def _ensure_browser(headless: bool = True, cookie_str: str = ""):
    browser, context = _get_thread_browser()
    if browser is not None:
        # ★ v2.37 BUGFIX: 每次创建新 context，防止 "last page closed → context auto-destroyed"
        # Chromium 会在关闭 context 中最后一个 page 时自动销毁 context，
        # 若不重建，下一个 scrape_*() 方法会因 context 已销毁而崩溃。
        ctx = browser.new_context(
            viewport=_DEFAULT_VIEWPORT,
            user_agent=_DEFAULT_UA,
            locale="zh-CN",
        )
        if cookie_str:
            _inject_cookies(ctx, cookie_str)
        _set_thread_browser(browser, ctx)
        page = ctx.new_page()
        if not headless:
            _set_window_state(page, minimized=True)
        return browser, ctx, page

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("[SpaceScraper] playwright not installed")
        return None, None, None

    pw = sync_playwright().start()
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
    ]
    browser_obj = pw.chromium.launch(headless=headless, args=launch_args)
    ctx = browser_obj.new_context(
        viewport=_DEFAULT_VIEWPORT,
        user_agent=_DEFAULT_UA,
        locale="zh-CN",
    )
    if cookie_str:
        _inject_cookies(ctx, cookie_str)
    _set_thread_browser(browser_obj, ctx)
    page = ctx.new_page()

    # ★ 背景模式：启动后立即最小化窗口，避免干扰用户前台操作
    if not headless:
        _set_window_state(page, minimized=True)

    return browser_obj, ctx, page


def _inject_cookies(context, cookie_str: str):
    if not cookie_str:
        return
    cookies = []
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        cookies.append({
            "name": name.strip(), "value": value.strip(),
            "domain": ".bilibili.com", "path": "/",
        })
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception as e:
            logger.warning(f"[SpaceScraper] cookie inject failed: {e}")


# ============================================================
#  CDP 窗口管理（后台运行 + 必要时弹到前台）
# ============================================================

_window_minimized = False  # 模块级标记，跟踪窗口是否已最小化


def _set_window_state(page, minimized: bool = True):
    """通过 CDP 最小化/恢复 Chromium 浏览器窗口，避免干扰用户前台操作。"""
    global _window_minimized
    try:
        cdp = page.context.new_cdp_session(page)
        result = cdp.send("Browser.getWindowForTarget")
        window_id = result.get("windowId")
        if not window_id:
            return
        state = "minimized" if minimized else "normal"
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": state}
        })
        _window_minimized = minimized
        logger.info(f"[SpaceScraper] Window {'minimized' if minimized else 'restored'}")
    except Exception as e:
        logger.debug(f"[SpaceScraper] CDP window state failed (non-critical): {e}")


def _focus_window(page):
    """将浏览器窗口恢复到前台（仅在检测到 CAPTCHA/登录墙时调用）。"""
    global _window_minimized
    if not _window_minimized:
        return  # 窗口已在前台，无需操作
    _set_window_state(page, minimized=False)


def _close_geetest(page):
    """尝试关闭 geetest 验证码弹窗（不影响已加载的 DOM 数据）"""
    try:
        close_btn = page.locator(".geetest_close, .geetest_panel_close, [class*=\"close\"]").first
        if close_btn.count() > 0:
            close_btn.click()
            page.wait_for_timeout(500)
    except Exception:
        pass


def _detect_blockers(page) -> dict:
    """
    检测 B站 是否弹出登录墙/CAPTCHA.
    
    返回: {"login_wall": bool, "captcha": bool, "reason": str}
    """
    result = {"login_wall": False, "captcha": False, "reason": ""}
    
    try:
        url = page.url
        # 1. URL 跳转到 passport（登录页）
        if "passport.bilibili.com" in url or "login" in url:
            result["login_wall"] = True
            result["reason"] = f"URL 跳转到登录页: {url}"
            return result
        
        # 2. DOM 检测登录弹窗
        login_selectors = [
            ".login-panel", ".login-panel-popover",
            ".bili-login", "[class*='login-modal']",
            ".bili-mini-mask",  # 登录遮罩
        ]
        for sel in login_selectors:
            if page.locator(sel).count() > 0:
                result["login_wall"] = True
                result["reason"] = f"检测到登录弹窗: {sel}"
                return result
        
        # 3. DOM 检测 CAPTCHA
        captcha_selectors = [
            ".geetest_panel", ".geetest_wind", ".captcha",
            "[class*='captcha']", "[id*='captcha']",
            ".bilibili-captcha", ".safety-verify",
        ]
        for sel in captcha_selectors:
            if page.locator(sel).count() > 0:
                result["captcha"] = True
                result["reason"] = f"检测到验证码: {sel}"
                return result
        
        # 4. 页面文字检测
        page_text = page.evaluate("() => document.body.innerText")
        if "请登录" in page_text and "space.bilibili.com" in page.url:
            result["login_wall"] = True
            result["reason"] = "页面文字检测到 '请登录'"
            return result
        if "验证码" in page_text and ("拖动" in page_text or "拼图" in page_text or "slide" in page_text.lower()):
            result["captcha"] = True
            result["reason"] = "页面文字检测到验证码提示"
            return result
        
    except Exception as e:
        logger.warning(f"[_detect_blockers] 检测异常: {e}")
    
    return result


def _wait_for_manual_bypass(page, headless: bool = False, max_wait_sec: int = 300):
    """
    当检测到登录墙/CAPTCHA 时，提示用户手动操作，并等待解除.
    
    - headless=False: 弹窗可见，提示用户手动完成
    - headless=True:  不可见，只能跳过（打印警告）
    - max_wait_sec:   最长等待时间（默认 5 分钟）
    
    返回: True=已解除, False=超时或未解除
    """
    blockers = _detect_blockers(page)
    
    if not blockers["login_wall"] and not blockers["captcha"]:
        return True  # 没有障碍，继续
    
    reason = blockers["reason"]
    
    if headless:
        # 无头模式：看不到浏览器，只能跳过
        logger.warning(f"[SpaceScraper] ⚠️ 检测到阻碍但 headless=True，无法手动操作: {reason}")
        return False
    
    # ★ 可见模式：检测到阻拦时，将浏览器窗口恢复到前台，方便用户操作
    _focus_window(page)
    
    # 提示用户手动操作
    import sys
    wait_min = max_wait_sec // 60
    
    # 在页面上显示提示（覆盖层）
    try:
        page.evaluate("""(reason) => {
            // 创建全屏提示覆盖层
            var overlay = document.createElement('div');
            overlay.id = '__pw_manual_hint__';
            overlay.style.cssText = [
                'position:fixed', 'top:0', 'left:0', 'width:100%', 'height:100%',
                'background:rgba(0,0,0,0.85)', 'z-index:2147483647',
                'display:flex', 'flex-direction:column', 'align-items:center', 'justify-content:center',
                'color:#fff', 'font-size:20px', 'font-family:Microsoft YaHei,sans-serif',
                'pointer-events:none', 'user-select:none',
            ].join(';');
            
            var title = document.createElement('div');
            title.style.cssText = 'font-size:28px;margin-bottom:20px;color:#00e5ff';
            title.textContent = '⏸️ Playwright 需要您手动操作';
            
            var desc = document.createElement('div');
            desc.style.cssText = 'font-size:16px;margin-bottom:30px;color:#ffcc02;max-width:600px;text-align:center;line-height:1.8';
            desc.innerHTML = '检测到：' + reason + '<br>请手动完成登录 / 验证码，完成后关闭此提示。';
            
            var status = document.createElement('div');
            status.id = '__pw_manual_status__';
            status.style.cssText = 'font-size:14px;color:#aaa';
            status.textContent = '等待中...（最多 ' + """ + """ + """ + str(wait_min) + """ + """ + """ + ' 分钟）';
            
            overlay.appendChild(title);
            overlay.appendChild(desc);
            overlay.appendChild(status);
            
            // 移除旧提示
            var old = document.getElementById('__pw_manual_hint__');
            if (old) old.remove();
            document.body.appendChild(overlay);
        }""")
    except Exception:
        pass
    
    # 在终端打印提示
    hint = (
        "\n" + "=" * 60 + "\n"
        "⏸️  Playwright 爬虫暂停 — 需要您手动操作\n"
        "=" * 60 + "\n"
        f"原因: {reason}\n"
        f"\n"
        "请在弹出的浏览器窗口中手动完成以下操作之一：\n"
        "  1. 登录 B站账号（如果弹出登录墙）\n"
        "  2. 完成验证码拼图/滑动（如果弹出 CAPTCHA）\n"
        "\n"
        f"完成后，爬虫会在 {wait_min} 分钟内自动继续。\n"
        "（或者手动关闭浏览器窗口以终止）\n"
        "=" * 60 + "\n"
    )
    # 同时输出到 stderr（确保用户看到）和 logger
    print(hint, file=sys.stderr)
    logger.warning(f"[SpaceScraper] ⏸️  暂停，等待用户手动操作: {reason}")
    
    # 轮询等待障碍解除
    import time
    start_time = time.time()
    check_interval = 3  # 每 3 秒检查一次
    
    while time.time() - start_time < max_wait_sec:
        # 检查障碍是否解除
        blockers_now = _detect_blockers(page)
        if not blockers_now["login_wall"] and not blockers_now["captcha"]:
            # 额外检查：页面是否已正常加载（URL 不再是登录页）
            current_url = page.url
            if "passport.bilibili.com" not in current_url and "login" not in current_url:
                elapsed = int(time.time() - start_time)
                logger.info(f"[SpaceScraper] ✅ 手动操作完成，继续执行（等待了 {elapsed} 秒）")
                
                # 移除提示覆盖层
                try:
                    page.evaluate("""() => {
                        var el = document.getElementById('__pw_manual_hint__');
                        if (el) el.remove();
                    }""")
                except Exception:
                    pass
                
                # ★ 操作完成后最小化窗口，回到后台
                _set_window_state(page, minimized=True)
                
                return True
        
        # 更新覆盖层状态
        try:
            elapsed = int(time.time() - start_time)
            remaining = max_wait_sec - elapsed
            page.evaluate(
                "((elapsed, remaining) => {" +
                "  var s = document.getElementById('__pw_manual_status__');" +
                "  if (s) s.textContent = '已等待 ' + elapsed + ' 秒，最多再等 ' + remaining + ' 秒...';" +
                "})(arguments[0], arguments[1])",
                elapsed, remaining
            )
        except Exception:
            pass
        
        page.wait_for_timeout(check_interval * 1000)
    
    # 超时
    logger.error(f"[SpaceScraper] ❌ 等待手动操作超时（{max_wait_sec} 秒），继续执行（可能失败）")
    try:
        page.evaluate("""() => {
            var el = document.getElementById('__pw_manual_hint__');
            if (el) el.remove();
        }""")
    except Exception:
        pass
    # ★ 超时后也最小化窗口
    _set_window_state(page, minimized=True)
    return False


# ================================================================
#  SpacePageScraper
# ================================================================

class SpacePageScraper:
    """B站空间页 Playwright 爬取器 v3."""

    def __init__(self, cookie: str = "", headless: bool = True, timeout: int = 30000):
        self.cookie = cookie
        self.headless = headless
        self.timeout = timeout

    def _get_page(self):
        browser, context, page = _ensure_browser(
            headless=self.headless, cookie_str=self.cookie,
        )
        if page is None:
            raise RuntimeError("Playwright browser launch failed")
        return page

    # ============================================================
    #  用户画像
    # ============================================================

    def scrape_user_profile(self, uid: int) -> Dict[str, Any]:
        """
        爬取用户空间首页 → 用户画像.

        基于 2026-06 B站新版 DOM (已验证 UID=27683704):
          - .nickname                          → 昵称
          - .header-sign .pure-text[title]     → 签名
          - [class*=\"user_level_\"]             → 等级图标
          - .gender i[class*=\"male\"]           → 性别
          - .nav-statistics__item              → 关注/粉丝
          - .nav-tab__item                     → 投稿数
          - .b-avatar__layer__res img          → 头像
          - .info-item (uid_line/cake_line)     → UID/生日
        """
        url = f"https://space.bilibili.com/{uid}"
        logger.info(f"[SpaceScraper] profile uid={uid}")
        page = self._get_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(4000)
            _close_geetest(page)
            page.wait_for_timeout(500)
            
            # ★ 检测登录墙/CAPTCHA，提示用户手动操作
            _wait_for_manual_bypass(page, headless=self.headless, max_wait_sec=300)

            profile = page.evaluate(r"""() => {
                var data = {};
                var el;

                // nickname
                el = document.querySelector('.nickname');
                data.name = el ? el.textContent.trim() : '';

                // sign — 优先从 <meta name="description"> 提取（最可靠，B站 SEO 标签）
                // 格式: "哔哩哔哩XXX的个人空间，提供XXX分享的...。{签名}"
                // 签名位于最后一个中文句号之后
                data.sign = '';
                var metaDesc = document.querySelector('meta[name="description"]');
                if (metaDesc) {
                    var desc = metaDesc.getAttribute('content') || '';
                    // 格式：前半部分是固定模板（以"。"结尾），签名在最后一个"。"后面
                    var lastDot = desc.lastIndexOf('\u3002');
                    if (lastDot >= 0 && lastDot < desc.length - 1) {
                        data.sign = desc.substring(lastDot + 1).trim();
                    }
                }
                // 兜底：从 DOM 选择器提取
                if (!data.sign) {
                    el = document.querySelector(
                        '.sign.header-sign .pure-text, ' +
                        '.header-sign .pure-text, ' +
                        '.user-desc, ' +
                        '[class*="sign"] .pure-text'
                    );
                    data.sign = el ? (el.getAttribute('title') || el.textContent.trim()) : '';
                }

                // level
                el = document.querySelector('.level-icon');
                data.level = 0;
                if (el) {
                    var m = el.className.match(/user_level_(\d+)/i);
                    if (m) data.level = parseInt(m[1]);
                }

                // gender
                data.sex = '';
                el = document.querySelector('.gender');
                if (el) {
                    var icon = el.querySelector('i, svg');
                    if (icon) {
                        var icls = icon.className || icon.getAttribute('class') || '';
                        if (icls.indexOf('male') >= 0) data.sex = '\u7537';
                        else if (icls.indexOf('female') >= 0) data.sex = '\u5973';
                        else data.sex = '\u4fdd\u5bc6';
                    }
                }

                // avatar
                el = document.querySelector('.b-avatar__layer__res img');
                data.face = el ? (el.getAttribute('src') || '') : '';

                // follower / following
                data.follower = 0; data.following = 0;
                document.querySelectorAll('.nav-statistics__item').forEach(function(item) {
                    var lbl = item.querySelector('.nav-statistics__item-text');
                    var num = item.querySelector('.nav-statistics__item-num');
                    if (!lbl || !num) return;
                    var txt = lbl.textContent.trim();
                    var val = parseInt(num.textContent.replace(/[^0-9]/g, '')) || 0;
                    if (txt.indexOf('\u7c89') >= 0) data.follower = val;
                    else if (txt.indexOf('\u5173') >= 0) data.following = val;
                });

                // video_count
                data.video_count = 0;
                document.querySelectorAll('.nav-tab__item').forEach(function(item) {
                    var lbl = item.querySelector('.nav-tab__item-text');
                    var num = item.querySelector('.nav-tab__item-num');
                    if (!lbl || !num) return;
                    var txt = lbl.textContent.trim();
                    var val = parseInt(num.textContent.replace(/[^0-9]/g, '')) || 0;
                    if (txt === '\u6295\u7a3f') data.video_count = val;
                });

                // birthday
                data.birthday = '';
                document.querySelectorAll('.info-item').forEach(function(item) {
                    var icon = item.querySelector('i');
                    var div = item.querySelector('.vui_ellipsis, [class*="ellipsis"]');
                    if (!icon || !div) return;
                    var icls = icon.className || '';
                    if (icls.indexOf('cake') >= 0) data.birthday = div.textContent.trim();
                });

                return data;
            }""")

            profile["mid"] = uid
            if profile.get("name"):
                logger.info(
                    f"[SpaceScraper] ✅ {profile['name']} Lv{profile.get('level',0)} "
                    f"fan={profile.get('follower',0)} v={profile.get('video_count',0)}"
                )
                return profile

            logger.warning(f"[SpaceScraper] profile for uid={uid}: name not found in DOM")
            return {}

        except Exception as e:
            logger.error(f"[SpaceScraper] profile({uid}) error: {e}")
            return {}
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ============================================================
    #  投稿视频
    # ============================================================

    def scrape_user_videos(self, uid: int, max_pages: int = 5) -> List[Dict[str, Any]]:
        """
        爬取用户投稿视频列表.

        策略: 先加载画像页 → 点击 "投稿" tab (SPA 客户端导航) → 翻页.

        返回: [{"bvid", "aid", "title", "cover", "play", "comment", "created"}, ...]
        """
        url = f"https://space.bilibili.com/{uid}"
        logger.info(f"[SpaceScraper] videos uid={uid}")
        page = self._get_page()
        videos = []

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(4000)
            _close_geetest(page)
            
            # ★ 检测登录墙/CAPTCHA，提示用户手动操作
            if not _wait_for_manual_bypass(page, headless=self.headless, max_wait_sec=300):
                logger.warning(f"[SpaceScraper] uid={uid} 手动操作等待超时/失败，跳过视频爬取")
                return videos

            # 点击 "投稿" tab（SPA 导航，重试 3 次）
            tab_clicked = False
            for attempt in range(3):
                try:
                    # 先确保 nav-tab 已渲染
                    page.wait_for_selector('.nav-tab__item', timeout=10000)
                    
                    upload_tab = page.locator('.nav-tab__item').filter(has_text='投稿').first
                    logger.info(f"[SpaceScraper] attempt {attempt+1}/3: tab_count={upload_tab.count()}")
                    
                    if upload_tab.count() > 0:
                        # 先移除遮罩（可能遮挡点击）
                        page.evaluate('() => document.querySelectorAll(".bili-mini-mask, .login-panel-popover").forEach(m => m.remove())')
                        page.wait_for_timeout(500)
                        
                        # JS 强制点击（绕过 Playwright 的 PointerEvent 检查）
                        try:
                            upload_tab.click(timeout=5000)
                        except Exception:
                            upload_tab.evaluate('el => el.click()')
                        
                        # 等待视频卡片出现（证明 SPA 导航成功）
                        page.wait_for_selector('.upload-video-card', timeout=15000)  # 改成 15 秒（和 test_video_click_only.py 一致）
                        tab_clicked = True
                        logger.info(f"[SpaceScraper] ✅ 投稿 tab 点击成功 (尝试 {attempt+1}/3)")
                        break
                except Exception as e:
                    logger.warning(f"[SpaceScraper] 投稿 tab 点击失败 (尝试 {attempt+1}/3): {e}")
                    # 模拟人类行为：滚动页面
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight / 2)')
                    page.wait_for_timeout(3000)
            
            # 如果 SPA 点击失败，快速失败（不尝试直接导航，因为会触发 geetest 验证码）
            if not tab_clicked:
                logger.warning(f"[SpaceScraper] uid={uid} SPA 点击失败，跳过视频爬取（可能触发风控，需要有效 Cookie）")
                logger.warning(f"[SpaceScraper] 提示：在 config/accounts.py 中填入有效 B站 Cookie 可提升成功率")
                return videos  # 立即返回空列表（快速失败，不卡住）
            
            # 检查是否显示"暂无投稿"（不是错误，是用户确实没视频）
            page_text = page.evaluate('() => document.body.innerText')
            if '还没投过视频' in page_text or '暂无投稿' in page_text:
                logger.info(f"[SpaceScraper] uid={uid} 暂无投稿视频")
                return videos  # 返回空列表

            # 提取视频卡片 — B站 2026 新版 DOM 结构
            # 正确选择器（通过 debug_video_tab.py 和 test_save_html.py 验证）:
            #   .upload-video-card.grid-mode  — 卡片容器
            #   .bili-cover-card            — 封面链接 (href 含 BV 号)
            #   .bili-video-card__info__title — 标题
            #   .bili-video-card__cover img  — 封面图
            #   .bili-cover-card__stats       — 播放/弹幕统计（在 .bili-cover-card 内部！）
            for pn in range(max_pages):
                items = page.evaluate(r"""() => {
                    var items = [];
                    document.querySelectorAll('.upload-video-card.grid-mode').forEach(function(card) {
                        var linkEl = card.querySelector('.bili-cover-card');
                        var href = linkEl ? linkEl.getAttribute('href') : '';
                        var bvMatch = href.match(/BV[a-zA-Z0-9]{10}/);
                        var bvid = bvMatch ? bvMatch[0] : '';

                        // 标题：多选择器兜底（2026-06 真实结构 .bili-video-card__title[title="xxx"] > a）
                        var titleEl = card.querySelector(
                            '.bili-video-card__title, ' +
                            '.bili-video-card__info__title, ' +
                            '.video-name'
                        );
                        // 优先用 title 属性，其次用文字内容
                        var title = '';
                        if (titleEl) {
                            title = titleEl.getAttribute('title') || titleEl.textContent.trim();
                        } else if (linkEl) {
                            title = linkEl.getAttribute('title') || '';
                        }

                        var imgEl = card.querySelector('.bili-video-card__cover img, img');
                        
                        // 播放量：在 .bili-cover-card__stats 里面（2026 新版 DOM）
                        var play = 0;
                        if (linkEl) {
                            var statsEl = linkEl.querySelector('.bili-cover-card__stats');
                            if (statsEl) {
                                var firstStat = statsEl.querySelector('.bili-cover-card__stat span');
                                if (firstStat) {
                                    play = parseInt(firstStat.textContent.replace(/[^0-9]/g,'')) || 0;
                                }
                            }
                        }

                        if (bvid) {
                            items.push({
                                bvid: bvid,
                                title: title,
                                cover: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
                                play: play,
                            });
                        }
                    });
                    return items;
                }""")

                if items:
                    videos.extend(items)
                    logger.info(f"[SpaceScraper] page {pn+1}: {len(items)} videos")

                # 尝试翻页
                if pn < max_pages - 1:
                    try:
                        next_btn = page.locator(
                            '.be-pager-next, [class*="pager-next"], .bili-pager-next, .next:not([disabled])'
                        ).first
                        if next_btn.count() > 0 and next_btn.is_enabled():
                            next_btn.click()
                            page.wait_for_timeout(2500)
                        else:
                            break
                    except Exception:
                        break
                if len(items) < 20:
                    break

            logger.info(f"[SpaceScraper] ✅ {len(videos)} videos total")
            return videos

        except Exception as e:
            logger.error(f"[SpaceScraper] videos({uid}) error: {e}")
            return videos
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ============================================================
    #  动态列表
    # ============================================================

    def scrape_user_posts(self, uid: int, max_scroll: int = 10) -> List[Dict[str, Any]]:
        """
        爬取用户动态列表.

        策略: 先加载画像页 → 点击 "动态" tab (SPA 客户端导航) → 滚动加载.

        返回: [{"content", "type", "time"}, ...]
        """
        url = f"https://space.bilibili.com/{uid}"
        logger.info(f"[SpaceScraper] posts uid={uid}")
        page = self._get_page()
        posts = []

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(4000)
            _close_geetest(page)
            
            # ★ 检测登录墙/CAPTCHA，提示用户手动操作
            if not _wait_for_manual_bypass(page, headless=self.headless, max_wait_sec=300):
                logger.warning(f"[SpaceScraper] uid={uid} 手动操作等待超时/失败，跳过动态爬取")
                return posts

            # 点击 "动态" tab（使用重试逻辑，和 videos 一致）
            tab_clicked = False
            for attempt in range(3):
                try:
                    page.wait_for_selector('.nav-tab__item', timeout=10000)
                    
                    dyn_tab = page.locator('.nav-tab__item').filter(has_text='动态').first
                    logger.info(f"[SpaceScraper] posts attempt {attempt+1}/3: tab_count={dyn_tab.count()}")
                    
                    if dyn_tab.count() > 0:
                        # 先移除遮罩
                        page.evaluate('() => document.querySelectorAll(".bili-mini-mask, .login-panel-popover").forEach(m => m.remove())')
                        page.wait_for_timeout(500)
                        
                        # 点击
                        try:
                            dyn_tab.click(timeout=5000)
                        except Exception:
                            dyn_tab.evaluate('el => el.click()')
                        
                        # 等待动态内容加载（等待 .bili-dyn-list 或 .bili-dyn-card 出现）
                        try:
                            page.wait_for_selector('.bili-dyn-list, .bili-dyn-card, [class*="dyn"]', timeout=15000)
                            tab_clicked = True
                            logger.info(f"[SpaceScraper] ✅ 动态 tab 点击成功 (尝试 {attempt+1}/3)")
                            break
                        except Exception as e:
                            logger.warning(f"[SpaceScraper] 动态内容未加载 (尝试 {attempt+1}/3): {e}")
                    
                except Exception as e:
                    logger.warning(f"[SpaceScraper] 动态 tab 点击失败 (尝试 {attempt+1}/3): {e}")
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight / 2)')
                    page.wait_for_timeout(3000)
            
            # 如果 SPA 点击失败，快速失败（不尝试直接导航，因为会触发 geetest）
            if not tab_clicked:
                logger.warning(f"[SpaceScraper] uid={uid} 动态 SPA 点击失败，跳过动态爬取")
                return posts  # 立即返回空列表
            
            # 滚动加载
            for _ in range(max_scroll):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)

            # 提取动态（临时选择器，可能需要更新）
            items = page.evaluate(r"""() => {
                var items = [];
                document.querySelectorAll(
                    '.bili-dyn-card, .dyn-card, [class*="dyn-item"], [class*="feed-card"]'
                ).forEach(function(card) {
                    var contentEl = card.querySelector('[class*="content"]');
                    var timeEl = card.querySelector('[class*="time"]');
                    var text = '';
                    if (contentEl) {
                        var clone = contentEl.cloneNode(true);
                        clone.querySelectorAll('[class*="card"], [class*="btn"], [class*="button"], [class*="stat"]').forEach(function(n) { n.remove(); });
                        text = clone.textContent.trim().slice(0, 500);
                    }
                    items.push({
                        content: text,
                        time: timeEl ? timeEl.textContent.trim() : '',
                    });
                });
                return items;
            }""")
            posts = items
            logger.info(f"[SpaceScraper] ✅ {len(posts)} posts")
            return posts

        except Exception as e:
            logger.error(f"[SpaceScraper] posts({uid}) error: {e}")
            return posts
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ============================================================
    #  专栏 / 音频 (用现有逻辑，选择器可能也需要更新)
    # ============================================================

    def scrape_user_opus(self, uid: int, max_pages: int = 5) -> List[Dict[str, Any]]:
        url = f"https://space.bilibili.com/{uid}/upload/opus"
        logger.info(f"[SpaceScraper] opus uid={uid}")
        page = self._get_page()
        opus = []
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(4000)
            _close_geetest(page)
            result = page.evaluate("""() => {
                const items = [];
                document.querySelectorAll('[class*=\"opus\"], [class*=\"article-card\"]').forEach(card => {
                    const t = card.querySelector('[class*=\"title\"]');
                    const l = card.querySelector('a[href*=\"cv\"]');
                    const i = card.querySelector('img');
                    const href = l ? l.getAttribute('href') : '';
                    items.push({
                        cvid: (href.match(/cv(\\d+)/) || ['',''])[1],
                        title: t ? t.textContent.trim() : '',
                        cover: i ? (i.src || i.getAttribute('data-src') || '') : '',
                    });
                });
                return items;
            }""")
            opus = result
            return opus
        except Exception as e:
            logger.error(f"[SpaceScraper] opus({uid}) error: {e}")
            return opus
        finally:
            try: page.close()
            except: pass

    def scrape_user_audio(self, uid: int, max_pages: int = 3) -> List[Dict[str, Any]]:
        url = f"https://space.bilibili.com/{uid}/upload/audio"
        logger.info(f"[SpaceScraper] audio uid={uid}")
        page = self._get_page()
        audio = []
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            page.wait_for_timeout(4000)
            _close_geetest(page)
            result = page.evaluate("""() => {
                const items = [];
                document.querySelectorAll('[class*=\"audio-card\"], [class*=\"song-item\"]').forEach(card => {
                    const t = card.querySelector('[class*=\"title\"]');
                    const l = card.querySelector('a[href*=\"au\"]');
                    const i = card.querySelector('img');
                    const href = l ? l.getAttribute('href') : '';
                    items.push({
                        auid: (href.match(/au(\\d+)/) || ['',''])[1],
                        title: t ? t.textContent.trim() : '',
                        cover: i ? (i.src || '') : '',
                    });
                });
                return items;
            }""")
            audio = result
            return audio
        except Exception as e:
            logger.error(f"[SpaceScraper] audio({uid}) error: {e}")
            return audio
        finally:
            try: page.close()
            except: pass

    def close(self):
        browser, context = _get_thread_browser()
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        _thread_local.browser = None
        _thread_local.context = None


# ============================================================
#  便捷函数
# ============================================================

def scrape_user_profile(uid: int, cookie: str = "", headless: bool = True) -> Dict[str, Any]:
    scraper = SpacePageScraper(cookie=cookie, headless=headless)
    return scraper.scrape_user_profile(uid)


def scrape_user_videos(uid: int, cookie: str = "", headless: bool = True, max_pages: int = 5) -> List[Dict[str, Any]]:
    scraper = SpacePageScraper(cookie=cookie, headless=headless)
    return scraper.scrape_user_videos(uid, max_pages=max_pages)


def scrape_user_posts(uid: int, cookie: str = "", headless: bool = True, max_scroll: int = 10) -> List[Dict[str, Any]]:
    scraper = SpacePageScraper(cookie=cookie, headless=headless)
    return scraper.scrape_user_posts(uid, max_scroll=max_scroll)


def scrape_user_opus(uid: int, cookie: str = "", headless: bool = True) -> List[Dict[str, Any]]:
    scraper = SpacePageScraper(cookie=cookie, headless=headless)
    return scraper.scrape_user_opus(uid)


def scrape_user_audio(uid: int, cookie: str = "", headless: bool = True) -> List[Dict[str, Any]]:
    scraper = SpacePageScraper(cookie=cookie, headless=headless)
    return scraper.scrape_user_audio(uid)
