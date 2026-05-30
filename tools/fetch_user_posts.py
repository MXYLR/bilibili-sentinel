"""
批量采集用户动态数据，用于 F13/F14 特征的 posts 数据源。

用法:
  python tools/fetch_user_posts.py [--mids 129400041,3546383550] [--all]

从 data/comments/ 中提取所有评论者 MID，逐一获取其动态并保存到 data/users/{mid}_posts.json。
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cffi_requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bilibili_crawler.utils.bilibili_api import get_user_posts_url, prewarm_wbi_cache

POSTS_DIR = ROOT / "data" / "users"
MAX_POSTS_PER_USER = 50
DELAY = 0.8  # 请求间隔（秒）


def collect_mids_from_comments() -> set:
    """从所有评论文件提取 MID 集合。"""
    mids = set()
    comments_dir = ROOT / "data" / "comments"
    if not comments_dir.exists():
        return mids
    for fpath in comments_dir.glob("*_comments.json"):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                comments = json.load(f)
            for c in (comments if isinstance(comments, list) else []):
                mid = c.get("mid")
                if mid:
                    mids.add(int(mid))
        except Exception:
            pass
    return mids


def fetch_posts(mid: int, offset: str = "") -> tuple:
    """获取一页用户动态。返回 (items列表, 下一页offset或None)。"""
    url = get_user_posts_url(mid, offset=offset)
    try:
        resp = cffi_requests.get(url, impersonate="chrome124", timeout=20)
        if resp.status_code != 200:
            return [], None
        data = resp.json()
        items = (data.get("data") or {}).get("items", [])
        next_offset = (data.get("data") or {}).get("offset", "")
        has_more = (data.get("data") or {}).get("has_more", False)
        return items, (next_offset if has_more else None)
    except Exception as e:
        print(f"  [ERROR] fetch failed for mid={mid}: {e}")
        return [], None


def extract_post(item: dict) -> dict:
    """提取动态的关键字段。"""
    tp = item.get("type", "")
    bid = item.get("id_str", "") or item.get("basic", {}).get("rid_str", "")

    modules = item.get("modules", {})
    module_dynamic = modules.get("module_dynamic", {})
    major = module_dynamic.get("major", {})
    archive = major.get("archive", {})
    desc = module_dynamic.get("desc", {})

    # 内容来源：优先标题，其次文本
    content = archive.get("title", "") or (desc.get("text", "") or "")

    # 是否转发
    pub_action = modules.get("module_author", {}).get("pub_action", "")
    is_repost = "转发" in pub_action

    return {
        "dynamic_id": bid,
        "post_type": tp,
        "content": content,
        "is_repost": is_repost,
        "timestamp": item.get("modules", {}).get("module_author", {}).get("pub_time", ""),
    }


def save_posts(mid: int, posts: list):
    """保存用户动态到文件（去重追加）。"""
    os.makedirs(POSTS_DIR, exist_ok=True)
    path = POSTS_DIR / f"{mid}_posts.json"

    existing = []
    existing_ids = set()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_ids = {p.get("dynamic_id") for p in existing if isinstance(p, dict)}
        except Exception:
            pass

    new_posts = [p for p in posts if p.get("dynamic_id") not in existing_ids]
    if not new_posts:
        return 0

    existing.extend(new_posts)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))
    return len(new_posts)


def main():
    prewarm_wbi_cache()

    # 收集 MID
    mids = collect_mids_from_comments()
    print(f"从评论文件中收集到 {len(mids)} 个独立用户 MID")

    # 检查已有数据，跳过已采集的
    existing = {int(f.stem.replace("_posts", ""))
                for f in POSTS_DIR.glob("*_posts.json") if f.stem.endswith("_posts")}
    pending = sorted(mids - existing)
    print(f"已采集: {len(existing)}, 待采集: {len(pending)}")

    if not pending:
        print("无需采集。")
        return

    total_posts = 0
    for i, mid in enumerate(pending):
        print(f"[{i+1}/{len(pending)}] mid={mid} ...", end=" ", flush=True)

        all_posts = []
        offset = ""
        pages = 0
        while len(all_posts) < MAX_POSTS_PER_USER and pages < 5:
            items, next_offset = fetch_posts(mid, offset)
            if not items:
                break
            all_posts.extend(items)
            pages += 1
            if not next_offset:
                break
            offset = next_offset
            time.sleep(DELAY)

        posts = [extract_post(p) for p in all_posts]
        saved = save_posts(mid, posts)
        total_posts += saved

        repost_count = sum(1 for p in posts if p["is_repost"])
        print(f"{len(posts)} 条动态 (转发: {repost_count}), 新增 {saved} 条")
        time.sleep(DELAY)

    print(f"\n完成！共 {len(pending)} 用户, {total_posts} 条新动态")


if __name__ == "__main__":
    main()
