#!/usr/bin/env python3
"""
独立运行 Playwright SpacePageScraper 的脚本
从 stdin 读取 JSON {"mid":..., "cookie":...}，输出 JSON 结果到 stdout
用于 multiprocessing.Process 调用，避开 Windows 事件循环问题
"""
import sys
import json
import os

# ★ 修复: 将项目根目录加入 sys.path，确保 bilibili_crawler 包可导入
# run_pw_scraper.py 位于 bilibili_crawler/utils/run_pw_scraper.py
#   abspath → 文件自身
#   1st dirname → utils/
#   2nd dirname → bilibili_crawler/
#   3rd dirname → 项目根目录 (bilibili-sentinel/)
_proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

def main():
    # 从 stdin 读取参数
    req_json = sys.stdin.read().strip()
    if not req_json:
        print(json.dumps({}))
        return
    try:
        req = json.loads(req_json)
    except Exception:
        print(json.dumps({}))
        return

    mid = req.get("mid")
    cookie_str = req.get("cookie", "")
    headless = req.get("headless", True)

    if not mid:
        print(json.dumps({}))
        return

    # ★ 在子进程中设置事件循环策略（子进程是全新的，不受主进程影响）
    import asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    scraper = None  # ★ 在 try 块外初始化，确保 finally 可引用
    try:
        from bilibili_crawler.utils.playwright_space_scraper import SpacePageScraper
        scraper = SpacePageScraper(cookie=cookie_str, headless=headless, timeout=30000)
        profile = scraper.scrape_user_profile(mid)

        # ★ 同时爬取投稿视频和动态（会点击 tab，用户可在可见模式下观察）
        videos = []
        posts = []
        try:
            videos = scraper.scrape_user_videos(mid, max_pages=5)
        except Exception as e:
            import traceback
            print(json.dumps({"_videos_error": str(e), "_videos_traceback": traceback.format_exc()}), file=sys.stderr)

        try:
            posts = scraper.scrape_user_posts(mid, max_scroll=10)
        except Exception as e:
            import traceback
            print(json.dumps({"_posts_error": str(e), "_posts_traceback": traceback.format_exc()}), file=sys.stderr)

        result = {
            "profile": profile if profile else {},
            "videos": videos,
            "posts": posts,
        }
        print(json.dumps(result))
    except Exception as e:
        import traceback
        print(json.dumps({
            "_error": str(e),
            "_traceback": traceback.format_exc()
        }))
    finally:
        # ★ 确保浏览器窗口关闭，不残留在用户桌面
        if scraper is not None:
            try:
                scraper.close()
            except Exception:
                pass

if __name__ == "__main__":
    main()
