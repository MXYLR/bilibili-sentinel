"""
水军分析报告生成器

生成详细 JSON 报告 → data/reports/{bvid}_report.json
"""

import json
import os
from datetime import datetime


class ReportGenerator:
    """
    水军分析报告生成器。

    输入: 视频信息、评分结果、统计摘要、相似集群、时间线
    输出: 完整 JSON 报告
    """

    def __init__(self, video_bvid: str, video_info: dict,
                 scored_users: list, stats: dict,
                 similarity_clusters: list, timeline: list,
                 llm_summary: str = None, llm_stats: dict = None,
                 deep_summary: str = None, deep_stats: dict = None,
                 comments: list = None):
        self.bvid = video_bvid
        self.video_info = video_info
        self.scored_users = scored_users
        self.stats = stats
        self.similarity_clusters = similarity_clusters
        self.timeline = timeline
        self.llm_summary = llm_summary
        self.llm_stats = llm_stats or {}
        self.deep_summary = deep_summary
        self.deep_stats = deep_stats or {}
        self.comments = comments or []

    def generate(self) -> dict:
        """
        生成完整报告 dict。

        Report structure:
        {
          "report_meta": { bvid, generated_at, analyzer_version },
          "video_info": { title, owner, stats, ... },
          "statistics": { total_users, risk_distribution, avg_score, ... },
          "top_suspects": [ { rank, mid, uname, score, features, sample } ],
          "similarity_clusters": [ { texts, count, members } ],
          "comment_timeline": [ { time, count } ],
          "ai_summary": "..."
        }
        """
        # Build top suspects
        top_suspects = []
        for i, user in enumerate(self.scored_users, 1):
            features = user.get("features", {})
            # Score each feature to 0-100 for readability
            normed_features = {
                k: round(v * 100, 1) for k, v in features.items()
            }
            top_suspects.append({
                "rank": i,
                "mid": user["mid"],
                "uname": user["uname"],
                "score": user.get("suspicious_score", 0),
                "risk_level": user.get("risk_level", "low"),
                "comment_count": user.get("comment_count", 0),
                "level": user.get("level", 0),
                "sign": user.get("sign", ""),  # v2.16: 个性签名
                "features": normed_features,
                "top_features": self._get_top_features(features, 3),
                "sample_comments": user.get("sample_comments", []),
                # LLM 分析结果
                "llm_type_id": user.get("llm_type_id", 0),
                "llm_type_name": user.get("llm_type_name", ""),
                "llm_confidence": user.get("llm_confidence", 0),
                "llm_reasoning": user.get("llm_reasoning", ""),
                # AICU 深度分析结果
                "deep_analyzed": "deep_type_id" in user,
                "deep_type_id": user.get("deep_type_id"),
                "deep_type_name": user.get("deep_type_name", ""),
                "deep_confidence": user.get("deep_confidence"),
                "deep_reasoning": user.get("deep_reasoning", ""),
                "deep_risk_confirmed": user.get("deep_risk_confirmed", False),
                "deep_key_evidence": user.get("deep_key_evidence", []),
                # AICU 元数据
                "aicu_comment_count": user.get("aicu_comment_count"),
                "aicu_stats": user.get("aicu_stats"),
                "aicu_device": user.get("aicu_device", ""),
            })

        # Build similarity clusters (enriched with member details)
        clusters_formatted = []
        if self.similarity_clusters:
            # Build rpid → comment detail lookup
            comment_by_rpid = {}
            for c in (self.comments or []):
                rpid = c.get("rpid")
                if rpid:
                    comment_by_rpid[rpid] = {
                        "rpid": rpid,
                        "mid": c.get("mid", 0),
                        "uname": c.get("uname", c.get("member", {}).get("uname", "未知")),
                        "content": (c.get("content", "") or c.get("message", "")),
                        "ctime": c.get("ctime", 0),
                    }

            for cluster in self.similarity_clusters[:10]:
                member_rpids = cluster[:20]  # cap at 20
                # Build detailed member list
                members = []
                for rpid in member_rpids:
                    detail = comment_by_rpid.get(rpid)
                    if detail:
                        members.append(detail)
                    else:
                        members.append({
                            "rpid": rpid, "mid": 0,
                            "uname": "未知", "content": "(已删除)",
                            "ctime": 0,
                        })

                clusters_formatted.append({
                    "size": len(cluster),
                    "member_rpids": member_rpids,
                    "members": members,
                })

        # Build summary text
        summary = self._generate_summary()
        # Combine summaries
        ai_summary_parts = [self.llm_summary or summary]
        if self.deep_summary:
            ai_summary_parts.append("\n---\n")
            ai_summary_parts.append(self.deep_summary)

        report = {
            "report_meta": {
                "bvid": self.bvid,
                "generated_at": datetime.now().isoformat(),
                "analyzer_version": "2.1-deep-aicu",
            },
            "video_info": {
                "title": self.video_info.get("title", "N/A"),
                "bvid": self.bvid,
                "owner": self.video_info.get("owner_name", "N/A"),
                "view_count": self.video_info.get("view_count", 0),
                "reply_count": self.video_info.get("reply_count", 0),
                "pubdate": self.video_info.get("pubdate", 0),
                "pic": self.video_info.get("pic", ""),
            },
            "statistics": self.stats,
            "top_suspects": top_suspects,
            "similarity_clusters": clusters_formatted,
            "comment_timeline": self.timeline,
            "ai_summary": "".join(ai_summary_parts),
            "llm_stats": self.llm_stats,
            "deep_stats": self.deep_stats,
            # 全量评分用户（供前端 riskMap 使用，仅含必要字段）
            "scored_users_export": [
                {
                    "mid": u.get("mid"),
                    "suspicious_score": u.get("suspicious_score", 0),
                    "risk_level": u.get("risk_level", "low"),
                    "water_army_type": u.get("water_army_type", ""),
                    "llm_type_id": u.get("llm_type_id", 0),
                    "llm_confidence": u.get("llm_confidence", 0),
                    "deep_analyzed": u.get("deep_analyzed", False),
                }
                for u in self.scored_users
                if u.get("mid")
            ],
        }

        return report

    def _get_top_features(self, features: dict, n: int = 3) -> list:
        """
        找出最可疑的 top-N 特征。

        Returns:
            [("content_similarity", 0.9), ...]
        """
        feature_names = {
            "f1_account_age": "账号年龄",
            "f2_follow_ratio": "粉丝/关注比",
            "f3_level_score": "用户等级",
            "f4_avatar_verify": "头像/认证",
            "f5_content_similarity": "内容相似度",
            "f6_time_burst": "时间爆发",
            "f7_sentiment_extreme": "情感极端",
            "f8_like_ratio": "赞评比异常",
            "f9_registration_batch": "批量注册",
            "f10_interaction_ring": "互动小圈子",
            "f11_vip_anomaly": "VIP异常",
            "f12_account_skeleton": "账号骨架",
            "f13_lottery_repost": "转发抽奖",
            "f14_sensitive_content": "敏感内容",
            "f15_commercial_spam": "商业引流",
        }
        sorted_features = sorted(
            features.items(), key=lambda x: x[1], reverse=True
        )
        return [
            {"name": feature_names.get(k, k), "score": round(v * 100, 1)}
            for k, v in sorted_features[:n] if v > 0.3
        ]

    def _generate_summary(self) -> str:
        """生成中文摘要"""
        if not self.scored_users:
            return "暂无足够数据进行分析。"

        stats = self.stats
        total = stats.get("total_users", 0)
        high = stats.get("high_risk_count", 0)
        medium = stats.get("medium_risk_count", 0)
        cluster_count = len(self.similarity_clusters)

        parts = []
        parts.append(f"该视频评论区共检测到 {total} 个评论用户。")

        if high > 0:
            parts.append(
                f"其中高风险账号 {high} 个 ({high/total*100:.1f}%), "
                f"中风险 {medium} 个 ({medium/total*100:.1f}%)。"
            )

        if cluster_count > 0:
            parts.append(f"发现 {cluster_count} 组高度相似的评论聚类, 疑似模板化评论。")

        if high > 0 and self.scored_users:
            top = self.scored_users[0]
            parts.append(
                f"嫌疑最高账号: \"{top['uname']}\" (评分 {top['suspicious_score']}分), "
                f"发表 {top['comment_count']} 条评论。"
            )

        return "".join(parts)

    def save_report(self):
        """保存报告到 data/reports/{bvid}_report.json"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        report_dir = os.path.join(root, "data", "reports")
        os.makedirs(report_dir, exist_ok=True)

        report = self.generate()
        path = os.path.join(report_dir, f"{self.bvid}_report.json")

        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return path
