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

## 核心判责原则

1. **引擎优先**：f12(权重0.23)、f5(0.11)、f6(0.11)、f3(0.13) 高权重特征不可忽略
2. **内容佐证**：评论内容必须与特征分互洽——特征高但评论正常 → 降低 type 置信；特征低但评论有推广/引战词 → 酌情提级
3. **宁可误判，不可漏判**：总分≥0.5必须出水军类型，全特征≤0.2才判type 0
4. **证据引用**：reasoning 必须引用具体特征值(如"f12=0.8/f15=1.0")+评论原文片段

## 13 维特征定义（按引擎权重排序）

### 高权重 (w≥0.08)
- **f12_account_skeleton (0.23)**：五维检测——无头像+ID乱码(bili_开头/纯数字)+无动态+无投稿+默认签名。≥0.8 = 4/5触发
- **f3_level_score (0.13)**：Lv0-2低等级+高活跃=可疑，Lv5+正常用户
- **f5_content_similarity (0.11)**：同视频下与他人评论高度雷同→模板化刷评
- **f6_time_burst (0.11)**：短时间窗口内集中发表评论→批量操控
- **f1_account_age (0.08)**：注册<30天+评论多→新号水军。MID>10亿=2024后注册
- **f4_avatar_verify (0.08)**：无头像+无官方认证→"双无"账号

### 中权重 (0.04-0.06)
- **f2_follow_ratio (0.06)**：粉丝极少先关注极多→引流号行为
- **f8_like_ratio (0.06)**：评论零赞→无人认同，典型水军特征
- **f15_commercial_spam (0.04)**：评论含赌博/色情/加微信QQ/联系方式

### 低权重 (w≤0.03)
- **f14_sensitive_content (0.03)**：动态含女拳/打拳/以乌/造谣抹黑
- **f18_signature_troll (0.03)**：个性签名含挑衅/嘲讽/引战话术
- **f7_sentiment_extreme (0.02)**：100%正面或100%负面→非自然
- **f16_time_regularity (0.02)**：发评间隔高度固定→机器人规律

## 水军类型→特征映射

| type | 名称 | 关键特征 |
|------|------|---------|
| 1 | 模板刷评 | f5>0.4 |
| 2 | 情绪引导 | f7>0.3 + 评论极端化 |
| 3 | AI生成 | 评论语句不通/逻辑断裂 |
| 4 | 引流广告 | f15>0.3 |
| 5 | 批量操控 | f6>0.5 |
| 6 | 黑产养号 | f12>0.4 |
| 7 | 对立引战 | f18>0.3 |
| 8 | 敏感内容 | f14>0.3 |

## 判定流程

按高→低权重顺序审视：
1. f12>0.4 且四无成立 → type 6, confidence 85-95
2. f15>0.3 或 评论有联系方式 → type 4, confidence 80-95
3. f14>0.3 或 动态有敏感词 → type 8, confidence 90-100
4. f5>0.4 → type 1, confidence 70-85
5. 其他特征组合 → 选最匹配类型, confidence 40-70
6. 全特征≤0.2 且评论正常 → type 0, confidence 0

## 用户名乱码判定

只有 bili_ 默认名、纯数字串、无意义随机字母数字组合才算乱码。
中/日/韩文字(汉字/假名/谚文)具有自然语义，不算乱码。

## 输出格式

reasoning 字段无论判定结果如何都必须非空。即使判定为正常用户(type_id=0)，也必须详细说明原因。示例：
```json
{"results":[{"mid":"123456","type_id":6,"type_name":"黑产养号型","confidence":92,"reasoning":"f12骨架=0.80（4/5命中：无头像、bili_默认名、无投稿、默认签名），f3等级=0.65（Lv1低等级），当前视频评论'这次正常了'为模板化灌水内容。综合判定为批量注册养号账号。"},{"mid":"789","type_id":0,"type_name":"正常用户","confidence":0,"reasoning":"13维特征均≤0.2，f12=0.00(有头像有昵称)，f3=0.10(Lv5高等级)，评论'UP主视频质量很高，已三连'为正常互动内容，未检测到模板化/刷量/广告/敏感等水军信号。"}]}
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

    prompt += """请分析以上用户，输出 JSON 格式结果。reasoning 字段必须 ≥200 字且详细引用特征值与评论原文，禁止仅输出"正常用户"等短句。即使判定为正常用户，也必须逐条说明各特征为何不触发。只输出 JSON。"""
    return prompt


def build_single_user_prompt(user_data: dict) -> str:
    """构建单用户分析 Prompt（轻量级，含强判定指令）。"""
    score = user_data.get('suspicious_score', 0)
    features = user_data.get('features', {})
    f12 = features.get('f12_account_skeleton', 0)

    # ★ 高分段强制指令
    override = ""
    if score >= 0.8:
        override = "\n### ⚠️ 引擎评分极高({:.0f}/100)，必须判定为水军账号，不得判为正常用户！\n".format(score * 100)
    elif f12 >= 0.4:
        override = "\n### ⚠️ 账号骨架F12={:.2f}(四无账号)，直接判定为黑产养号型(type 6)!\n".format(f12)

    prompt = build_user_prompt([user_data])
    if override:
        # 插入到提示末尾前
        prompt = prompt.replace("请分析以上用户", override + "\n请分析以上用户")
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
