"""
B站水军 LLM 分析 — 提示词模板

定义 7 种水军类型及结构化分析提示词。
"""

# ============================================================
#  7 大水军类型定义
# ============================================================

WATER_ARMY_TYPES = {
    1: {
        "name": "模板化刷评型",
        "description": "使用相同或高度相似的文案在多个视频下批量刷评，评论内容空洞/无实质性观点",
        "signals": ["多评论内容相似度 > 80%", "评论时间高度集中", "评论内容模板化、机械化"],
    },
    2: {
        "name": "情绪引导型",
        "description": "刻意煽动情绪、带节奏，通过极端化表达影响舆论走向",
        "signals": ["情感极值偏离正常", "使用大量感叹号/问号", "刻意制造对立/焦虑/愤怒"],
    },
    3: {
        "name": "AI生成型",
        "description": "评论内容由AI工具生成，表现为逻辑怪异/语句不通/中英混杂/格式异常",
        "signals": ["句式结构异常", "上下文逻辑断裂", "存在AI生成特征词", "中英文混用不自然"],
    },
    4: {
        "name": "引流广告型",
        "description": "评论中包含微信号/QQ号/链接/推广信息，或引导用户私信/添加联系方式",
        "signals": ["含联系方式", "引导私信/加群", "竞品/课程推广", "频繁使用特定关键词"],
    },
    5: {
        "name": "批量操控型",
        "description": "多个账号在同一时间窗口内集中发布评论，行为模式高度一致，疑似同一人/组织操控",
        "signals": ["时间爆发度 > 0.7", "评论间隔 < 30秒", "多个账号行为模式相同", "粉丝/关注比异常"],
    },
    6: {
        "name": "黑产养号型",
        "description": "账号注册时间集中/相近，等级低但评论频率高，疑似批量注册后养号",
        "signals": ["注册时间批次集中", "等级低 (Lv0-Lv2)", "无头像/默认头像", "ID乱码/默认名",
                   "无动态 + 无投稿 = 账号骨架为空"],
    },
    7: {
        "name": "对立引战型",
        "description": "刻意制造二元对立观点，激化评论区矛盾以提升互动量",
        "signals": ["使用二元对立词汇", "刻意贬低/抬高某群体", "频繁与其他用户争吵", "评论回复量异常高"],
    },
    8: {
        "name": "敏感内容型",
        "description": "历史动态中含女拳极端言论、国际政治立场宣导或造谣抹黑类内容，此类账号为职业水军号",
        "signals": ["含女拳/打拳/蝈蝻等极端性别词汇", "含以色列/乌克兰/俄乌等时政内容",
                   "含造谣/抹黑/带节奏/境外势力等词汇", "动态内容与评论高度政治化"],
    },
}

# ============================================================
#  System Prompt
# ============================================================

SYSTEM_PROMPT = """你是一个专业的B站水军识别分析师。你的任务是分析评论用户的行为特征，判断其是否为水军账号。

## 8 种水军类型

1. **模板化刷评型**：批量使用相似文案，内容空洞无实质观点
2. **情绪引导型**：刻意煽动情绪、带节奏，极端化表达
3. **AI生成型**：内容由AI生成，逻辑怪异、语句不通
4. **引流广告型**：含联系方式、推广信息
5. **批量操控型**：多账号同步操作，行为模式一致
6. **黑产养号型**：批量注册、低等级高活跃、无头像+ID乱码+无动态+无投稿（账号骨架完全为空）
7. **对立引战型**：制造二元对立，激化矛盾
8. **敏感内容型**：动态含女拳极端言论/以色列乌克兰等时政/造谣抹黑，此类账号100%为职业水军

## 分析要求

- 每个用户给出分析：最可能的类型 (type_id 1-8)，置信度 (0-100)，200字以内推理
- 如果用户行为正常，type_id 设为 0，confidence 设为 0，reasoning 说明正常原因
- 注意：一个用户可能同时符合多种类型，请选择最匹配的一种
- 如果看到无头像+ID乱码+无动态+无投稿的"四无账号"，直接判定为 type 6 且 confidence ≥ 90
- 如果历史动态中出现女拳/以乌/造谣内容，直接判定为 type 8 且 confidence ≥ 95
- 请严格按 JSON 格式输出

输出格式：
```json
{
  "results": [
    {
      "mid": 用户ID,
      "type_id": 水军类型编号(0-8),
      "type_name": "类型名称",
      "confidence": 置信度(0-100),
      "reasoning": "推理过程(200字内)"
    }
  ]
}
```"""

# ============================================================
#  User Prompt Builder
# ============================================================

def build_user_prompt(users_data: list) -> str:
    """
    构建用户分析 Prompt。

    Args:
        users_data: [{"mid": ..., "uname": ..., "level": ..., "comments": [...], "features": {...}}, ...]

    Returns:
        结构化的 prompt 字符串
    """
    if not users_data:
        return ""

    # 简要说明水军类型
    type_desc = "\n".join(
        f"  {tid}. {info['name']}: {info['description']}"
        for tid, info in WATER_ARMY_TYPES.items()
    )

    prompt = f"""请分析以下 B站评论用户，判断他们是否属于水军账号。

## 背景
- 视频: B站评论区
- 水军类型参考:
{type_desc}

## 用户数据

"""

    for i, user in enumerate(users_data, 1):
        features = user.get("features", {})
        comments = user.get("comments", [])

        # 只取前 10 条评论用于分析
        comment_texts = [
            (c if isinstance(c, str) else c.get("content", c.get("message", str(c))))
            for c in comments[:10]
        ]
        comments_str = "\n    ".join(f"[{j+1}] {t[:200]}" for j, t in enumerate(comment_texts))

        prompt += f"""### 用户 {i}
- MID: {user.get('mid', 'unknown')}
- 用户名: {user.get('uname', 'unknown')}
- 等级: Lv{user.get('level', 0)}
- 评论数 (此视频): {len(comments)}
- 特征分数 (0-1, 越高越可疑):
  * 账号年龄: {features.get('f1_account_age', 0):.2f}
  * 粉丝/关注比: {features.get('f2_follow_ratio', 0):.2f}
  * 等级分数: {features.get('f3_level_score', 0):.2f}
  * 头像/认证: {features.get('f4_avatar_verify', 0):.2f}
  * 内容相似度: {features.get('f5_content_similarity', 0):.2f}
  * 时间爆发: {features.get('f6_time_burst', 0):.2f}
  * 情感极端: {features.get('f7_sentiment_extreme', 0):.2f}
  * 赞评比: {features.get('f8_like_ratio', 0):.2f}
  * 批量注册: {features.get('f9_registration_batch', 0):.2f}
  * 互动圈子: {features.get('f10_interaction_ring', 0):.2f}
  * VIP异常: {features.get('f11_vip_anomaly', 0):.2f}
  * 账号骨架: {features.get('f12_account_skeleton', 0):.2f} (无头像+ID乱码+无动态+无投稿)
  * 转发抽奖: {features.get('f13_lottery_repost', 0):.2f} (无投稿+全转发抽奖)
  * 敏感内容: {features.get('f14_sensitive_content', 0):.2f} (女拳/以乌/造谣)
  * 综合异常分: {features.get('综合异常分', user.get('suspicious_score', 0)):.2f}
- 评论内容:
    {comments_str if comments_str else '(无评论)'}

"""

    prompt += """请分析以上用户，输出 JSON 格式结果。只输出 JSON，不要输出其他内容。"""
    return prompt


def build_single_user_prompt(user_data: dict) -> str:
    """构建单用户分析 Prompt（轻量级）"""
    return build_user_prompt([user_data])


def parse_llm_response(response_text: str) -> list:
    """
    解析 LLM 返回的 JSON。

    Args:
        response_text: LLM 原始返回文本

    Returns:
        [{"mid": ..., "type_id": ..., "type_name": ..., "confidence": ..., "reasoning": ...}, ...]
    """
    import json
    import re

    # 尝试提取 JSON 块
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
    if not json_match:
        # 尝试直接找 JSON 对象
        json_match = re.search(r'\{[\s\S]*"results"[\s\S]*\}', response_text)
        if json_match:
            text = json_match.group(0)
        else:
            # 兜底：整个响应
            text = response_text
    else:
        text = json_match.group(1)

    try:
        data = json.loads(text)
        return data.get("results", [])
    except json.JSONDecodeError:
        return []
