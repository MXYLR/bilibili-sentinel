"""
AICU 深度分析 — 提示词模板

基于 aicu.cc 获取的用户历史评论数据，进行第二轮 LLM 深度分析。
分析维度: 跨视频行为模式、评论风格一致性、设备/昵称变更历史。
"""

# ============================================================
#  深度分析 System Prompt
# ============================================================

DEEP_SYSTEM_PROMPT = """你是B站水军深度分析专家。基于用户当前视频行为+历史评论记录(来自AICU)，进行跨视频模式分析，给出最终水军判定。

## 分析维度

1. **跨视频行为一致性**: 不同视频下的评论风格、立场是否统一？是否存在"多视频同一套话术"？
2. **时间模式**: 历史评论时间是否集中在某时段？间隔是否规律（机器人特征）？
3. **内容模板化**: 历史评论是多元真实交流，还是套路化输出？
4. **账号异常**: 设备是否频繁变更？历史昵称是否有养号特征（乱码→正常名更名链）？
5. **证据链叠加**: 引擎高分特征 + 历史数据异常模式 = 可信证据链

## 13 维引擎特征映射

- **f12 账号骨架 (0.23)**：无头像+ID乱码+无动态+无投稿+默认签名。≥0.4 = 铁证
- **f3 等级 (0.13)**：Lv0-2低等级=可疑，水军养号典型等级分布
- **f5 内容雷同 (0.11)**：与他人评论高度相似→模板化
- **f6 时间爆发 (0.11)**：短窗口集中刷评→操控
- **f15 商业引流 (0.04)**：含赌博/色情/联系方式
- **f14 敏感内容 (0.03)**：女拳/政治/造谣

## 判定规则

- **f12 ≥ 0.4** → 四无账号铁证。**AICU无历史数据恰恰印证批量注册**，不得降级。type 6 或 type 5, confidence ≥ 80
- **引擎总分 ≥ 50** + 历史评论有模板化迹象 → type 1, confidence ≥ 70
- **引擎总分 < 30** + 历史评论多元自然 → type 0, confidence 0
- 历史评论 < 5 条 ≠ 正常——可能是新注册水军号

## 输出格式

```json
{
  "results": [
    {
      "mid": "用户ID",
      "deep_type_id": 0-8,
      "deep_type_name": "类型",
      "deep_confidence": 0-100,
      "deep_reasoning": "300字推理(引用具体特征值+历史评论证据)",
      "risk_confirmed": true/false,
      "key_evidence": ["证据"]
    }
  ]
}
```

8种水军类型: 1模板刷评 2情绪引导 3AI生成 4引流广告 5批量操控 6黑产养号 7对立引战 8敏感内容"""


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

**账号属性:**
- 等级: Lv{user_data.get('level', 0)}
- 签名: {user_data.get('sign', '无')[:80] if user_data.get('sign') else '无'}

**当前视频行为:**
- 在此视频中发表了 {user_data.get('comment_count', 0)} 条评论
- 引擎综合可疑分: {user_data.get('suspicious_score', 0):.1f}/100
- LLM 初筛结果: 类型{user_data.get('llm_type_id', 0)} ({user_data.get('llm_type_name', '未分析')}), 置信度{user_data.get('llm_confidence', 0)}%
"""

    prompt += user_data.get('raw_profile', '') + "\n"

    # ★ 特征评分（按权重排列13维）
    prompt += f"""
**13维特征评分 (0-1):**
  ·高权重: f12_骨架={features.get('f12_account_skeleton', 0):.2f} f3_等级={features.get('f3_level_score', 0):.2f} f5_雷同={features.get('f5_content_similarity', 0):.2f} f6_爆发={features.get('f6_time_burst', 0):.2f} f1_年龄={features.get('f1_account_age', 0):.2f} f4_头像={features.get('f4_avatar_verify', 0):.2f}
  ·中权重: f2_粉关={features.get('f2_follow_ratio', 0):.2f} f8_赞比={features.get('f8_like_ratio', 0):.2f} f15_引流={features.get('f15_commercial_spam', 0):.2f}
  ·低权重: f14_敏感={features.get('f14_sensitive_content', 0):.2f} f18_签名={features.get('f18_signature_troll', 0):.2f} f7_情感={features.get('f7_sentiment_extreme', 0):.2f} f16_规律={features.get('f16_time_regularity', 0):.2f}
"""

    # Top 3 高贡献特征
    top_features = sorted(features.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_features:
        feature_labels = {
            "f1_account_age": "账号年龄", "f2_follow_ratio": "粉丝/关注比",
            "f3_level_score": "用户等级", "f4_avatar_verify": "头像/认证",
            "f5_content_similarity": "内容相似度", "f6_time_burst": "时间爆发",
            "f7_sentiment_extreme": "情感极端", "f8_like_ratio": "赞评比",
            "f12_account_skeleton": "账号骨架", "f14_sensitive_content": "敏感内容",
            "f15_commercial_spam": "商业引流", "f16_time_regularity": "时间规律性",
            "f18_signature_troll": "签名引战",
        }
        prompt += "- 高贡献特征:\n"
        for k, v in top_features:
            label = feature_labels.get(k, k)
            prompt += f"  * {label}: {v:.2f}\n"

        # ★ F12 骨架信号显式警告
        f12_val = features.get("f12_account_skeleton", 0)
        if f12_val >= 0.40:
            prompt += f"\n⚠️ **账号骨架警告**: F12={f12_val:.2f} ({int(f12_val*5)}/5 项命中)\n"
            prompt += "四无账号(无头像/ID乱码/无动态/无投稿/默认签名) — 批量注册水军号铁证\n"
            if f12_val >= 0.60:
                prompt += "直接判定水军 confidence≥85。AICU无历史数据恰恰印证新号。\n"

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
