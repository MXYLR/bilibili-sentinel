"""
反检测工具

参考: MediaCrawler tools/stealth_utils.py

提供:
- stealth.js 路径查找
- 浏览器指纹随机化
- 通用反检测辅助
"""

import os
import random


def get_stealth_js_path() -> str:
    """获取 stealth.min.js 路径（兼容多种安装方式）"""
    # 项目内路径
    project_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "libs", "stealth.min.js",
    )
    if os.path.exists(project_path):
        return project_path

    # 如果安装了 playwright-stealth
    try:
        import playwright_stealth
        pkg_dir = os.path.dirname(playwright_stealth.__file__)
        stealth_path = os.path.join(pkg_dir, "js", "stealth.min.js")
        if os.path.exists(stealth_path):
            return stealth_path
    except ImportError:
        pass

    return None


# 扩展的UA池（参考 MediaCrawler utils.get_user_agent）
_EXTENDED_UAS = [
    # Chrome 124 - Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 - Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.118 Safari/537.36",
    # Chrome 123 - Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 122 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Edge 124 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome 125 - Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def get_random_ua() -> str:
    """获取随机User-Agent"""
    return random.choice(_EXTENDED_UAS)


def get_playwright_stealth_args() -> list:
    """
    获取 Playwright 反检测启动参数。

    参考 MediaCrawler 的 browser launch args。
    """
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--disable-web-security",
        "--disable-features=VizDisplayCompositor",
    ]
