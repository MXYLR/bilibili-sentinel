"""
AICU 深度分析 — 提示词模板

基于 aicu.cc 获取的用户历史评论数据，进行第二轮 LLM 深度分析。
分析维度: 跨视频行为模式、评论风格一致性、设备/昵称变更历史。
"""

import sys
from pathlib import Path

# 导入智能压缩器
try:
    from .text_compressor import compress_comments_for_prompt as compress_fn
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from text_compressor import compress_comments_for_prompt as compress_fn


# ============================================================
#  深度分析 System Prompt
# ============================================================

DEEP_SYSTEM_PROMPT = """你是B站水军深度分析专家。基于当前视频行为+历史评论(来自AICU)，进行跨视频模式分析，做最终判定。

## 原则
   1. 谨慎判断，避免误报。历史数据缺失≠水军，新号无历史正常
   2. 内容为王，但非绝对。无评论时，账号特征本身可作判定依据
   3. 必须引用特征值+证据，不能只说"分数高"

## 无评论时的判定规则（重要！）
- F12账号骨骼≥0.6（3/5命中）→ 可判type6，即使无评论
- F14敏感内容≥0.3 → 可判type8，即使无评论
- F12≥0.4（2/5）+ F14≥0.3 → 可判type8，骨骼+敏感双重证据
- 仅F12=0.2（1/5）或仅F1新号 → 无评论时判type0（证据不足）
- 有评论时：以评论内容为主，特征为辅

## 分析维度
跨视频行为一致性、时间模式、内容模板化、账号异常(设备/昵称变更)、证据链叠加

## 关键特征速查
- f12 账号骨骼: 1.0=5/5全中, 0.8=4/5, 0.6=3/5, 0.4=2/5, 0.2=1/5
  - 5项: 无头像+默认名(bili_开头/乱码)+无动态+无投稿+默认签名
  - f12≥0.6（3/5）= 骨骼号，本身是水军强证据
- f14 敏感内容: ≥0.3=历史动态含敏感内容，职业水军强证据
- f5 内容雷同: ≥0.6→模板刷评  f6 时间爆发: ≥0.7→批量操控
- f15 商业引流: ≥0.3→广告

## 8类型
1模板刷评 2情绪引导 3AI生成 4引流广告 5批量操控 6黑产养号(f12≥0.6) 7对立引战 8敏感内容(f14≥0.3)

## 判定
- f12≥0.6 + 历史异常 → type6, conf≥85 | f14≥0.3 → type8, conf≥85
- f12≥0.4 + f14≥0.3 → type8, conf75-90（双重证据）
- 引擎≥70 + 历史模板化 → type1 | 引擎<30 + 历史多元 → type0
- type0=正常用户, type1-8=水军

## 输出
每个用户输出JSON。reasoning 80-120字，含特征解读+证据+判定逻辑。AICU无数据时注明"历史缺失"。

示例1(有评论): "f12=1.0全中，历史评论'加我薇信xxx'持续引流。跨3个视频同一话术，确认引流广告水军(type4,conf90)。"
示例2(无评论): "f12=0.4命中2/5，非完整骨骼。但f14=100历史动态含敏感内容，判type8敏感内容(conf85)。无评论时f14本身是强力证据。"
"""


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

    prompt = f"""## 深度分析: {user_data.get('uname', '?')} (MID:{mid})

**当前视频:** Lv{user_data.get('level', 0)} 评论{user_data.get('comment_count', 0)}条 引擎分{user_data.get('suspicious_score', 0):.1f}/100 初筛:type{user_data.get('llm_type_id', 0)}({user_data.get('llm_type_name', '?')}) conf{user_data.get('llm_confidence', 0)}%"""

    # v2.29: 压缩 raw_profile
    _raw_profile = user_data.get('raw_profile', '')
    if _raw_profile:
        _raw_profile = _raw_profile[:200] + ("…" if len(_raw_profile) > 200 else "")
        prompt += f"\n{_raw_profile}"

    # v2.29: 特征一行展示（仅f≥0.3）
    active_features = []
    fnames = {"f1_account_age":"f1年龄","f2_follow_ratio":"f2粉关","f3_level_score":"f3等级",
              "f4_avatar_verify":"f4头像","f5_content_similarity":"f5雷同","f6_time_burst":"f6爆发",
              "f7_sentiment_extreme":"f7情感","f8_like_ratio":"f8赞比",
              "f12_account_skeleton":"f12骨架","f14_sensitive_content":"f14敏感",
              "f15_commercial_spam":"f15引流","f16_time_regularity":"f16规律","f18_signature_troll":"f18签名"}
    for fk, fs in fnames.items():
        fv = features.get(fk, 0)
        if fv >= 0.3:
            active_features.append(f"{fs}={fv:.2f}")
    feat_line = "、".join(active_features) if active_features else "无显著异常"
    prompt += f"\n特征(f≥0.3): {feat_line}"

    # F12 骨架警告
    f12_val = features.get("f12_account_skeleton", 0)
    if f12_val >= 0.40:
        prompt += f"\n⚠️ F12骨架={f12_val:.2f}({int(f12_val*5)}/5命中)"

    # v2.29: 当前视频评论（智能压缩）
    user_comments = user_data.get("comments", [])
    if user_comments:
        comments_summary = compress_fn(user_comments, max_examples=2)
        prompt += f"\n当前评论: {comments_summary}"
    
    # AICU 历史数据
    if aicu_data and aicu_data.fetch_ok:
        parts = [f"\n**AICU历史:** {aicu_data.comment_count}条评论"]
        if aicu_data.active_hour:
            parts.append(f"活跃{aicu_data.active_hour}点")
        if aicu_data.device_name:
            parts.append(f"设备:{aicu_data.device_name}")
        if aicu_data.history_names:
            parts.append(f"昵称:{','.join(aicu_data.history_names[:3])}")
        prompt += "\n" + " ".join(parts)

        # v2.29: 历史评论（智能压缩，max_examples=3）
        if aicu_data.comments:
            history_summary = compress_fn(aicu_data.comments, max_examples=3)
            prompt += f"\n{history_summary}"
    else:
        prompt += "\n**AICU:** 无历史数据"

    prompt += """\n\n请输出深度分析 JSON。reasoning 80-120字，含特征解读+历史评论原文+判定逻辑。

输出格式:
{"results": [{"mid": 123456, "deep_type_id": 0, "deep_type_name": "正常用户", "deep_confidence": 0, "deep_reasoning": "f12=0.8命中4/5项，历史评论引流，判黑产养号(type6)"}]}
字段说明: deep_type_id(0=正常 1-8=水军), deep_type_name(中文类型名), deep_confidence(0-100整数), deep_reasoning(80-120字)。
只输出JSON。"""

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
