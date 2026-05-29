"""
登录状态管理器

参考: MediaCrawler 的登录态管理 + Cookie持久化

功能:
- 全局单例模式管理登录态
- 自动检测Cookie有效性
- 提供SESSDATA等关键Cookie获取
"""

import logging
from typing import Optional, Dict

from bilibili_crawler.login.bilibili_login import BilibiliLogin

logger = logging.getLogger(__name__)


class LoginManager:
    """
    登录状态管理器（单例模式）。

    参考 MediaCrawler 的 login 模块设计，
    全局管理 B站 登录态，避免重复登录。
    """

    _instance: Optional["LoginManager"] = None
    _login: Optional[BilibiliLogin] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_login(cls) -> BilibiliLogin:
        """获取 BilibiliLogin 实例"""
        if cls._login is None:
            cls._login = BilibiliLogin()
        return cls._login

    @classmethod
    async def ensure_login(cls) -> str:
        """
        确保已登录状态。

        优先级:
        1. 已保存的Cookie（如果有效）
        2. 如果都无效，返回空字符串

        Returns:
            Cookie字符串，未登录返回空字符串
        """
        login = cls.get_login()

        # 尝试加载保存的登录态
        saved = await login.load_login_state()
        if saved and await login._validate_cookies():
            logger.info("使用已保存的登录态")
            return login.get_cookies_string()

        # 未登录
        logger.info("未登录，部分API功能受限")
        return ""

    @classmethod
    def get_sessdata(cls) -> Optional[str]:
        """获取SESSDATA Cookie值（最重要的B站认证Cookie）"""
        if cls._login is None:
            return None
        cookies = cls._login.get_cookies_dict()
        return cookies.get("SESSDATA")

    @classmethod
    def get_cookies_dict(cls) -> Dict[str, str]:
        """获取Cookie字典"""
        if cls._login is None:
            return {}
        return cls._login.get_cookies_dict()

    @classmethod
    def is_logged_in(cls) -> bool:
        """检查是否已登录（优先检查内存中的登录态，其次尝试从文件加载）"""
        if cls._login is None:
            cls._login = BilibiliLogin()
        # 如果内存中没有Cookie，尝试从文件加载
        if not cls._login._cookies:
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                saved = loop.run_until_complete(cls._login.load_login_state())
                loop.close()
                if not saved:
                    return False
            except Exception:
                return False
        return bool(cls._login.get_cookies_dict())
