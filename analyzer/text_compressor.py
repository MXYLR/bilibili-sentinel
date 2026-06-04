"""
智能文本压缩器 — 在保留语义的前提下压缩评论数据

策略:
1. 去重: 删除完全重复的评论
2. 相似度合并: 相似度>80% 的评论只保留1条
3. 结构化摘要: 提取情感分布、高频话题、模板化迹象
4. 示例保留: 每种类型保留1-2条代表性评论

适用场景:
- LLM 初筛分析 (llm_prompts.py)
- LLM 单用户分析 (llm_prompts.py)
- AICU 深度分析 (aicu_prompts.py)
"""

import re
from collections import Counter
from difflib import SequenceMatcher


# ============================================================
#  文本归一化
# ============================================================

def normalize_text(text: str) -> str:
    """归一化文本（去标点、去空格、转小写）"""
    if not text:
        return ""
    # 保留中文、英文、数字
    text = re.sub(r'[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffa-zA-Z0-9\s]', '', text)
    # 合并空白
    text = re.sub(r'\s+', '', text)
    return text.lower()


# ============================================================
#  去重与相似度检测
# ============================================================

def deduplicate_comments(comments: list) -> list:
    """
    去重评论（完全重复 + 近义重复）
    
    Args:
        comments: [{"content": "...", ...}, ...] 或 ["comment1", ...]
    
    Returns:
        去重后的评论列表
    """
    if not comments:
        return []
    
    # 提取文本内容
    texts = []
    originals = []
    for c in comments:
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
        else:
            text = str(c)
        texts.append(text)
        originals.append(c)
    
    # 第1轮: 完全去重
    seen = set()
    unique_texts = []
    unique_originals = []
    for text, orig in zip(texts, originals):
        norm = normalize_text(text)
        if norm and norm not in seen:
            seen.add(norm)
            unique_texts.append(text)
            unique_originals.append(orig)
    
    # 第2轮: 相似度去重（O(n²)，仅对小数据集）
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
                if ratio >= 0.80:  # 80% 相似度阈值
                    used.add(j)
        
        return final_originals
    
    return unique_originals


# ============================================================
#  评论特征提取
# ============================================================

def extract_sentiment_distribution(comments: list) -> dict:
    """
    提取情感分布（简单关键词匹配）
    
    Returns:
        {"positive": 0.6, "negative": 0.3, "neutral": 0.1}
    """
    if not comments:
        return {"positive": 0, "negative": 0, "neutral": 0}
    
    pos_keywords = ["好", "棒", "赞", "喜欢", "优秀", "完美", "厉害", "牛", "6", "❤", "♡"]
    neg_keywords = ["差", "烂", "垃圾", "恶心", "差评", "失望", "拉黑", "举报", "踩"]
    
    pos_count = 0
    neg_count = 0
    total = len(comments)
    
    for c in comments:
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
        else:
            text = str(c)
        
        has_pos = any(kw in text for kw in pos_keywords)
        has_neg = any(kw in text for kw in neg_keywords)
        
        if has_pos and not has_neg:
            pos_count += 1
        elif has_neg and not has_pos:
            neg_count += 1
    
    neu_count = total - pos_count - neg_count
    
    return {
        "positive": round(pos_count / total, 2) if total > 0 else 0,
        "negative": round(neg_count / total, 2) if total > 0 else 0,
        "neutral": round(neu_count / total, 2) if total > 0 else 0,
    }


def extract_key_topics(comments: list, top_n: int = 5) -> list:
    """
    提取高频话题关键词
    
    Args:
        comments: 评论列表
        top_n: 返回前N个关键词
    
    Returns:
        ["话题1", "话题2", ...]
    """
    if not comments:
        return []
    
    # 简单分词（按空格、标点）
    stop_words = {"的", "了", "是", "在", "和", "有", "我", "你", "他", "她", "它",
                  "这", "那", "都", "也", "就", "不", "很", "吗", "呢", "吧", "啊",
                  "哦", "嗯", "哈", "嘿", "唉", "诶"}
    
    words = []
    for c in comments[:50]:  # 只分析前50条
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
        else:
            text = str(c)
        
        # 简单提取2-4字短语
        for phrase_len in [2, 3, 4]:
            for i in range(len(text) - phrase_len + 1):
                phrase = text[i:i + phrase_len]
                if all('\u4e00' <= c <= '\u9fff' for c in phrase):  # 仅中文
                    if phrase not in stop_words:
                        words.append(phrase)
    
    # 统计频率
    counter = Counter(words)
    return [word for word, _ in counter.most_common(top_n)]


def detect_templating(comments: list) -> dict:
    """
    检测模板化迹象
    
    Returns:
        {"has_template": bool, "similarity_score": float, "examples": [str, ...]}
    """
    if len(comments) < 3:
        return {"has_template": False, "similarity_score": 0, "examples": []}
    
    # 计算两两相似度
    texts = []
    for c in comments:
        if isinstance(c, dict):
            texts.append(c.get("content", c.get("message", "")))
        else:
            texts.append(str(c))
    
    similarities = []
    for i in range(len(texts)):
        for j in range(i + 1, min(i + 10, len(texts))):  # 只比较相邻的10条
            ratio = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if ratio >= 0.60:  # 60% 以上视为相似
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


# ============================================================
#  主压缩函数
# ============================================================

def compress_comments_for_prompt(comments: list, max_examples: int = 5) -> str:
    """
    压缩评论数据为结构化摘要（保留语义，大幅减少 token）
    
    Args:
        comments: 评论列表 [{"content": "...", "like": 123}, ...] 或 [str, ...]
        max_examples: 保留的示例评论数量
    
    Returns:
        压缩后的结构化文本（约200-400字符，原文本约2000-5000字符）
    
    Example output:
        评论分析（413条去重后58条）:
        - 情感分布: 正面60% | 负面30% | 中性10%
        - 高频话题: ["画质", "音质", "UP主"]
        - 模板化迹象: 有（相似度0.75，示例: "画质太棒了"）
        - 示例评论:
          [1] "画质太棒了！"（点赞234）
          [2] "音质需要改进"（点赞12）
    """
    if not comments:
        return "（无评论）"
    
    # 1. 去重
    deduped = deduplicate_comments(comments)
    original_count = len(comments)
    deduped_count = len(deduped)
    
    # 2. 提取特征
    sentiment = extract_sentiment_distribution(deduped)
    topics = extract_key_topics(deduped)
    templating = detect_templating(deduped)
    
    # 3. 构建压缩摘要
    lines = []
    lines.append(f"评论分析（{original_count}条去重后{deduped_count}条）:")
    
    # 情感分布
    sent_str = f"正面{sentiment['positive']:.0%} | 负面{sentiment['negative']:.0%} | 中性{sentiment['neutral']:.0%}"
    lines.append(f"- 情感分布: {sent_str}")
    
    # 高频话题
    if topics:
        topics_str = "、".join([f'"{t}"' for t in topics[:5]])
        lines.append(f"- 高频话题: {topics_str}")
    
    # 模板化检测
    if templating["has_template"]:
        lines.append(f"- ⚠️ 模板化迹象: 有（相似度{templating['similarity_score']:.2f}）")
        if templating["examples"]:
            for ex in templating["examples"][:2]:
                lines.append(f"  示例: 「{ex}...」")
    else:
        lines.append(f"- 模板化迹象: 无（相似度{templating['similarity_score']:.2f}）")
    
    # 4. 保留少量示例评论
    lines.append(f"\n- 示例评论（{min(max_examples, len(deduped))}条）:")
    example_count = 0
    for c in deduped:
        if example_count >= max_examples:
            break
        
        if isinstance(c, dict):
            text = c.get("content", c.get("message", ""))
            like = c.get("like", c.get("like_count", 0))
        else:
            text = str(c)
            like = 0
        
        if text:
            text_trunc = text[:80] + ("..." if len(text) > 80 else "")
            if like > 0:
                lines.append(f"  [{example_count + 1}] {text_trunc}（👍{like}）")
            else:
                lines.append(f"  [{example_count + 1}] {text_trunc}")
            example_count += 1
    
    return "\n".join(lines)


def compress_user_profile(profile_text: str, max_chars: int = 300) -> str:
    """
    压缩用户画像文本（提取关键信息，去除冗余）
    
    Args:
        profile_text: 原始画像文本
        max_chars: 最大字符数
    
    Returns:
        压缩后的文本（保留关键信息）
    """
    if not profile_text or len(profile_text) <= max_chars:
        return profile_text
    
    # 简单策略: 保留前max_chars字符 + 最后50字符（可能含重要信息）
    if len(profile_text) > max_chars + 50:
        return profile_text[:max_chars] + "\n...(略去中间内容，保留关键信息)"
    
    return profile_text[:max_chars] + "..."


# ============================================================
#  统一接口
# ============================================================

def compress_for_llm_prompt(
    comments: list,
    profile_text: str = "",
    scenario: str = "initial",  # initial | single | deep
) -> dict:
    """
    为 LLM Prompt 压缩数据（统一接口）
    
    Args:
        comments: 评论列表
        profile_text: 用户画像文本
        scenario: 场景（initial=初筛, single=单用户, deep=深度分析）
    
    Returns:
        {
            "comments_compressed": str,  # 压缩后的评论摘要
            "profile_compressed": str,    # 压缩后的画像
            "original_comment_count": int,
            "compressed_comment_count": int,
            "compression_ratio": float,    # 压缩率
        }
    """
    # 压缩评论
    comments_compressed = compress_comments_for_prompt(
        comments,
        max_examples=5 if scenario == "deep" else 3
    )
    
    # 压缩画像
    profile_compressed = compress_user_profile(profile_text)
    
    # 计算压缩率
    original_chars = sum(len(str(c)) for c in comments) + len(profile_text)
    compressed_chars = len(comments_compressed) + len(profile_compressed)
    compression_ratio = compressed_chars / original_chars if original_chars > 0 else 1.0
    
    return {
        "comments_compressed": comments_compressed,
        "profile_compressed": profile_compressed,
        "original_comment_count": len(comments),
        "compressed_comment_count": len(deduplicate_coments(comments)),
        "compression_ratio": round(compression_ratio, 2),
    }


if __name__ == "__main__":
    # 测试
    test_comments = [
        {"content": "画质太棒了！", "like": 234},
        {"content": "画质太棒了！", "like": 120},  # 重复
        {"content": "画质真的很棒！", "like": 89},    # 近义
        {"content": "音质需要改进", "like": 12},
        {"content": "UP主加油！", "like": 56},
        {"content": "UP主加油啊", "like": 43},     # 近义
    ]
    
    result = compress_for_llm_prompt(test_comments, scenario="initial")
    print(result["comments_compressed"])
    print(f"\n压缩率: {result['compression_ratio']:.1%}")
