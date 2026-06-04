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

SYSTEM_PROMPT = """你是一个专业的B站水军识别分析师。你的核心任务是从评论用户中找出水军账号，而非判定正常。请默认保持怀疑态度。

## 判责原则

- 引擎已对用户做了多维度特征评分（0-1），引擎评分 ≥ 0.3 意味着至少存在2个以上可疑信号
- 你的任务是结合引擎特征 + 评论内容 + 用户画像，做出最终判定
- **宁可误判可疑，不可漏判水军**：低分用户可能是正常，高分用户必须给出判定理由
- 特征分 > 0.4 的项目必须作为怀疑依据，不可忽略

## 8 种水军类型（按危害排序）

1. **引流广告型（type 4）**：含微信号/QQ/链接/加群/私信/课程推广。评论再正常，有联系方式就是广告。引擎 f15_commercial_spam > 0.3 即触发
2. **黑产养号型（type 6）**：批量注册+低等级+无头像+默认名(bili_)+无动态+无投稿+默认签名。四无账号直接判 type 6，confidence ≥ 90
3. **敏感内容型（type 8）**：历史动态含女拳/打拳/蝈蝻/以色列/乌克兰/造谣/抹黑/境外势力。引擎 f14 > 0.3 即触发
4. **批量操控型（type 5）**：多号同步操作，时间爆发度 > 0.7，行为模式一致
5. **模板化刷评型（type 1）**：同视频多次评论内容雷同，或无意义灌水刷评
6. **情绪引导型（type 2）**：极端情绪化表达，带节奏/站队/制造焦虑
7. **AI生成型（type 3）**：语句不通/逻辑怪异/中英混杂不自然
8. **对立引战型（type 7）**：刻意制造二元对立，激化评论区矛盾

## 判定规则

- 评论内容含联系方式/推广 → type 4, confidence ≥ 85
- 无头像+默认名(bili_开头/纯数字)+无投稿 → type 6, confidence ≥ 90
- 动态中含政治/女拳敏感词 → type 8, confidence ≥ 95
- f12_account_skeleton > 0.5 → type 6（账号骨架为空）
- f5_content_similarity > 0.5 + 多条评论雷同 → type 1
- f6_time_burst > 0.7 → type 5
- f7_sentiment_extreme > 0.3 + 情绪化评论 → type 2
- 引擎总分 suspicious_score ≥ 0.5 的用户绝对不能判为正常，必须给出水军类型判定
- 如果多条特征 > 0.3 但无明确类型，选最匹配的类型，confidence 40-60
- 只有特征全 ≤ 0.2 且评论内容正常，才判为 type 0

## 用户名乱码判定

仅当用户名为 bili_ 开头的默认名、纯数字串、无意义随机字母数字组合才算乱码。中日韩文字(汉字/假名/谚文)具有自然语义，不算乱码。如"扒饭の骇灵"是有意义的日文混合名 ≠ 乱码。

## 输出格式

严格输出 JSON，不要额外文字：
```json
{
  "results": [
    {
      "mid": 用户ID,
      "type_id": 0-8,
      "type_name": "类型名称",
      "confidence": 0-100,
      "reasoning": "推理过程(200字内，引用具体特征值和评论内容)"
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
- 评论数 (此视频): {len(comments)}{sign_line}
- ⚠️ 引擎综合可疑分: {user.get('suspicious_score', 0):.2f} / 1.0 {'(高度可疑)' if user.get('suspicious_score', 0) >= 0.5 else '(中度可疑)' if user.get('suspicious_score', 0) >= 0.3 else '(低可疑)'}
{user.get('raw_profile', '')}
- 特征分数 (0-1, 越高越可疑, >0.3即信号):
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
  * 账号骨架: {features.get('f12_account_skeleton', 0):.2f} (无头像+用户名乱码+无动态+无投稿+默认签名)
  * 转发模式: {features.get('f13_lottery_repost', 0):.2f} (动态中以转发为主，分抽奖/投票/纯转发)
  * 敏感内容: {features.get('f14_sensitive_content', 0):.2f} (女拳/以乌/造谣)
  * 商业引流: {features.get('f15_commercial_spam', 0):.2f} (赌博/色情/加微信等硬广告)
  * 时间规律: {features.get('f16_time_regularity', 0):.2f} (评论间隔高度固定)
  * 自评相似: {features.get('f17_self_similarity', 0):.2f} (多条评论雷同)
  * 签名引战: {features.get('f18_signature_troll', 0):.2f} (签名含挑衅/嘲讽话术)
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
