"""
一键分析脚本 — 水军检测引擎

对指定视频的评论数据进行分析, 生成水军检测报告。

用法:
  # 分析单个视频
  python deploy/run_analyzer.py BV1xx411c7mD

  # 分析所有已采集视频
  python deploy/run_analyzer.py --all

  # 分析评论最多的前N个视频
  python deploy/run_analyzer.py --top 10

  # 列出所有可分析的视频
  python deploy/run_analyzer.py --list
"""

import argparse
import json
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR, VIDEO_DIR, COMMENT_DIR, REPORT_DIR
from analyzer.similarity_detector import SimilarityDetector
from analyzer.time_analyzer import TimeAnalyzer
from analyzer.feature_extractor import FeatureExtractor
from analyzer.scorer import WaterArmyScorer
from analyzer.llm_analyzer import LLMAnalyzer, create_llm_analyzer
from analyzer.report_generator import ReportGenerator


def analyze_video(bvid: str, verbose: bool = True):
    """
    对单个视频执行完整分析流程。

    流程:
      1. 加载评论 JSON
      2. 加载视频信息
      3. SimilarityDetector → 相似度矩阵 + 集群
      4. TimeAnalyzer → 时间爆发 + 注册批次
      5. FeatureExtractor → 10个特征
      6. WaterArmyScorer → 评分排序
      7. ReportGenerator → 生成 + 保存报告
    """
    # ---- 加载数据 ----
    comment_path = os.path.join(COMMENT_DIR, f"{bvid}_comments.json")
    video_path = os.path.join(VIDEO_DIR, f"{bvid}.json")

    if not os.path.exists(comment_path):
        print(f"[ERROR] Comments file not found: {comment_path}")
        return None

    with open(comment_path, "r", encoding="utf-8") as f:
        comments = json.load(f)

    video_info = {}
    if os.path.exists(video_path):
        with open(video_path, "r", encoding="utf-8") as f:
            video_info = json.load(f)

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  Analyzing: {bvid}")
        print(f"  Title: {video_info.get('title', 'N/A')}")
        print(f"  Comments: {len(comments)}")
        print(f"{'=' * 60}\n")

    # ---- 相似度检测 ----
    if verbose:
        print("[1/4] Building similarity matrix...")
    sim_detector = SimilarityDetector(comments, threshold=0.75)
    sim_detector.build_matrix()
    clusters = sim_detector.find_clusters()
    if verbose:
        print(f"      Found {len(clusters)} similarity clusters")
        print(f"      Similarity pairs: {len(sim_detector.similarity_matrix)}")

    # ---- 时间分析 ----
    if verbose:
        print("[2/4] Analyzing time patterns...")
    # Load users if available
    users = {}
    user_posts = {}  # v2.1: F12-F14 数据源
    users_dir = os.path.join(DATA_DIR, "users")
    if os.path.exists(users_dir):
        for fname in os.listdir(users_dir):
            if fname.endswith(".json") and fname != "unique_mids.json" and not fname.endswith("_posts.json"):
                with open(os.path.join(users_dir, fname), "r", encoding="utf-8") as f:
                    user_data = json.load(f)
                    users[user_data.get("mid")] = user_data
            elif fname.endswith("_posts.json"):
                mid_str = fname.replace("_posts.json", "")
                try:
                    mid = int(mid_str)
                except ValueError:
                    continue
                with open(os.path.join(users_dir, fname), "r", encoding="utf-8") as f:
                    posts_data = json.load(f)
                    user_posts[mid] = posts_data if isinstance(posts_data, list) else []

    time_analyzer = TimeAnalyzer(comments, users)
    burst_scores = time_analyzer.detect_time_burst()
    batch_scores = time_analyzer.detect_registration_batch()
    timeline = time_analyzer.get_comment_timeline()
    if verbose:
        burst_count = sum(1 for s in burst_scores.values() if s > 0.5)
        print(f"      Burst users detected: {burst_count}")
        print(f"      Timeline points: {len(timeline)}")

    # ---- 特征提取 ----
    if verbose:
        print("[3/4] Extracting features...")
    extractor = FeatureExtractor(
        comments, users,
        sim_detector.get_user_similarity_score,
        burst_scores,
        batch_scores,
        user_posts=user_posts,  # v2.1: F12-F14 数据源
    )
    features_list = extractor.extract_all()
    if verbose:
        print(f"      Users analyzed: {len(features_list)}")
        posts_users = sum(1 for p in user_posts.values() if p)
        if posts_users > 0:
            print(f"      User posts loaded: {posts_users} users, {sum(len(p) for p in user_posts.values())} posts")

    # ---- 评分 ----
    if verbose:
        print("[4/4] Scoring and generating report...")
    scorer = WaterArmyScorer()
    scored_users = scorer.score_users(features_list)
    stats = scorer.get_statistics(scored_users)

    if verbose:
        print(f"      High risk: {stats['high_risk_count']}")
        print(f"      Medium risk: {stats['medium_risk_count']}")
        print(f"      Low risk: {stats['low_risk_count']}")

    # ---- LLM 深度语义分析 (可选) ----
    # 注意: AICU 深度分析已改为手动触发 (Dashboard → 深度分析按钮)
    #       自动触发已移除，避免无人值守时产生高额 API 费用
    llm_result = None
    llm_analyzer = create_llm_analyzer()
    if llm_analyzer:
        if verbose:
            print(f"\n[+] LLM 深度语义分析 (DeepSeek)...")
        llm_result = llm_analyzer.analyze(scored_users, comments, video_info)
        if llm_result["llm_available"]:
            scored_users = llm_result["enhanced_users"]
            stats["llm_stats"] = llm_result.get("stats", {})
            if verbose:
                print(f"      LLM 确认水军: {llm_result['stats'].get('llm_positive', 0)}")
                print(f"      API 调用次数: {llm_result['stats'].get('total_calls', 0)}")
        else:
            if verbose:
                print(f"      LLM 不可用: {llm_result.get('error', '未知错误')}")
    else:
        if verbose:
            print(f"\n[-] LLM 分析跳过 (未配置 DEEPSEEK_API_KEY)")

    # ---- 生成报告 ----
    # 注意: deep_result 永远为 None (深度分析已改为 Dashboard 手动触发)
    generator = ReportGenerator(
        video_bvid=bvid,
        video_info=video_info,
        scored_users=scored_users,
        stats=stats,
        similarity_clusters=clusters,
        timeline=timeline,
        llm_summary=llm_result.get("llm_summary") if llm_result else None,
        llm_stats=llm_result.get("stats") if llm_result else None,
        deep_summary=None,
        deep_stats=None,
        comments=comments,
    )
    report = generator.generate()
    generator.save_report()

    # ---- 打印摘要 ----
    if verbose:
        top = scorer.get_top_suspects(scored_users, top_n=5)
        print(f"\n  TOP 5 Suspects:")
        print(f"  {'Rank':<6} {'Username':<20} {'Score':<8} {'Lv':<4} {'Comments':<10}")
        print(f"  {'-' * 55}")
        for i, user in enumerate(top, 1):
            print(
                f"  {i:<6} {user['uname'][:18]:<20} "
                f"{user['suspicious_score']:<8.1f} "
                f"{user.get('level', '?'):<4} "
                f"{user['comment_count']:<10}"
            )

        print(f"\n  Report saved: data/reports/{bvid}_report.json")
        print(f"{'=' * 60}\n")

    return report


def analyze_all(verbose: bool = False):
    """分析所有已采集视频"""
    if not os.path.exists(COMMENT_DIR):
        print(f"Comment directory not found: {COMMENT_DIR}")
        return

    comment_files = [
        f for f in os.listdir(COMMENT_DIR)
        if f.endswith("_comments.json")
    ]

    bvids = [f.replace("_comments.json", "") for f in comment_files]

    if not bvids:
        print("No comment data found. Run the spider first.")
        return

    for i, bvid in enumerate(bvids, 1):
        print(f"\n[{i}/{len(bvids)}] {bvid}")
        analyze_video(bvid, verbose=verbose)


def analyze_top(n: int = 10):
    """分析评论最多的前N个视频"""
    if not os.path.exists(COMMENT_DIR):
        print("No comment data found.")
        return

    # Find largest comment files
    comment_files = [
        f for f in os.listdir(COMMENT_DIR)
        if f.endswith("_comments.json")
    ]

    file_sizes = []
    for f in comment_files:
        path = os.path.join(COMMENT_DIR, f)
        bvid = f.replace("_comments.json", "")
        with open(path, "r", encoding="utf-8") as fp:
            comments = json.load(fp)
        file_sizes.append((bvid, len(comments)))

    file_sizes.sort(key=lambda x: x[1], reverse=True)
    top_bvids = file_sizes[:n]

    for i, (bvid, count) in enumerate(top_bvids, 1):
        print(f"\n[{i}/{len(top_bvids)}] {bvid} ({count} comments)")
        analyze_video(bvid, verbose=True)


def list_videos():
    """列出所有可分析的视频"""
    print("\nAvailable videos for analysis:\n")
    print(f"{'BV ID':<20} {'Comments':<10} {'File'}")
    print("-" * 60)

    if not os.path.exists(COMMENT_DIR):
        print("  (no comment data)")
        return

    comment_files = sorted([
        f for f in os.listdir(COMMENT_DIR)
        if f.endswith("_comments.json")
    ])

    for f in comment_files:
        path = os.path.join(COMMENT_DIR, f)
        bvid = f.replace("_comments.json", "")
        with open(path, "r", encoding="utf-8") as fp:
            comments = json.load(fp)
        # Check if already analyzed
        has_report = os.path.exists(os.path.join(REPORT_DIR, f"{bvid}_report.json"))
        status = "[has report]" if has_report else "[new]"
        print(f"{bvid:<20} {len(comments):<10} {status}")


def main():
    parser = argparse.ArgumentParser(description="B站水军检测 — 一键分析")
    parser.add_argument("bvid", nargs="?", type=str, help="BV号 (分析单个视频)")
    parser.add_argument("--all", action="store_true", help="分析所有已采集视频")
    parser.add_argument("--top", type=int, metavar="N", help="分析评论最多的N个视频")
    parser.add_argument("--list", action="store_true", help="列出所有可分析视频")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")

    args = parser.parse_args()

    if args.list:
        list_videos()
    elif args.all:
        analyze_all(verbose=args.verbose)
    elif args.top:
        analyze_top(args.top)
    elif args.bvid:
        analyze_video(args.bvid, verbose=True)
    else:
        parser.print_help()
        print("\n示例:")
        print("  python deploy/run_analyzer.py BV1xx411c7mD")
        print("  python deploy/run_analyzer.py --top 10")
        print("  python deploy/run_analyzer.py --all")
        print("  python deploy/run_analyzer.py --list")


if __name__ == "__main__":
    main()
