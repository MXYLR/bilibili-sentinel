# -*- coding: utf-8 -*-
"""
文本智能压缩器
- 去重（完全 + 近义）
- 提取情感分布、高频话题、模板化迹象
- 保留代表性评论示例
"""

import re
from collections import Counter
from difflib import SequenceMatcher


# ============================================================
#  公开 API
# ============================================================

def compress_comments_for_prompt(comments, max_examples=5):
    """
    将评论列表压缩为结构化文本（用于 LLM prompt）
    返回字符串，直接插入 prompt
    """
    if not comments:
        return "（无评论数据）"

    # 1) 标准化
    texts = _normalize_comments(comments)

    # 2) 完全去重
    unique_texts, unique_originals = _exact_dedup(texts, comments)

    # 3) 相似度去重（仅对小数据集）
    if len(unique_texts) <= 100:
        final = _similarity_dedup(unique_texts, unique_originals)
    else:
        final = unique_originals

    # 4) 提取特征
    sentiment = _extract_sentiment(final)
    topics = _extract_topics(final, top_n=5)
    template_info = _detect_templating(final)

    # 5) 构建输出
    lines = []
    lines.append(f"评论分析（{len(comments)}条去重后{len(final)}条）:")
    lines.append(f"- 情感分布: {_fmt_sentiment(sentiment)}")
    lines.append(f"- 高频话题: {topics}")
    if template_info["has_template"]:
        lines.append(f"- ⚠️ 模板化迹象: 有（相似度{template_info['similarity_score']:.2f}）")
        for ex in template_info["examples"]:
            lines.append(f"  示例： 「{ex}...」")
    else:
        lines.append(f"- 模板化迹象: 无（相似度{template_info['similarity_score']:.2f}）")

    # 6) 示例评论
    lines.append(f"\n- 示例评论（{min(max_examples, len(final))}条）:")
    for j, c in enumerate(final[:max_examples], 1):
        if isinstance(c, dict):
            msg = c.get("content", c.get("message", ""))
            like = c.get("like", c.get("liked", 0))
        elif isinstance(c, (list, tuple)) and len(c) >= 2:
            msg, like = c[0], c[1]
        else:
            msg, like = str(c), 0
        lines.append(f"  [{j}] {msg}（👍{like}）")

    return "\n".join(lines)


def compress_user_profile(profile_text, max_chars=300):
    """
    智能压缩用户画像文本
    - 保留关键信息（等级、粉丝数、投稿数等）
    - 截断过长部分
    """
    if not profile_text:
        return "（无用户画像数据）"

    # 提取关键信息
    key_info = []

    # 等级
    m = re.search(r"Level?\s*[:：]?\s*(\d+)", profile_text, re.IGNORECASE)
    if m:
        key_info.append(f"等级=Lv{m.group(1)}")

    # 粉丝数
    m = re.search(r"粉丝\s*[:：]?\s*(\d+)", profile_text)
    if m:
        key_info.append(f"粉丝={m.group(1)}")

    # 投稿数
    m = re.search(r"投稿\s*[:：]?\s*(\d+)", profile_text)
    if m:
        key_info.append(f"投稿={m.group(1)}")

    # 是否有头像
    if "头像" in profile_text and "无" not in profile_text:
        key_info.append("有头像")

    if key_info:
        summary = " | ".join(key_info)
        if len(summary) > max_chars:
            return summary[:max_chars] + "...(截断)"
        return summary

    # 兜底：截断
    if len(profile_text) > max_chars:
        return profile_text[:max_chars] + "...(截断)"
    return profile_text


# ============================================================
#  内部工具函数
# ============================================================

def _normalize_comments(comments):
    """将评论列表标准化为字符串列表"""
    texts = []
    for c in comments:
        if isinstance(c, dict):
            t = c.get("content", c.get("message", ""))
        elif isinstance(c, (list, tuple)) and len(c) >= 1:
            t = str(c[0])
        else:
            t = str(c)
        texts.append(t.strip())
    return texts


def _exact_dedup(texts, original_comments):
    """完全去重，返回 (unique_texts, unique_originals)"""
    seen = set()
    unique_texts = []
    unique_originals = []
    for i, text in enumerate(texts):
        if text not in seen:
            seen.add(text)
            unique_texts.append(text)
            unique_originals.append(original_comments[i])
    return unique_texts, unique_originals


def _similarity_dedup(unique_texts, unique_originals, threshold=0.80):
    """相似度去重（O(n²)，仅对小数据集）"""
    if len(unique_texts) <= 100:
        final_texts = []
        final_originals = []
        used = set()

        for i, (text, orig) in enumerate(zip(unique_texts, unique_originals)):
            if i in used:
                continue
            used.add(i)
            final_texts.append(text)
            final_originals.append(orig)

            # 查找相似评论
            for j in range(i + 1, len(unique_texts)):
                if j in used:
                    continue
                ratio = SequenceMatcher(None, text, unique_texts[j]).ratio()
                if ratio >= threshold:
                    used.add(j)

        return final_originals

    return unique_originals


def _extract_sentiment(comments):
    """提取情感分布"""
    pos_keywords = ["棒", "好", "赞", "喜欢", "优秀", "精彩", "厉害", "牛"]
    neg_keywords = ["差", "烂", "垃圾", "恶心", "失望", "拉黑", "举报"]

    pos, neg, neu = 0, 0, 0
    for c in comments:
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
        elif isinstance(c, (list, tuple)) and len(c) >= 1:
            text = str(c[0])
        else:
            text = str(c)

        text = text.lower()
        has_pos = any(kw in text for kw in pos_keywords)
        has_neg = any(kw in text for kw in neg_keywords)

        if has_pos and not has_neg:
            pos += 1
        elif has_neg and not has_pos:
            neg += 1
        else:
            neu += 1

    total = max(pos + neg + neu, 1)
    return {"pos": pos / total, "neg": neg / total, "neu": neu / total}


def _extract_topics(comments, top_n=5):
    """提取高频话题关键词"""
    # 简单分词（按空格 + 标点）
    words = []
    for c in comments:
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
        elif isinstance(c, (list, tuple)) and len(c) >= 1:
            text = str(c[0])
        else:
            text = str(c)
        # 分词（保留中文+英文）
        tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text)
        words.extend([t for t in tokens if len(t) >= 2])

    if not words:
        return "（无）"

    counter = Counter(words)
    top = counter.most_common(top_n)
    return "、".join(w for w, _ in top)


def _detect_templating(comments):
    """检测模板化迹象"""
    texts = []
    for c in comments:
        if isinstance(c, dict):
            texts.append(c.get("content", c.get("message", "")))
        elif isinstance(c, (list, tuple)) and len(c) >= 1:
            texts.append(str(c[0]))
        else:
            texts.append(str(c))

    similarities = []
    for i in range(len(texts)):
        for j in range(i + 1, min(i + 10, len(texts))):
            ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if ratio >= 0.60:
                similarities.append(ratio)

    if not similarities:
        return {"has_template": False, "similarity_score": 0, "examples": []}

    avg_sim = sum(similarities) / len(similarities)
    has_template = avg_sim >= 0.70

    # 找出相似的评论作为示例
    examples = []
    for i in range(min(3, len(texts))):
        for j in range(i + 1, min(i + 5, len(texts))):
            ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if ratio >= 0.70:
                if len(examples) < 2:
                    examples.append(texts[i][:50])
                break

    return {
        "has_template": has_template,
        "similarity_score": round(avg_sim, 2),
        "examples": examples,
    }


def _fmt_sentiment(s):
    return f"正面{int(s['pos']*100)}% | 负面{int(s['neg']*100)}% | 中性{int(s['neu']*100)}%"


if __name__ == "__main__":
    # 测试
    test_comments = [
        {"content": "画质太棒了！", "like": 234},
        {"content": "画质太棒了！", "like": 120},
        {"content": "画质真的很棒！", "like": 89},
        {"content": "音质需要改进", "like": 12},
        {"content": "UP主加油！", "like": 56},
    ]

    result = compress_comments_for_prompt(test_comments, max_examples=3)
    print(result)

    profile = "等级: Lv5 | 粉丝: 1234 | 投稿: 0 | 头像: 无"
    print(compress_user_profile(profile))
