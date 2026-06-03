"""
B站登录实现

参考: MediaCrawler media_platform/bilibili/login.py

支持:
- 二维码登录: 打开B站登录页 → 显示二维码 → 用户扫码 → 获取Cookie
- Cookie登录: 直接使用已有Cookie字符串
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class BilibiliLogin:
    """
    B站登录管理器。

    参考 MediaCrawler 的登录策略模式:
    - login_by_qrcode: 二维码登录
    - login_by_cookies: Cookie登录
    """

    BILIBILI_LOGIN_URL = "https://passport.bilibili.com/login"
    QRCODE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QRCODE_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

    def __init__(self, cookie_file: str = None):
        """
        Args:
            cookie_file: Cookie持久化文件路径
        """
        if cookie_file is None:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cookie_file = os.path.join(root, "data", "cookies.json")

        self._cookie_file = cookie_file
        self._cookies: Dict[str, str] = {}
        self._browser_context = None

    # ---- 二维码登录（参考 MediaCrawler 模式） ----

    async def login_by_qrcode(self, browser_context=None) -> str:
        """
        二维码登录流程。

        1. 调用B站API生成登录二维码
        2. 在浏览器中显示二维码 / 输出终端URL
        3. 轮询B站API检测扫码状态
        4. 扫码成功后提取Cookie

        Args:
            browser_context: Playwright BrowserContext（可选）

        Returns:
            Cookie字符串

        Raises:
            TimeoutError: 扫码超时
        """
        import requests

        logger.info("开始二维码登录流程...")

        # Step 1: 生成二维码
        resp = requests.get(
            self.QRCODE_API,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "https://www.bilibili.com",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {data.get('message')}")

        qrcode_data = data["data"]
        qrcode_key = qrcode_data["qrcode_key"]
        qrcode_url = qrcode_data["url"]

        # 输出二维码URL（用户可用手机扫码）
        print("\n" + "=" * 50)
        print("请使用B站APP扫描以下二维码登录:")
        print(f"二维码URL: {qrcode_url}")
        print("=" * 50 + "\n")

        # 如果在浏览器上下文中，打开登录页
        if browser_context:
            page = await browser_context.new_page()
            await page.goto(
                f"https://passport.bilibili.com/x/passport-login/web/qrcode/show?qrcode_key={qrcode_key}"
            )
            logger.info("已在浏览器中显示二维码，请扫码登录")

        # Step 2: 轮询扫码状态
        poll_interval = 2  # 秒
        max_poll_time = 180  # 3分钟超时
        start_time = time.time()

        while time.time() - start_time < max_poll_time:
            try:
                poll_resp = requests.get(
                    self.QRCODE_POLL_API,
                    params={"qrcode_key": qrcode_key},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                    },
                    timeout=10,
                )
                poll_data = poll_resp.json()

                if poll_data.get("code") == 0:
                    # 扫码成功
                    cookie_data = poll_data.get("data", {})
                    self._cookies = self._extract_cookies_from_response(poll_resp)
                    logger.info("二维码登录成功!")

                    # 保存Cookie
                    await self.save_login_state()
                    return self._cookies_to_string()

                elif poll_data.get("code") == 86038:
                    logger.info("二维码已过期，请重新生成")
                    raise RuntimeError("二维码已过期")

                elif poll_data.get("code") == 86090:
                    logger.info("已扫码，请在手机上确认...")

                elif poll_data.get("code") == 86101:
                    logger.info("等待扫码...")

                else:
                    logger.debug(f"轮询状态: code={poll_data.get('code')}")

            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"轮询异常: {e}")

            await asyncio.sleep(poll_interval)

        raise TimeoutError("扫码超时(3分钟)")

    # ---- Cookie登录（参考 MediaCrawler cookie 模式） ----

    async def login_by_cookies(self, cookie_str: str = None) -> str:
        """
        Cookie登录。

        Args:
            cookie_str: Cookie字符串（不传则从文件加载）

        Returns:
            Cookie字符串
        """
        if cookie_str:
            self._cookies = self._parse_cookie_string(cookie_str)
        else:
            # 从文件加载
            await self.load_login_state()

        if not self._cookies:
            raise ValueError("Cookie为空，请先执行二维码登录或提供Cookie")

        # 验证Cookie有效性
        if await self._validate_cookies():
            logger.info("Cookie登录成功（已保存的登录态）")
            return self._cookies_to_string()
        else:
            logger.warning("Cookie已失效，需要重新登录")
            self._cookies = {}
            return ""

    # ---- Cookie持久化（参考 MediaCrawler SAVE_LOGIN_STATE） ----

    async def save_login_state(self) -> None:
        """保存登录态到文件（参考 MediaCrawler persistent_context）"""
        if not self._cookies:
            return

        os.makedirs(os.path.dirname(self._cookie_file), exist_ok=True)
        with open(self._cookie_file, "w", encoding="utf-8") as f:
            json.dump(self._cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"登录态已保存: {self._cookie_file}")

    def save_login_state_sync(self) -> None:
        """同步保存登录态（供 Flask 同步端点使用）"""
        if not self._cookies:
            return
        os.makedirs(os.path.dirname(self._cookie_file), exist_ok=True)
        with open(self._cookie_file, "w", encoding="utf-8") as f:
            json.dump(self._cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"登录态已保存: {self._cookie_file}")

    async def load_login_state(self) -> Optional[Dict[str, str]]:
        """从文件加载登录态"""
        if not os.path.exists(self._cookie_file):
            logger.info("未找到已保存的登录态")
            return None

        try:
            with open(self._cookie_file, "r", encoding="utf-8") as f:
                self._cookies = json.load(f)
            logger.info("已加载保存的登录态")
            return self._cookies
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"加载登录态失败: {e}")
            return None

    # ---- 工具方法 ----

    async def _validate_cookies(self) -> bool:
        """验证Cookie有效性（通过B站 nav 接口）"""
        if not self._cookies:
            return False

        import requests
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                cookies=self._cookies,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                    "Referer": "https://www.bilibili.com",
                },
                timeout=10,
            )
            data = resp.json()
            # code=0 且 data.isLogin=true 表示登录有效
            return data.get("code") == 0 and data.get("data", {}).get("isLogin", False)
        except Exception:
            return False

    def get_cookies_dict(self) -> Dict[str, str]:
        """获取Cookie字典（供 requests 库使用）"""
        return dict(self._cookies)

    def get_cookies_string(self) -> str:
        """获取Cookie字符串"""
        return self._cookies_to_string()

    def _cookies_to_string(self) -> str:
        """Cookie字典转换为字符串"""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    @staticmethod
    def _parse_cookie_string(cookie_str: str) -> Dict[str, str]:
        """解析Cookie字符串为字典"""
        cookies = {}
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                key, value = item.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies

    @staticmethod
    def _extract_cookies_from_response(response) -> Dict[str, str]:
        """从 requests.Response 提取 Set-Cookie（值已 URL 解码）"""
        from urllib.parse import unquote
        cookies = {}
        for cookie in response.cookies:
            cookies[cookie.name] = unquote(cookie.value)
        return cookies
