"""
水军特征提取器

从评论列表和用户信息中提取 15 个特征 (F1-F15)。
每个特征返回 0.0 ~ 1.0 分数, 1.0 = 高度可疑。

v2.1 新增 F12-F14: 账号空间画像检测
  - F12: 账号骨架 (无头像+ID乱码+无动态+无投稿)
  - F13: 转发抽奖模式 (无投稿+全转发抽奖动态)
  - F14: 敏感内容 (女拳/以乌/造谣抹黑)
v2.8 新增 F15: 商业引流 (赌博/色情/加微信/刷单等硬广告)
"""

import re
from collections import defaultdict
from datetime import datetime


class FeatureExtractor:
    """
    14 个水军特征提取器。

    输入:
      comments: [CommentItem dict]
      users: {mid: UserInfoItem dict}
      get_user_sim_score: callable mid → float (来自 SimilarityDetector)
      burst_scores: {mid: float} (来自 TimeAnalyzer)
      batch_scores: {mid: float} (来自 TimeAnalyzer)
      user_posts: {mid: [post_dict]} (v2.1 — 用户空间动态, 可选)
    """

    # ---- F14 敏感内容关键词库 ----
    _FEMINIST_EXTREMIST_KW = {
        "女拳", "打拳", "蝈蝻", "婚驴", "直男癌", "普信男", "家暴男",
        "xdz", "金针菇", "屌癌", "白骑士", "婚恋市场", "彩礼",
        "生孩子警告", "接盘", "幕刃", "小仙女", "妈宝男", "凤凰男",
        "性别对立", "厌男", "厌女", "incel", "男性凝视",
    }
    _GEOPOLITICAL_KW = {
        "以色列", "乌克兰", "巴勒斯坦", "哈马斯", "加沙", "IDF",
        "泽连斯基", "内塔尼亚胡", "俄乌", "乌军", "以军",
        "犹太", "纳粹", "锡安", "中东",
    }
    _RUMOR_SLANDER_KW = {
        "造谣", "抹黑", "黑子", "水军", "收了钱", "恰烂钱",
        "带节奏", "洗地", "五毛", "美分", "谣言", "假的",
        "别信", "骗人", "资本", "境外势力", "1450", "网军",
    }

    # ---- F13 转发抽奖关键词 ----
    _LOTTERY_KW = {
        "抽奖", "转发", "送", "roll", "揪", "抽", "关注+",
        "三连", "一键三连", "白嫖", "福利", "粉丝福利",
    }

    def __init__(self, comments, users, get_user_sim_score,
                 burst_scores=None, batch_scores=None, user_posts=None):
        self.comments = comments
        self.users = users or {}
        self._sim_score_fn = get_user_sim_score
        self._burst_scores = burst_scores or {}
        self._batch_scores = batch_scores or {}
        self._user_posts = user_posts or {}  # v2.1: {mid: [post_dict]}

        # Group comments by user
        self._user_comments = self._group_by_user()

        # Extract all user mids from comments
        self._comment_mids = set()
        for c in comments:
            mid = c.get("mid")
            if mid is not None:
                self._comment_mids.add(int(mid))

    def _group_by_user(self) -> dict:
        """按 mid 分组评论"""
        groups = defaultdict(list)
        for c in self.comments:
            mid = c.get("mid", 0)
            groups[int(mid)].append(c)
        return dict(groups)

    # ================================================================
    #  Main extraction
    # ================================================================

    def extract_all(self) -> list:
        """
        提取所有用户的特征向量。

        Returns:
        [
            {
                "mid": 123456,
                "uname": "用户A",
                "level": 4,
                "comment_count": 15,
                "features": { "f1_account_age": 0.3, ... },
                "sample_comments": ["评论1", "评论2"],
            }
        ]
        """
        results = []
        for mid, user_comms in self._user_comments.items():
            if len(user_comms) < 1:
                continue

            features = {}
            features["f1_account_age"] = self._f1_account_age(mid, user_comms)
            features["f2_follow_ratio"] = self._f2_follow_ratio(mid)
            features["f3_level_score"] = self._f3_level_score(mid, user_comms)
            features["f4_avatar_verify"] = self._f4_avatar_verify(mid)
            features["f5_content_similarity"] = self._f5_content_similarity(mid)
            features["f6_time_burst"] = self._f6_time_burst(mid)
            features["f7_sentiment_extreme"] = self._f7_sentiment_extreme(user_comms)
            features["f8_like_ratio"] = self._f8_like_ratio(user_comms)
            features["f9_registration_batch"] = self._f9_registration_batch(mid)
            features["f10_interaction_ring"] = self._f10_interaction_ring(mid, user_comms)
            features["f11_vip_anomaly"] = self._f11_vip_anomaly(mid, user_comms)

            # ---- v2.1 新增: 账号空间画像 ----
            features["f12_account_skeleton"] = self._f12_account_skeleton(mid, user_comms)
            features["f13_lottery_repost"] = self._f13_lottery_repost(mid)
            features["f14_sensitive_content"] = self._f14_sensitive_content(mid)
            features["f15_commercial_spam"] = self._f15_commercial_spam(user_comms)  # v2.8

            # Gather sample comments
            sample = [c.get("content", "")[:80] for c in user_comms[:3]]

            user_info = self.users.get(mid, {})

            results.append({
                "mid": mid,
                "uname": user_comms[0].get("uname", f"User_{mid}"),
                "level": user_comms[0].get("level", 0),
                "comment_count": len(user_comms),
                "features": features,
                "sample_comments": sample,
            })

        return results

    # ================================================================
    #  Feature 1: Account Age
    # ================================================================

    def _f1_account_age(self, mid: int, user_comms: list) -> float:
        """
        特征1: 账号年龄。

        数据来源: UserInfoItem.birthday (B站 API 字段名就是 birthday, 实际是注册日期)
        规则: 注册 < 30天 且 评论多 → 高分
        """
        user = self.users.get(mid, {})
        birthday = user.get("birthday", "")

        if not birthday:
            # No user info → moderate suspicion for new-feeling accounts
            # We can use the commenter's level as proxy
            level = user_comms[0].get("level", 3)
            if level <= 1:
                return 0.6
            return 0.3

        try:
            if isinstance(birthday, str):
                reg_date = datetime.strptime(birthday[:10], "%Y-%m-%d")
            elif isinstance(birthday, int):
                reg_date = datetime.fromtimestamp(birthday)
            else:
                return 0.3
        except (ValueError, TypeError):
            return 0.3

        days_since_reg = (datetime.now() - reg_date).days
        comment_count = len(user_comms)

        # Score: newer + more comments = more suspicious
        age_score = max(0, 1 - days_since_reg / 365)  # 0→1, 1y→0
        volume_score = min(1, comment_count / 10)       # 0→0, 10→1
        return age_score * volume_score

    # ================================================================
    #  Feature 2: Follow Ratio
    # ================================================================

    def _f2_follow_ratio(self, mid: int) -> float:
        """
        特征2: 粉丝/关注比。

        水军特征: 大量关注但极低粉丝 (刷粉模式)
        """
        user = self.users.get(mid, {})
        follower = user.get("follower", 0)
        following = user.get("following", 0)

        if not user:
            return 0.3  # unknown

        if follower < 50 and following > 500:
            return 0.9
        if follower < 100 and following > 300:
            return 0.7
        if follower < 200 and following > 200:
            return 0.4
        return 0.0

    # ================================================================
    #  Feature 3: User Level
    # ================================================================

    def _f3_level_score(self, mid: int, user_comms: list) -> float:
        """
        特征3: 用户等级。

        水军特征: 低等级(0-2) + 高频评论
        """
        level = user_comms[0].get("level", 3)
        comment_count = len(user_comms)

        level_map = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.3, 4: 0.1, 5: 0.0, 6: 0.0}
        base = level_map.get(level, 0.3)

        # Amplify if many comments
        return min(1.0, base * min(1, comment_count / 5))

    # ================================================================
    #  Feature 4: Avatar / Verification
    # ================================================================

    def _f4_avatar_verify(self, mid: int) -> float:
        """
        特征4: 头像/认证/签名。

        水军特征: 无头像 + 无认证 + 无签名
        每个缺失项 = +0.34, 三项合计 = ~1.0

        注意: VIP（大会员）已从此特征移除，独立为 F11 检测。
        有大会员不代表不是水军——专业水军团队会批量购买大会员做伪装。
        """
        user = self.users.get(mid, {})

        if not user:
            return 0.3

        score = 0.0

        # Check avatar
        face = user.get("face", "")
        if not face or "noface" in face:
            score += 0.34

        # Check verification
        official = user.get("official_verify", {})
        if not official or official.get("type", -1) == -1:
            score += 0.34

        # Check signature
        sign = user.get("sign", "")
        if not sign:
            score += 0.34

        return min(1.0, score)

    # ================================================================
    #  Feature 5: Content Similarity
    # ================================================================

    def _f5_content_similarity(self, mid: int) -> float:
        """
        特征5: 内容相似度。

        来自 SimilarityDetector 的预计算结果。
        """
        if self._sim_score_fn:
            return self._sim_score_fn(int(mid))
        return 0.0

    # ================================================================
    #  Feature 6: Time Burst
    # ================================================================

    def _f6_time_burst(self, mid: int) -> float:
        """
        特征6: 时间爆发。

        来自 TimeAnalyzer 的滑动窗口 Z-score。
        """
        return self._burst_scores.get(int(mid), 0.0)

    # ================================================================
    #  Feature 7: Sentiment Extreme
    # ================================================================

    # Keywords
    _POSITIVE_WORDS = {"最好", "最棒", "太棒", "完美", "无敌", "永远滴神", "神作",
                        "顶", "支持", "最牛", "厉害", "第一", "最强", "不服不行"}
    _NEGATIVE_WORDS = {"垃圾", "最差", "恶心", "取关", "傻逼", "废物", "晦气",
                        "骗人", "举报", "踩", "差评"}

    def _f7_sentiment_extreme(self, user_comms: list) -> float:
        """
        特征7: 情感极端。

        简易关键词匹配 (无需 NLP 库)。
        如果 100% 都是正面或 100% 都是负面 → 高分
        """
        if not user_comms:
            return 0.0

        positive = 0
        negative = 0
        total = 0

        for c in user_comms:
            text = c.get("content", "")
            if len(text) < 3:
                continue
            total += 1
            if any(w in text for w in self._POSITIVE_WORDS):
                positive += 1
            if any(w in text for w in self._NEGATIVE_WORDS):
                negative += 1

        if total < 3:
            return 0.0

        pos_ratio = positive / total
        neg_ratio = negative / total
        max_bias = max(pos_ratio, neg_ratio)

        # 100% one-side → 1.0, 50/50 → 0.0
        return max(0, (max_bias - 0.5) * 2)

    # ================================================================
    #  Feature 8: Like Ratio
    # ================================================================

    def _f8_like_ratio(self, user_comms: list) -> float:
        """
        特征8: 赞评比。

        水军特征: 大量评论但几乎零赞
        score = max(0, 1 - (avg_likes / 10))
        """
        if not user_comms:
            return 0.0

        total_likes = sum(c.get("like_count", 0) for c in user_comms)
        avg_likes = total_likes / len(user_comms)

        return max(0, 1 - avg_likes / 10)

    # ================================================================
    #  Feature 9: Registration Batch
    # ================================================================

    def _f9_registration_batch(self, mid: int) -> float:
        """
        特征9: 批量注册。

        来自 TimeAnalyzer 的注册日期集中度。
        """
        return self._batch_scores.get(int(mid), 0.0)

    # ================================================================
    #  Feature 10: Interaction Ring
    # ================================================================

    _MENTION_RE = re.compile(r'@(.+?)(?:\s|$|:)')

    def _f10_interaction_ring(self, mid: int, user_comms: list) -> float:
        """
        特征10: 互动小圈子检测。

        子评论中反复 @ 相同几个账号 → 互相刷量嫌疑。
        """
        mentioned = defaultdict(int)

        for c in user_comms:
            content = c.get("content", "")
            mentions = self._MENTION_RE.findall(content)
            for m in mentions:
                mentioned[m] += 1

        if not mentioned:
            return 0.0

        # Check concentration: if mentions are concentrated on few targets
        mention_counts = sorted(mentioned.values(), reverse=True)
        total = sum(mention_counts)
        if total < 3:
            return 0.0

        # Top-2 targets' share
        top2 = sum(mention_counts[:2])
        concentration = top2 / total

        # 80%+ mentions on same 2 targets → suspicious
        if concentration > 0.8:
            return 0.8
        if concentration > 0.6:
            return 0.5
        if concentration > 0.4:
            return 0.2
        return 0.0

    # ================================================================
    #  Feature 11: VIP Anomaly (大会员异常)
    # ================================================================

    def _f11_vip_anomaly(self, mid: int, user_comms: list) -> float:
        """
        特征11: 大会员异常。

        设计意图:
          当前 F4 已将 VIP 移除——大会员不再被视为"正常用户"证据。
          本特征专门捕捉"买大会员做伪装"的水军模式。

        判断逻辑:
          1. 无大会员 → 0.0（不在此特征扣分，由其他特征覆盖）
          2. 高等级(Lv4+) + 大会员 → 0.0（真正常付费用户）
          3. 低等级 + 大会员 + 模板化/爆发评论 → 0.6~1.0

        VIP 类型区分:
          vip_status=1 (月度) → 成本25元，水军最爱 → 乘数1.0
          vip_status=2 (年度) → 成本较高 → 乘数0.7
        """
        user = self.users.get(mid, {})
        if not user:
            return 0.0

        vip_status = user.get("vip_status", 0)

        # 无大会员 → 不在本特征评分
        if vip_status == 0:
            return 0.0

        level = user_comms[0].get("level", 3) if user_comms else 3

        # 高等级 + 大会员 = 正常付费用户，不扣分
        if level >= 4:
            return 0.0

        # --- 低等级 + 大会员 → 需要结合其他信号判断 ---

        # 等级反比基础分：等级越低 + 有大会员 → 越像买来的号
        level_score_map = {0: 0.9, 1: 0.7, 2: 0.5, 3: 0.3}
        base = level_score_map.get(level, 0.2)

        # 获取其他可疑信号（复用已有特征方法）
        sim_score = self._f5_content_similarity(mid)
        burst_score = self._f6_time_burst(mid)

        # 内容/行为越可疑，VIP 作为伪装的嫌疑越大
        suspicious_amplifier = max(sim_score, burst_score)

        # 月费大会员比年费更像水军伪装（成本低，可批量）
        vip_type_multiplier = 1.0 if vip_status == 1 else 0.7

        vip_anomaly = base * (1.0 + suspicious_amplifier) * vip_type_multiplier

        return min(1.0, vip_anomaly)

    # ================================================================
    #  Helper: Garbled Name Detection
    # ================================================================

    # 乱码ID匹配模式
    _GARBLED_NAME_RE = re.compile(
        r'(?:^bili_[\w]{5,}$)'          # bili_xxxxxxxx 默认名
        r'|(?:^用户\d{5,}$)'              # 用户+数字
        r'|(?:^[a-zA-Z0-9_]{6,}$)'       # 纯字母数字下划线短名
        r'|(?:^[a-zA-Z]+\d{4,}$)'        # 字母+长数字
        , re.IGNORECASE
    )

    def _is_garbled_name(self, uname: str) -> bool:
        """
        检测 ID 是否为乱码/机器生成。

        判定规则:
          1. "bili_" 开头 + 随机字符 → B站默认未改名
          2. "用户" + 长数字 → B站默认未改名
          3. 纯字母数字组合且数字占比 > 35% → 疑似机器生成
          4. 字母 + 4位以上数字 → 批量注册模式
        """
        if not uname:
            return True

        # 1. 默认用户名模式
        if re.match(r'^bili_[\w]{5,}$', uname, re.IGNORECASE):
            return True
        if re.match(r'^用户\d{5,}$', uname):
            return True

        # 2. 高熵字符串检测
        if len(uname) >= 6 and re.match(r'^[a-zA-Z0-9_]+$', uname):
            digit_ratio = sum(c.isdigit() for c in uname) / len(uname)
            # 数字占比超过 35% 且无明显英文单词 → 乱码
            if digit_ratio > 0.35:
                words = re.findall(r'[a-zA-Z]{3,}', uname)
                if len(words) == 0:
                    return True

        # 3. 字母 + 4位以上数字 (如 abc12345, test2024)
        if re.match(r'^[a-zA-Z]+\d{4,}$', uname):
            return True

        return False

    # ================================================================
    #  Feature 12: Account Skeleton (账号骨架检测)
    # ================================================================

    def _f12_account_skeleton(self, mid: int, user_comms: list) -> float:
        """
        特征12: 账号骨架检测。

        规则: 无头像 + ID乱码 + 无动态 + 无投稿 → 百分百水军

        四要素每项 0.25 分:
          1. 无头像 (复用 F4 逻辑)
          2. ID 乱码 (机器生成/默认名)
          3. 无动态 (帖子数=0)
          4. 无投稿 (视频数=0)

        全部命中 → 1.0 (百分百水军)
        三项命中 → 0.75
        两项命中 → 0.5
        一项命中 → 0.25
        """
        user = self.users.get(mid, {})

        score = 0.0

        # 1. 无头像
        face = user.get("face", "")
        if not face or "noface" in face:
            score += 0.25

        # 2. ID 乱码
        uname = user_comms[0].get("uname", "") if user_comms else ""
        if self._is_garbled_name(uname):
            score += 0.25

        # 3. 无动态: 优先用 API 返回的 post_count, 其次用 user_posts 列表长度
        post_count = user.get("post_count")
        if post_count is None:
            posts = self._user_posts.get(mid, [])
            post_count = len(posts)
        if post_count == 0:
            score += 0.25

        # 4. 无投稿
        uploads = user.get("upload_count", -1)
        if uploads == -1:
            # 数据不足时不扣此项 (保守策略)
            pass
        elif uploads == 0:
            score += 0.25

        return score

    # ================================================================
    #  Feature 13: Lottery Repost (转发抽奖检测)
    # ================================================================

    def _f13_lottery_repost(self, mid: int) -> float:
        """
        特征13: 转发抽奖模式。

        规则: 无投稿 + 动态全是转发抽奖 → 大概率水军号

        判定:
          1. 无动态数据 → 0.0 (数据不足)
          2. 有投稿 → 0.0 (正常用户, 不在本特征扣分)
          3. 无投稿 + 转发比 > 80% + 抽奖比 > 50% → 0.85 (大概率)
          4. 无投稿 + 转发比 > 60% → 0.5
          5. 无投稿 + 转发比 > 40% → 0.3
        """
        user = self.users.get(mid, {})

        # 数据不足
        uploads = user.get("upload_count", -1)
        posts = self._user_posts.get(mid, [])
        if uploads == -1 and not posts:
            return 0.0

        # 有投稿 → 正常用户
        if uploads > 0:
            return 0.0

        if not posts or len(posts) < 3:
            return 0.0

        repost_count = 0
        lottery_count = 0

        for post in posts:
            content = post if isinstance(post, str) else (
                post.get("content", "") if isinstance(post, dict) else str(post)
            )

            # 判断是否转发
            is_repost = False
            if isinstance(post, dict):
                is_repost = post.get("is_repost", False)
                if not is_repost:
                    # 内容中检测 "转发动态" 或 "转发了"
                    is_repost = "转发" in content[:20]

            if is_repost:
                repost_count += 1
                if any(kw in content for kw in self._LOTTERY_KW):
                    lottery_count += 1

        total = len(posts)
        if total < 3:
            return 0.0

        repost_ratio = repost_count / total
        lottery_ratio = lottery_count / max(repost_count, 1)

        if repost_ratio > 0.8 and lottery_ratio > 0.5:
            return 0.85
        elif repost_ratio > 0.6:
            return 0.5
        elif repost_ratio > 0.4:
            return 0.3

        return 0.0

    # ================================================================
    #  Feature 14: Sensitive Content (敏感内容检测)
    # ================================================================

    def _f14_sensitive_content(self, mid: int) -> float:
        """
        特征14: 敏感内容检测。

        规则: 历史动态含 女拳/以乌/造谣抹黑 → 百分百水军号

        三组关键词独立匹配, 任意命中 → 1.0:
          1. 女拳极端言论 (25 关键词)
          2. 国际政治 (14 关键词)
          3. 造谣抹黑 (18 关键词)

        无动态数据 → 0.0
        """
        posts = self._user_posts.get(mid, [])
        if not posts:
            return 0.0

        for post in posts:
            content = post if isinstance(post, str) else (
                post.get("content", "") if isinstance(post, dict) else str(post)
            )

            if any(kw in content for kw in self._FEMINIST_EXTREMIST_KW):
                return 1.0
            if any(kw in content for kw in self._GEOPOLITICAL_KW):
                return 1.0
        return 0.0

    # ================================================================
    #  Feature 15: Commercial Spam (商业引流检测) — v2.8
    # ================================================================

    # 赌博/色情/刷单 硬广告关键词
    _GAMBLING_KW = {
        "赌博", "赌场", "百家乐", "六合彩", "澳门", "赌", "押注",
        "下注", "庄闲", "彩票", "时时彩", "快三", "PK10", "赛车",
        "骰宝", "轮盘", "老虎机",
    }
    _PORN_KW = {
        "约炮", "一夜情", "上门", "包夜", "援交", "小姐", "楼凤",
        "大保健", "全套", "半套", "莞式", "丝足", "按摩",
        "av", "番号", "福利姬", "萝莉", "裸聊", "看片",
    }
    _COMMERCIAL_KW = {
        "加微信", "加v", "加V", "加q", "加Q", "加QQ", "加群",
        "微信号", "vx", "VX", "wx", "WX", "QQ", "qq",
        "私聊", "联系我", "找我", "滴滴", "代理", "招代理",
        "兼职", "刷单", "日结", "在家做", "手机兼职",
        "免费领取", "点击领取", "薅羊毛", "0元",
    }

    def _f15_commercial_spam(self, user_comms: list) -> float:
        """
        特征15: 商业引流/硬广告检测。

        三组关键词:
          1. 赌博/赌场 → 直接 1.0
          2. 色情/约炮 → 直接 1.0
          3. 商业引流 (加微信/刷单/招代理) → 按命中比例评分

        多篇评论命中 → 更强信号。
        """
        if not user_comms:
            return 0.0

        gambling_hits = 0
        porn_hits = 0
        commercial_hits = 0

        for c in user_comms:
            content = c.get("content", "")
            if not content:
                continue
            if any(kw in content for kw in self._GAMBLING_KW):
                gambling_hits += 1
            if any(kw in content for kw in self._PORN_KW):
                porn_hits += 1
            if any(kw in content for kw in self._COMMERCIAL_KW):
                commercial_hits += 1

        total = len(user_comms)

        # 赌博/色情 — 高度确定性信号
        if gambling_hits > 0 or porn_hits > 0:
            return 1.0

        # 商业引流 — 按命中比例
        if commercial_hits > 0:
            ratio = commercial_hits / total
            if ratio >= 0.5:
                return 0.9
            elif commercial_hits >= 3:
                return 0.85
            elif commercial_hits >= 2:
                return 0.7
            else:
                return 0.5

        return 0.0
