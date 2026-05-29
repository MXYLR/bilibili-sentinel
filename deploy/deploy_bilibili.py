"""
B站种子 URL 注入工具

将视频采集任务注入 Redis 队列, 供 BilibiliVideoSpider 消费。

用法:
  python deploy/deploy_bilibili.py --hot                  # 热门排行榜 (默认5页)
  python deploy/deploy_bilibili.py --hot --pages 10       # 10页热门
  python deploy/deploy_bilibili.py --bvid BV1xx411c7mD    # 单个视频
  python deploy/deploy_bilibili.py --keyword 华为          # 搜索关键词
  python deploy/deploy_bilibili.py --keyword 华为 --pages 3 # 搜索3页
  python deploy/deploy_bilibili.py --file bvids.txt       # 从文件批量注入 (每行一个BV号)
  python deploy/deploy_bilibili.py --status               # 查看当前队列状态
  python deploy/deploy_bilibili.py --clear                # 清空所有B站爬虫队列

种子 URL 格式 (Spider 内部解析):
  热门: bilibili_hot://page/1-5
  BV号: bilibili_bvid://BV1xx411c7mD
  搜索: bilibili_search://华为/page/1
"""

import argparse
import sys
import os

import redis

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REDIS_KEY_VIDEO = "bilibili_crawler:start_urls"
REDIS_KEY_COMMENT = "bilibili_crawler:comment_seeds"


def get_redis():
    """获取 Redis 连接 (db=1)"""
    return redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)


def show_status(r):
    """显示当前队列状态"""
    video_count = r.llen(REDIS_KEY_VIDEO)
    comment_count = r.llen(REDIS_KEY_COMMENT)

    print("=" * 50)
    print("  Bilibili Sentinel — 队列状态")
    print("=" * 50)
    print(f"  视频种子队列 (bilibili_video):    {video_count}")
    print(f"  评论种子队列 (bilibili_comment):  {comment_count}")
    print("=" * 50)


def clear_all(r):
    """清空所有队列"""
    print("警告: 将清空所有 B站爬虫 Redis 队列!")
    confirm = input("确认? (y/N): ")
    if confirm.lower() != "y":
        print("已取消")
        return
    r.delete(REDIS_KEY_VIDEO, REDIS_KEY_COMMENT)
    print("队列已清空")


def inject_hot(r, pages=5):
    """注入热门排行榜种子"""
    url = f"bilibili_hot://page/1-{pages}"
    result = r.lpush(REDIS_KEY_VIDEO, url)
    print(f"已注入热门排行榜种子: 1-{pages}页 ({pages * 50} 个视频)")


def inject_bvid(r, bvid):
    """注入单个BV号种子"""
    url = f"bilibili_bvid://{bvid}"
    result = r.lpush(REDIS_KEY_VIDEO, url)
    print(f"已注入 BV号种子: {bvid}")


def inject_keyword(r, keyword, pages=1):
    """注入搜索关键词种子"""
    count = 0
    for p in range(1, pages + 1):
        url = f"bilibili_search://{keyword}/page/{p}"
        r.lpush(REDIS_KEY_VIDEO, url)
        count += 1
    print(f"已注入搜索种子: '{keyword}' {count} 页")


def inject_file(r, filepath):
    """从文件批量注入BV号"""
    if not os.path.exists(filepath):
        print(f"文件不存在: {filepath}")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        bvids = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    count = 0
    for bvid in bvids:
        url = f"bilibili_bvid://{bvid}"
        r.lpush(REDIS_KEY_VIDEO, url)
        count += 1

    print(f"已从 {filepath} 注入 {count} 个种子")


def main():
    parser = argparse.ArgumentParser(description="B站种子URL注入工具")
    parser.add_argument("--hot", action="store_true", help="注入热门排行榜")
    parser.add_argument("--bvid", type=str, help="注入单个BV号")
    parser.add_argument("--keyword", type=str, help="注入搜索关键词")
    parser.add_argument("--file", type=str, help="从文件批量注入BV号")
    parser.add_argument("--pages", type=int, default=5, help="页数 (热门/搜索)")
    parser.add_argument("--status", action="store_true", help="查看队列状态")
    parser.add_argument("--clear", action="store_true", help="清空所有队列")

    args = parser.parse_args()
    r = get_redis()

    if args.status:
        show_status(r)
    elif args.clear:
        clear_all(r)
    elif args.hot:
        inject_hot(r, args.pages)
    elif args.bvid:
        inject_bvid(r, args.bvid)
    elif args.keyword:
        inject_keyword(r, args.keyword, args.pages)
    elif args.file:
        inject_file(r, args.file)
    else:
        parser.print_help()
        print("\n示例:")
        print("  python deploy/deploy_bilibili.py --hot --pages 3")
        print("  python deploy/deploy_bilibili.py --bvid BV1xx411c7mD")
        print("  python deploy/deploy_bilibili.py --keyword 科技 --pages 2")
        print("  python deploy/deploy_bilibili.py --status")


if __name__ == "__main__":
    main()
