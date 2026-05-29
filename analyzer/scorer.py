"""
水军嫌疑加权评分引擎

计算公式:
  total_score = SUM(weight_i * feature_i)  for i in 1..10
  → 映射到 0-100 分
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DEFAULT_WEIGHTS, RISK_HIGH, RISK_MEDIUM


class WaterArmyScorer:
    """
    水军评分器。

    使用流程:
      1. scorer = WaterArmyScorer()
      2. scored = scorer.score_users(features_list)
      3. top = scorer.get_top_suspects(scored, top_n=20)
    """

    def __init__(self, weights: dict = None):
        self.weights = weights or DEFAULT_WEIGHTS

    def score_users(self, features_list: list) -> list:
        """
        对所有用户进行加权评分。

        Args:
            features_list: FeatureExtractor.extract_all() 的结果

        Returns:
            按 suspicious_score 降序排列的用户列表,
            每个额外添加 suspicious_score 和 risk_level

        v2.9: 确定性信号加成大幅强化 (F12 权重 0.10→0.15 + 硬加成升级)
          - F12 (账号骨架) 0.50→0.12, 0.75→0.25, 1.00→0.35
          - F14 (敏感内容) 1.0 → 硬加成 20 分
          - F15 (商业引流) 1.0 → 硬加成 20 分
        """
        for user_data in features_list:
            features = user_data.get("features", {})

            # ---- 基础加权分 ----
            raw_score = sum(
                self.weights.get(k, 0) * v
                for k, v in features.items()
            )

            # ---- v2.9: F12 硬加成大幅升级 ----
            decisive_bonus = 0.0
            decisive_tags = []

            # F12 账号骨架: 按命中数比例加成 (v2.9 升级: 分段精确)
            f12_val = features.get("f12_account_skeleton", 0)
            if f12_val >= 0.50:
                if f12_val >= 1.0:
                    f12_bonus = 0.35              # 4/4 铁证
                elif f12_val >= 0.75:
                    f12_bonus = 0.25 + (f12_val - 0.75) * 0.40  # 3/4→4/4 区间
                else:
                    f12_bonus = 0.12 + (f12_val - 0.50) * 0.52  # 2/4→3/4 区间
                decisive_bonus += f12_bonus
                decisive_tags.append(f"账号骨架({int(f12_val * 4)}/4)")

            # F14 敏感内容命中 → 强力佐证 (女拳/以乌/造谣)
            if features.get("f14_sensitive_content", 0) >= 1.0:
                decisive_bonus += 0.20
                decisive_tags.append("敏感内容")

            # F15 商业引流命中 → 强力佐证 (赌博/色情/加微信)
            if features.get("f15_commercial_spam", 0) >= 1.0:
                decisive_bonus += 0.20
                decisive_tags.append("商业引流")

            # 融合: 基础分 + 确定性加成, 封顶100分
            total_raw = min(1.0, raw_score + decisive_bonus)

            # Scale to 0-100
            user_data["suspicious_score"] = round(total_raw * 100, 1)

            if decisive_tags:
                user_data["decisive_signals"] = decisive_tags

            # Assign risk level
            if user_data["suspicious_score"] >= RISK_HIGH:
                user_data["risk_level"] = "high"
            elif user_data["suspicious_score"] >= RISK_MEDIUM:
                user_data["risk_level"] = "medium"
            else:
                user_data["risk_level"] = "low"

        # Sort desc
        features_list.sort(key=lambda x: x["suspicious_score"], reverse=True)
        return features_list

    def get_top_suspects(self, scored_users: list, top_n: int = None) -> list:
        """获取嫌疑最高的 TOP N，top_n 为 None 时返回全部"""
        if top_n is None:
            return scored_users
        return scored_users[:top_n]

    def get_statistics(self, scored_users: list) -> dict:
        """
        生成统计摘要。

        Returns:
            {
              "total_users": int,
              "high_risk_count": int,
              "medium_risk_count": int,
              "low_risk_count": int,
              "avg_score": float,
              "score_distribution": {"0-20": N, "20-40": N, ...}
            }
        """
        if not scored_users:
            return {
                "total_users": 0,
                "high_risk_count": 0,
                "medium_risk_count": 0,
                "low_risk_count": 0,
                "avg_score": 0,
                "score_distribution": {},
            }

        high = sum(1 for u in scored_users if u.get("risk_level") == "high")
        medium = sum(1 for u in scored_users if u.get("risk_level") == "medium")
        low = sum(1 for u in scored_users if u.get("risk_level") == "low")
        avg = sum(u["suspicious_score"] for u in scored_users) / len(scored_users)

        # Score distribution
        distribution = {"0-20": 0, "20-40": 0, "40-60": 0, "60-80": 0, "80-100": 0}
        for u in scored_users:
            s = u["suspicious_score"]
            if s < 20:
                distribution["0-20"] += 1
            elif s < 40:
                distribution["20-40"] += 1
            elif s < 60:
                distribution["40-60"] += 1
            elif s < 80:
                distribution["60-80"] += 1
            else:
                distribution["80-100"] += 1

        return {
            "total_users": len(scored_users),
            "high_risk_count": high,
            "medium_risk_count": medium,
            "low_risk_count": low,
            "avg_score": round(avg, 1),
            "score_distribution": distribution,
        }
