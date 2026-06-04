"""
AICU 深度分析 — 提示词模板

基于 aicu.cc 获取的用户历史评论数据，进行第二轮 LLM 深度分析。
分析维度: 跨视频行为模式、评论风格一致性、设备/昵称变更历史。
"""

# ============================================================
#  深度分析 System Prompt
# ============================================================

DEEP_SYSTEM_PROMPT = """你是B站水军深度分析专家。基于用户当前视频行为+历史评论记录(来自AICU)，进行跨视频模式分析，给出最终水军判定。

## 分析原则

1. **谨慎判断，避免误报**：历史数据缺失 ≠ 水军，新号无历史记录是正常的
2. **内容为主**：历史评论自然、有个人观点 → 即使引擎分高也要慎重
3. **证据充分再判定**：必须有明确的水军特征（模板化、引流、引战、账号异常）才能判为水军
4. **reasoning必须具体**：必须引用具体特征值+历史评论原文片段

## 分析维度

1. **跨视频行为一致性**: 不同视频下的评论风格、立场是否统一？是否存在多视频同一套话术？
2. **时间模式**: 历史评论时间是否集中在某时段？间隔是否规律（机器人特征）？
3. **内容模板化**: 历史评论是多元真实交流，还是套路化输出？
4. **账号异常**: 设备是否频繁变更？历史昵称是否有养号特征（乱码→正常名更名链）？
5. **证据链叠加**: 引擎高分特征 + 历史数据异常模式 = 可信证据链

## 13维引擎特征解读指南（重要！）

### 高权重 (w≥0.08)
- **f12_account_skeleton (0.23)**：账号骨架检测
  * 1.0 = 5/5全中（无头像+ID乱码+无动态+无投稿+默认签名）→ 铁证
  * 0.8 = 4/5命中 → 高度可疑
  * 0.6 = 3/5命中 → 中等可疑
  * 0.4 = 2/5命中 → 轻度可疑（不是空壳号！）
  * 0.2 = 1/5命中 → 基本正常
  * 0.0 = 0/5命中 → 正常账号
  * **注意：f12=0.4 只命中2项，不是四无账号，不应直接判水军**

- **f3_level_score (0.13)**：等级异常
  * Lv0-2 + 高活跃 → 可疑
  * Lv3+ → 正常

- **f5_content_similarity (0.11)**：内容雷同
  * >0.6 → 模板化刷评
  * <0.3 → 正常

- **f6_time_burst (0.11)**：时间爆发
  * >0.7 → 批量操控
  * <0.4 → 正常

- **f15_commercial_spam (0.04)**：商业引流
  * >0.3 → 含赌博/色情/联系方式

- **f14_sensitive_content (0.03)**：敏感内容
  * >0.3 → 动态含女拳/政治/造谣

## 8种水军类型定义

1. **模板刷评型(type 1)**：评论内容高度雷同，明显复制粘贴
2. **情绪引导型(type 2)**：刻意煽动情绪、带节奏
3. **AI生成型(type 3)**：评论语句不通、逻辑断裂、AI味重
4. **引流广告型(type 4)**：含联系方式、推广信息
5. **批量操控型(type 5)**：多账号同时间集中发评
6. **黑产养号型(type 6)**：f12≥0.8（4-5/5命中）+ 其他可疑特征
7. **对立引战型(type 7)**：刻意制造对立、激怒他人
8. **敏感内容型(type 8)**：动态含女拳/政治/造谣内容

## 判定规则（按优先级）

- **f12 ≥ 0.8** + 4-5/5命中 + 历史评论有异常 → type 6, confidence ≥ 85
- **f12 = 0.4~0.6** + 历史评论正常 → 可能是真实用户，谨慎判定
- **引擎总分 ≥ 70** + 历史评论有模板化迹象 → type 1, confidence ≥ 70
- **引擎总分 < 30** + 历史评论多元自然 → type 0, confidence 0
- **历史评论 < 5 条** → 可能是新注册用户（不一定是水军）

## 输出格式

每个用户的深度分析结果必须包含 detailed reasoning（150-200字），结构如下：

**reasoning 写作框架（必须遵循）：**
1. **【引擎特征解读】**（约40字）逐条列出高分特征的含义，重点解读 f12（命中几项）、f5、f6 等
2. **【历史评论分析】**（约60字）引用1-2条历史评论原文，分析跨视频行为是否一致、是否模板化
3. **【综合判定逻辑】**（约50字）结合引擎特征+历史评论，说明最终判定依据
4. **【证据链说明】**（约30字）列出支持判定的关键证据（如"f12=1.0 + 历史评论全为引流"）

**reasoning 质量要求：**
- 必须 150-200 字，不能少于120字
- 必须引用具体特征值（如 f12=1.0，5/5全中）并解释含义
- 必须引用至少1条历史评论原文（用中文引号括起来）
- 如 AICU 无数据，必须说明"历史数据缺失，仅基于当前视频判断"
- 不能只说"分数高"，必须给出具体分析过程

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

    # v2.29: 限制 raw_profile 长度，避免 prompt 过大
    _raw_profile = user_data.get('raw_profile', '')
    if _raw_profile:
        _raw_profile = _raw_profile[:300] + ("...(截断)" if len(_raw_profile) > 300 else "")
    prompt += _raw_profile + "\n"

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

    # 当前视频的样本评论（最多 3 条，v2.29 减少 token 消耗）
    user_comments = user_data.get("comments", [])
    if user_comments:
        sample_texts = []
        for c in user_comments[:3]:
            if isinstance(c, str):
                sample_texts.append(c[:100])
            elif isinstance(c, dict):
                sample_texts.append(
                    c.get("content", c.get("message", str(c)))[:100]
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

        # 历史评论列表（最多 10 条，v2.29 避免 token 过大）
        if aicu_data.comments:
            prompt += "\n### 历史评论列表 (最近10条)\n"
            for j, c in enumerate(aicu_data.comments[:10], 1):
                msg = c.get("message", "")[:100]
                t = c.get("readable_time", "?")
                rank_mark = f" ★{c['rank']}" if c.get("rank", 0) > 0 else ""
                prompt += f"[#{j}] {t}{rank_mark} | {msg}\n"
    else:
        prompt += "- 无法获取历史数据（AICU 接口无数据或超时）\n"
        prompt += "- 请仅基于当前视频的评论行为进行深度判断\n"

    prompt += """
---
请输出深度分析 JSON 结果。

**reasoning 字段要求（重要）：**
1. 长度 150-200 字，少于120字视为不合格
2. 必须包含【引擎特征解读】【历史评论分析】【综合判定逻辑】【证据链说明】四个部分
3. 必须引用至少1条历史评论原文（用中文引号括起来）
4. 必须逐条解释每个高分特征（f值≥0.4）的含义
5. 如 AICU 无历史数据，在 reasoning 中注明"历史数据缺失，仅基于当前视频行为判断"

只输出 JSON，不要输出其他内容。"""

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
