"""
B站水军 LLM 分析 — 提示词模板

定义 7 种水军类型及结构化分析提示词。
"""


import sys
from pathlib import Path

# 导入智能压缩器
try:
    from .text_compressor import compress_comments_for_prompt, compress_user_profile
except ImportError:
    # 独立运行时的兜底
    sys.path.insert(0, str(Path(__file__).parent))
    from text_compressor import compress_comments_for_prompt, compress_user_profile

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

SYSTEM_PROMPT = """你是B站水军识别引擎的语义分析层。引擎已完成13维量化评分，你需结合分数+评论语义+用户画像做最终判定。

## 原则
1. 谨慎判断，避免误报。分数高但评论正常→判正常
2. 内容为主，分数为辅。必须有评论证据才能判水军
3. 必须引用特征值+评论原文，不能只说"分数高"

## 关键特征速查
- f12 账号骨架: 1.0=5/5全中, 0.8=4/5, 0.6=3/5, 0.4=2/5(轻度), 0.2=1/5。**f12=0.4不是四无号**
- f5 内容雷同: ≥0.6→模板刷评  f6 时间爆发: ≥0.7→批量操控
- f15 商业引流: ≥0.3→广告  f14 敏感内容: ≥0.3→敏感
- f1 新号(<30天), f3 低等级(Lv0-2)+高活跃, f4 无头像→轻度可疑

## 8类型
1.模板刷评 2.情绪引导 3.AI生成 4.引流广告 5.批量操控 6.黑产养号(f12≥0.8) 7.对立引战 8.敏感内容

## 判定
- f12≥0.8+评论异常→type6,conf85-95 | f15≥0.3→type4 | f14≥0.3→type8
- f5≥0.6→type1,conf70-85 | 2-3特征≥0.5但评论正常→type0,conf0(可疑)
- type0=正常用户, type1-8=水军

## 输出格式
每个用户输出 JSON。reasoning 80-120字，含特征解读+1条评论原文引用+判定逻辑。

示例 reasoning(90字): "f12=0.4命中2/5项(无头像、无动态)，非四无号。评论'讲的还行吧'自然有观点，无模板化。虽有轻度骨架可疑但内容真实，判为正常(type0)。"
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

    prompt = f"""请分析以下 B站评论用户，判断是否属于水军（类型定义见系统提示）。

## 用户数据

"""

    for i, user in enumerate(users_data, 1):
        features = user.get("features", {})
        comments = user.get("comments", [])

        # v2.29: 智能压缩评论
        comments_summary = compress_comments_for_prompt(comments, max_examples=3)
        
        # v2.29: 只展示 f≥0.3 的特征（精简一行）
        active_features = []
        feature_names = {
            "f1_account_age": "f1年龄", "f2_follow_ratio": "f2粉关", "f3_level_score": "f3等级",
            "f4_avatar_verify": "f4头像", "f5_content_similarity": "f5雷同", "f6_time_burst": "f6爆发",
            "f7_sentiment_extreme": "f7情感", "f8_like_ratio": "f8赞比",
            "f12_account_skeleton": "f12骨架", "f14_sensitive_content": "f14敏感",
            "f15_commercial_spam": "f15引流", "f16_time_regularity": "f16规律",
            "f18_signature_troll": "f18签名",
        }
        for fk, fshort in feature_names.items():
            fv = features.get(fk, 0)
            if fv >= 0.3:
                active_features.append(f"{fshort}={fv:.2f}")
        feat_line = "、".join(active_features) if active_features else "无显著异常特征"
        
        # v2.29: 压缩用户画像
        raw_profile = compress_user_profile(user.get('raw_profile', ''), max_chars=200)

        score = user.get('suspicious_score', 0)
        risk_tag = "高危" if score >= 0.5 else ("中危" if score >= 0.3 else "低危")
        
        sign = user.get("sign", "")
        sign_txt = f" 签名:{sign[:30]}" if sign and sign != "这个人没有填简介啊~~~" else ""
        
        prompt += f"""### 用户{i} MID:{user.get('mid','?')} {user.get('uname','?')} Lv{user.get('level',0)} score={score:.2f}({risk_tag}){sign_txt}
{raw_profile}
特征(f≥0.3): {feat_line}
评论: {comments_summary if comments_summary else '(无)'}

"""

    prompt += """请分析以上用户，只输出 JSON（不要markdown代码块）。

输出格式（每个用户一个对象，放在 results 数组中）：
{"results": [{"mid": 123456, "type_id": 0, "type_name": "正常用户", "confidence": 0, "reasoning": "f12=0.4命中2/5项非四无号，评论'讲得还行'自然，判正常(type0)"}]}
字段说明: type_id(0=正常 1-8=水军), type_name(中文类型名), confidence(0-100整数), reasoning(80-120字分析)。
只输出 JSON，不要其他内容。"""
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
    支持两种格式：
      1. {"results": [{"mid":..., ...}, ...]}  (批量)
      2. {"mid":..., "type_id":..., ...}       (单用户直接对象)

    Args:
        response_text: LLM 原始返回文本

    Returns:
        [{"mid": ..., "type_id": ..., "type_name": ..., "confidence": ..., "reasoning": ...}, ...]
    """
    import json
    import re

    if not response_text or not response_text.strip():
        return []

    text = response_text.strip()

    def _try_parse(s: str):
        """尝试解析字符串，返回 (data, success)"""
        try:
            d = json.loads(s)
            return d, True
        except json.JSONDecodeError:
            return None, False

    def _extract_results(data):
        """从解析后的数据中提取结果列表"""
        if isinstance(data, dict):
            if "results" in data:
                r = data.get("results", [])
                return r if isinstance(r, list) else []
            if "mid" in data:
                return [data]
        if isinstance(data, list):
            return data
        return []

    # 策略1: 直接解析整个文本
    data, ok = _try_parse(text)
    if ok:
        return _extract_results(data)

    # 策略2: 提取 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*\})\s*```', text, re.DOTALL)
    if m:
        data, ok = _try_parse(m.group(1))
        if ok:
            return _extract_results(data)

    # 策略3: 提取最外层 {} 块（贪婪匹配，解决嵌套JSON问题）
    # 非贪婪 *? 会在第一个 } 停止，导致嵌套JSON截断
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        data, ok = _try_parse(m.group(0))
        if ok:
            return _extract_results(data)

    return []
