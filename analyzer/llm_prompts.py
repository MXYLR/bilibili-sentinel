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
        "signals": ["注册时间批次集中", "等级低 (Lv0-Lv2)", "无头像/默认头像", "用户名乱码(bili_开头)/默认名",
                   "无动态 + 无投稿 = 账号骨架为空", "默认签名(这个人没有填简介啊~~~)"],
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

SYSTEM_PROMPT = """你是B站水军识别引擎的语义分析层。引擎已对13个维度完成量化评分(0-1)，你的任务是**结合引擎分数+评论内容语义+用户画像**做最终判定。

## 核心判断原则

1. **谨慎判断，避免误报**：引擎分数是参考，不是判决。分数高但评论正常 → 应判为正常或低置信度
2. **内容为主，分数为辅**：评论内容自然、有个人观点、互动真实 → 即使引擎分高也要慎重
3. **证据充分再判定**：必须有明确的水军特征（模板化、引流、引战、账号异常）才能判为水军
4. **reasoning必须具体**：必须引用具体特征值+评论原文片段，不能只说分数高

## 13维特征解读指南（重要！）

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

- **f1_account_age (0.08)**：账号年龄
  * <30天 → 新号可疑
  * >1年 → 正常

- **f4_avatar_verify (0.08)**：头像/认证
  * 无头像+无认证 → 轻度可疑

### 中权重 (0.04-0.06)
- **f2_follow_ratio (0.06)**：粉丝/关注比异常
- **f8_like_ratio (0.06)**：评论零赞 → 无人认同
- **f15_commercial_spam (0.04)**：含赌博/色情/联系方式

### 低权重 (w≤0.03)
- **f14_sensitive_content (0.03)**：动态含敏感词
- **f18_signature_troll (0.03)**：签名引战
- **f7_sentiment_extreme (0.02)**：情感极端
- **f16_time_regularity (0.02)**：时间规律（机器人）

## 8种水军类型定义

1. **模板刷评型(type 1)**：评论内容高度雷同，明显复制粘贴
2. **情绪引导型(type 2)**：刻意煽动情绪、带节奏
3. **AI生成型(type 3)**：评论语句不通、逻辑断裂、AI味重
4. **引流广告型(type 4)**：含联系方式、推广信息
5. **批量操控型(type 5)**：多账号同时间集中发评
6. **黑产养号型(type 6)**：f12≥0.8（4-5/5命中）+ 其他可疑特征
7. **对立引战型(type 7)**：刻意制造对立、激怒他人
8. **敏感内容型(type 8)**：动态含女拳/政治/造谣内容

## 判定流程（按顺序判断）

1. **明确水军**：f12≥0.8（4-5/5命中）+ 评论有异常 → type 6, confidence 85-95
2. **引流广告**：f15≥0.3 或 评论含联系方式 → type 4, confidence 80-95
3. **敏感内容**：f14≥0.3 或 动态有敏感词 → type 8, confidence 90-100
4. **模板刷评**：f5≥0.6 → type 1, confidence 70-85
5. **轻度可疑**：有2-3个特征≥0.5，但评论基本正常 → type 0, confidence 0（判正常但标注可疑）
6. **正常用户**：全特征≤0.3 且评论自然 → type 0, confidence 0

## 重要提醒

- f12=0.4（2/5命中）**不是**四无账号，不应直接判水军
- 新注册账号（f1高）但评论正常 → 可能是真实新用户
- 低等级账号（f3高）但评论有实质内容 → 可能是真实用户
- **必须有评论内容证据才能判水军，不能只看分数**

## 输出格式

每个用户的分析结果必须包含 detailed reasoning（150-200字），结构如下：

**reasoning 写作框架（必须遵循）：**
1. **【特征值解读】**（约40字）逐条列出高分特征的含义，如"f12=0.4表示命中2/5项（无头像、无动态），属于轻度可疑，不是四无账号"
2. **【评论内容分析】**（约60字）引用1-2条评论原文片段，分析是否有模板化/引流/引战等实质性证据
3. **【综合判定逻辑】**（约50字）说明最终判定的依据，解释为什么判为水军/正常/可疑
4. **【风险说明】**（约30字）如判为正常，说明可能存在的风险点；如判为水军，说明置信度依据

**reasoning 质量要求：**
- 必须 150-200 字，不能少于120字
- 必须引用具体特征值（如 f12=0.4）并解释其含义
- 必须引用评论原文片段（用引号括起来）
- 不能只说"分数高"或"疑似水军"，必须给出具体分析过程
- 如判定为正常用户，必须说明"为什么不是水军"

示例（约180字）：
reasoning: "该用户引擎评分0.65，主要贡献来自f12=0.4（账号骨架轻度可疑，命中2/5项：无头像、无动态，但ID正常且有投稿，不是四无账号）和f5=0.6（评论内容相似度较高）。评论原文："这视频讲得不错"、"我也遇到过类似情况"，内容自然且有个人观点，无明显模板化特征。综合判断：账号虽有一定可疑点，但评论内容真实，判定为正常用户（type 0），建议持续关注。"
"""

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

        # 只取前 10 条评论用于分析，过滤图片URL，防止 LLM API 报 image_url 错误
        _SKIP_PREFIXES = ("http://", "https://", "//", "data:")
        comment_texts = []
        for c in comments[:10]:
            if isinstance(c, str):
                t = c
            else:
                t = c.get("content", c.get("message", str(c)))
            # 如果整条评论是一个 URL（图片），跳过
            t_stripped = (t or "").strip()
            if t_stripped and any(t_stripped.startswith(p) for p in _SKIP_PREFIXES):
                continue
            comment_texts.append(t[:200])
        comments_str = "\n    ".join(f"[{j+1}] {t}" for j, t in enumerate(comment_texts))

        # v2.16: 个性签名加入分析
        sign = user.get("sign", "")
        sign_line = f"- 个性签名: {sign[:100]}\n" if sign else ""

        prompt += f"""### 用户 {i}
- MID: {user.get('mid', 'unknown')}
- 用户名: {user.get('uname', 'unknown')}
- 等级: Lv{user.get('level', 0)}
- 评论数(此视频): {len(comments)}{sign_line}
{user.get('raw_profile', '')}
- ⚠️ 引擎综合可疑分: {user.get('suspicious_score', 0):.2f} / 1.0 {'(高度可疑!!)' if user.get('suspicious_score', 0) >= 0.5 else '(中度可疑)' if user.get('suspicious_score', 0) >= 0.3 else '(低可疑)'}
- 13维特征(0-1):
  ·高: f12_骨架={features.get('f12_account_skeleton', 0):.2f} f3_等级={features.get('f3_level_score', 0):.2f} f5_雷同={features.get('f5_content_similarity', 0):.2f} f6_爆发={features.get('f6_time_burst', 0):.2f} f1_年龄={features.get('f1_account_age', 0):.2f} f4_头像={features.get('f4_avatar_verify', 0):.2f}
  ·中: f2_粉关={features.get('f2_follow_ratio', 0):.2f} f8_赞比={features.get('f8_like_ratio', 0):.2f} f15_引流={features.get('f15_commercial_spam', 0):.2f}
  ·低: f14_敏感={features.get('f14_sensitive_content', 0):.2f} f18_签名={features.get('f18_signature_troll', 0):.2f} f7_情感={features.get('f7_sentiment_extreme', 0):.2f} f16_规律={features.get('f16_time_regularity', 0):.2f}
- 评论内容:
    {comments_str if comments_str else '(无评论)'}

"""

    prompt += """请分析以上用户，输出 JSON。

**reasoning 字段要求（重要）：**
1. 长度 150-200 字，少于120字视为不合格
2. 必须包含【特征值解读】【评论内容分析】【综合判定逻辑】【风险说明】四个部分
3. 必须引用至少1条评论原文（用中文引号括起来）
4. 必须逐条解释每个高分特征（f值≥0.4）的含义，不能只写分数
5. 如判定为正常用户，必须明确说明"为什么不是水军"

只输出 JSON，不要输出其他内容。"""
    return prompt


def build_single_user_prompt(user_data: dict) -> str:
    """构建单用户分析 Prompt（轻量级，含判定建议）。"""
    score = user_data.get('suspicious_score', 0)
    features = user_data.get('features', {})
    f12 = features.get('f12_account_skeleton', 0)

    # ★ 高分段建议指令（不是强制）
    hint = ""
    if score >= 0.8:
        hint = "\n### 📊 引擎评分较高({:.0f}/100)，建议重点关注以下特征：\n".format(score * 100)
        hint += "- 请仔细检查评论内容是否有模板化、引流、引战等实质性证据\n"
        hint += "- 账号骨架f12={:.2f}，请确认命中了几项（5项全中=1.0，2项命中=0.4）\n".format(f12)
        hint += "- 即使分数高，如果评论内容自然真实，也应判为正常用户\n"
    elif f12 >= 0.6:
        hint = "\n### 📊 账号骨架f12={:.2f}（3-4/5命中），建议仔细检查：\n".format(f12)
        hint += "- f12=0.6 表示命中3/5项，不是'四无账号'，需结合评论内容判断\n"
        hint += "- 如果评论内容自然有实质，可能是真实用户\n"

    prompt = build_user_prompt([user_data])
    if hint:
        # 插入到提示末尾前
        prompt = prompt.replace("请分析以上用户", hint + "\n请分析以上用户")
    return prompt


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
