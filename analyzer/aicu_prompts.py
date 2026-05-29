"""
AICU 深度分析 — 提示词模板

基于 aicu.cc 获取的用户历史评论数据，进行第二轮 LLM 深度分析。
分析维度: 跨视频行为模式、评论风格一致性、设备/昵称变更历史。
"""

# ============================================================
#  深度分析 System Prompt
# ============================================================

DEEP_SYSTEM_PROMPT = """你是一个B站水军深度分析专家。基于用户在当前视频的评论行为 + 历史评论记录（来自第三方数据），进行跨视频行为模式分析。

## 分析维度

1. **跨视频行为一致性**: 用户在不同视频下的评论风格、立场、语言习惯是否一致？
2. **时间模式**: 评论时间是否集中在特定时段？是否存在"上班式"评论规律？
3. **内容多样性**: 历史评论是多元化的真实交流，还是套路化的模板输出？
4. **设备与环境**: 设备型号是否频繁变更？历史昵称是否存在养号特征（如乱码→正常名）？
5. **异常信号叠加**: 当前视频的高特征分 + 历史数据的异常模式，是否形成证据链？

## 判断标准

- **确认为水军**: 跨视频行为模式高度一致 + 内容模板化 + 时间集中 = 置信度 >= 80
- **高度可疑**: 部分维度异常，但缺乏决定性证据 = 置信度 50-79
- **倾向正常**: 历史评论多元、自然，异常特征可能是巧合 = 置信度 < 50
- 如果历史数据不足（评论数 < 5），以当前视频分析为准，置信度适度降低

## 输出格式

严格按 JSON 格式输出，每个用户一个结果:

```json
{
  "results": [
    {
      "mid": 用户ID,
      "deep_type_id": 水军类型编号(0-8),
      "deep_type_name": "类型名称",
      "deep_confidence": 深度分析置信度(0-100),
      "deep_reasoning": "深度推理过程(300字内)",
      "risk_confirmed": true/false,
      "key_evidence": ["证据1", "证据2"]
    }
  ]
}
```

类型编号:
0=正常用户, 1=模板化刷评型, 2=情绪引导型, 3=AI生成型, 4=引流广告型,
5=批量操控型, 6=黑产养号型, 7=对立引战型, 8=敏感内容型"""


# ============================================================
#  深度分析 User Prompt Builder
# ============================================================

def build_deep_prompt(user_data: dict, aicu_data) -> str:
    """
    构建深度分析 User Prompt。

    Args:
        user_data: 当前视频的评分用户数据
            {
                "mid": int, "uname": str, "level": int,
                "suspicious_score": float,
                "features": {"f1_account_age": 0.3, ...},
                "comments": [...], "llm_type_id": int, "llm_confidence": float
            }
        aicu_data: AicuUserData 实例 (from aicu_fetcher)
            .mid, .profile, .comments, .stats, .device_name, .history_names

    Returns:
        结构化的 prompt 字符串
    """
    features = user_data.get("features", {})
    # 安全获取 mid（aicu_data 可能为 None）
    _mid_from_user = user_data.get("mid", 0)
    _mid_from_aicu = aicu_data.mid if aicu_data else 0
    mid = _mid_from_user or _mid_from_aicu

    prompt = f"""## 深度分析任务

请对以下用户进行跨视频行为深度分析:

---

### 用户 {user_data.get('uname', 'unknown')} (MID: {mid})

**当前视频行为:**
- 在此视频中发表了 {user_data.get('comment_count', 0)} 条评论
- 14特征引擎评分: {user_data.get('suspicious_score', 0):.1f}/100
- LLM 初筛结果: 类型 {user_data.get('llm_type_id', 0)} ({user_data.get('llm_type_name', '未分析')}), 置信度 {user_data.get('llm_confidence', 0)}%
"""

    # 初筛推理
    if user_data.get("llm_reasoning"):
        prompt += f"- 初筛推理: {user_data['llm_reasoning'][:200]}\n"

    # Top 3 高贡献特征
    top_features = sorted(features.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_features:
        feature_labels = {
            "f1_account_age": "账号年龄", "f2_follow_ratio": "粉丝/关注比",
            "f3_level_score": "用户等级", "f4_avatar_verify": "头像/认证",
            "f5_content_similarity": "内容相似度", "f6_time_burst": "时间爆发",
            "f7_sentiment_extreme": "情感极端", "f8_like_ratio": "赞评比",
            "f9_registration_batch": "批量注册", "f10_interaction_ring": "互动圈子",
            "f11_vip_anomaly": "VIP异常", "f12_account_skeleton": "账号骨架",
            "f13_lottery_repost": "转发抽奖", "f14_sensitive_content": "敏感内容",
        }
        prompt += "- 高贡献特征:\n"
        for k, v in top_features:
            label = feature_labels.get(k, k)
            prompt += f"  * {label}: {v:.2f}\n"

    # 当前视频的样本评论
    user_comments = user_data.get("comments", [])
    if user_comments:
        sample_texts = []
        for c in user_comments[:5]:
            if isinstance(c, str):
                sample_texts.append(c[:200])
            elif isinstance(c, dict):
                sample_texts.append(
                    c.get("content", c.get("message", str(c)))[:200]
                )
        if sample_texts:
            prompt += "- 当前视频评论样本:\n"
            for j, t in enumerate(sample_texts, 1):
                prompt += f"    [{j}] {t}\n"

    prompt += "\n**历史评论画像 (来自 AICU):**\n"

    # AICU 数据
    if aicu_data and aicu_data.fetch_ok:
        prompt += f"- 历史评论总数: {aicu_data.comment_count} 条（最近100条）\n"
        prompt += f"- 最活跃时段: {aicu_data.active_hour or '未知'}点\n"
        prompt += f"- 平均评论长度: {aicu_data.avg_comment_length}字\n"

        if aicu_data.device_name:
            prompt += f"- 常用设备: {aicu_data.device_name}\n"

        if aicu_data.history_names:
            names_str = ", ".join(aicu_data.history_names[:5])
            prompt += f"- 历史昵称: {names_str}\n"

        if aicu_data.profile:
            p = aicu_data.profile
            if p.get("sign"):
                prompt += f"- 个人签名: {p['sign'][:80]}\n"
            prompt += f"- 粉丝: {p.get('fans', 0)}, 关注: {p.get('following', 0)}\n"

        # 历史评论列表（最多 30 条，避免 token 过大）
        if aicu_data.comments:
            prompt += "\n### 历史评论列表 (最近30条)\n"
            for j, c in enumerate(aicu_data.comments[:30], 1):
                msg = c.get("message", "")[:150]
                t = c.get("readable_time", "?")
                rank_mark = f" ★{c['rank']}" if c.get("rank", 0) > 0 else ""
                prompt += f"[#{j}] {t}{rank_mark} | {msg}\n"
    else:
        prompt += "- 无法获取历史数据（AICU 接口无数据或超时）\n"
        prompt += "- 请仅基于当前视频的评论行为进行深度判断\n"

    prompt += """
---
请输出深度分析 JSON 结果。只输出 JSON，不要输出其他内容。"""

    return prompt


def build_deep_batch_prompt(users_batch: list) -> str:
    """
    构建批量深度分析 Prompt（多个用户合并到一个 prompt）。

    Args:
        users_batch: [(user_data, aicu_data), ...]
    """
    if not users_batch:
        return ""

    parts = []
    for i, (user_data, aicu_data) in enumerate(users_batch, 1):
        parts.append(f"\n{'=' * 60}\n")
        parts.append(build_deep_prompt(user_data, aicu_data))

    return "\n".join(parts)
