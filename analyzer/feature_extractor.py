"""
水军特征提取器

从评论列表和用户信息中提取 13 个特征 (F1-F8, F12, F14-F16, F18)。
每个特征返回 0.0 ~ 1.0 分数, 1.0 = 高度可疑。

v2.1 新增 F12-F14: 账号空间画像检测
v2.8 新增 F15: 商业引流 (赌博/色情/加微信/刷单等硬广告)
v2.10 新增 F16-F17 (来自 CleanX 机器人判断增强版):
  - F16: 评论时间规律性
  - F17: 自评相似度
v2.16 新增 F18: 签名引战检测 (个性签名含挑衅/引战话术)
"""

import re
from collections import defaultdict
from datetime import datetime


class FeatureExtractor:
    """
    13 个水军特征提取器 (F1-F8, F12, F14-F16, F18)。

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

    # ---- F18 签名引战关键词库 (v2.16) ----
    # 分类一: 直接挑衅 — 向点击主页的人宣战
    _SIGN_TROLL_DIRECT_KW = {
        "查成分", "查你爹", "查爹", "点进主页", "点进来",
        "你爹", "你妈", "你主子", "你爹成分", "成分查",
        "视奸", "偷看", "窥屏", "翻主页", "看主页",
        "精神胜利", "精神胜利法",
    }
    # 分类二: 防御/嘲讽 — 预设自己遭到攻击并进行嘲讽
    _SIGN_TROLL_DEFENSIVE_KW = {
        "包容别人", "喷子", "键盘侠", "杠精", "举报狗",
        "拉黑", "黑名单", "加入黑名单",
        "自认吵不过", "吵不过", "逃避", "自尊心",
        "你攻击", "有人会理你吗", "你又能怎样",
        "你又能如何", "你什么都不是",
        "可怜的自尊心", "满足一下你",
    }
    # 分类三: 引战宣言 — "我就是来搞事的"
    _SIGN_TROLL_PROVOKE_KW = {
        "我混的圈子", "你攻击啥", "我圈子",
        "你尽管骂", "随便骂", "随便喷",
        "无所谓", "不在意", "不痛不痒",
        "只会口嗨", "口嗨", "网络巨人",
        "现实唯唯诺诺", "重拳出击", "网络重拳",
        "来对线", "欢迎对线", "来对骂",
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

            # ---- v2.1 新增: 账号空间画像 ----
            features["f12_account_skeleton"] = self._f12_account_skeleton(mid, user_comms)
            features["f14_sensitive_content"] = self._f14_sensitive_content(mid)
            features["f15_commercial_spam"] = self._f15_commercial_spam(user_comms)  # v2.8
            features["f16_time_regularity"] = self._f16_time_regularity(user_comms)  # v2.10
            features["f18_signature_troll"] = self._f18_signature_troll(mid)         # v2.16

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
                "sign": user_info.get("sign", ""),  # v2.16: 个性签名传给LLM分析
            })

        return results

    # ================================================================
    #  Feature 1: Account Age
    # ================================================================

    def _f1_account_age(self, mid: int, user_comms: list) -> float:
        """
        特征1: 账号年龄。三层兜底:
          1. card API birthday (精确)
          2. MID号段推算 (±1年)
          3. 用户等级
        """
        user = self.users.get(mid, {})
        birthday = user.get("birthday", "")

        # 验证 birthday 是否为有效日期
        reg_date = None
        if isinstance(birthday, str) and len(birthday) >= 10:
            try:
                reg_date = datetime.strptime(birthday[:10], "%Y-%m-%d")
            except ValueError:
                pass
        elif isinstance(birthday, (int, float)) and birthday > 1000000000:
            reg_date = datetime.fromtimestamp(birthday)

        if reg_date:
            account_days = (datetime.now() - reg_date).days
            return _account_age_score(account_days, len(user_comms))

        # ★ 兜底1: MID 号段推算
        estimated_year = _mid_to_approx_year(mid)
        if estimated_year:
            account_days = (datetime.now() - datetime(estimated_year, 1, 1)).days
            return _account_age_score(account_days, len(user_comms))

        # ★ 兜底2: 等级
        level = user_comms[0].get("level", 3)
        if level <= 1:
            return 0.6
        return 0.3

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
        特征4: 头像/认证。

        水军特征: 无头像 + 无认证
        每个缺失项 = +0.50, 两项合计 = 1.0

        注意: 签名检测已独立为 F18 (签名引战度)。
        VIP（大会员）已从此特征移除，独立为 F11 检测。
        """
        user = self.users.get(mid, {})

        if not user:
            return 0.3

        score = 0.0

        # Check avatar
        face = user.get("face", "")
        if not face or "noface" in face:
            score += 0.50

        # Check verification
        official = user.get("official_verify", {})
        if not official or official.get("type", -1) == -1:
            score += 0.50

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
    #  Helper: Garbled Name Detection
    # ================================================================

    # 乱码用户名匹配模式
    _GARBLED_NAME_RE = re.compile(
        r'(?:^bili_[\w]{5,}$)'          # bili_xxxxxxxx 默认名
        r'|(?:^用户\d{5,}$)'              # 用户+数字
        r'|(?:^[a-zA-Z0-9_]{6,}$)'       # 纯字母数字下划线短名
        r'|(?:^[a-zA-Z]+\d{4,}$)'        # 字母+长数字
        , re.IGNORECASE
    )

    def _is_garbled_name(self, uname: str) -> bool:
        """
        检测用户名是否为乱码/机器生成（不是检测 MID/ID）。

        判定规则:
          1. "bili_" 开头 + 随机字符 → B站默认未改名
          2. "用户" + 长数字 → B站默认未改名
          3. 纯数字 (6位以上) → 批量注册模式
          4. 纯字母数字组合且数字占比 > 35% → 疑似机器生成
          5. 键盘顺序字母 (如 asdfgh, qwerty) → 随意输入
          6. 字母 + 4位以上数字 → 批量注册模式
        """
        if not uname:
            return True

        # 1. 默认用户名模式
        if re.match(r'^bili_[\w]{5,}$', uname, re.IGNORECASE):
            return True
        if re.match(r'^用户\d{5,}$', uname):
            return True

        # 2. 纯数字 (6位以上 → 批量注册号)
        if re.match(r'^\d{6,}$', uname):
            return True

        # 3. 高熵字符串检测
        if len(uname) >= 6 and re.match(r'^[a-zA-Z0-9_]+$', uname):
            digit_ratio = sum(c.isdigit() for c in uname) / len(uname)
            # 数字占比超过 35% 且无明显英文单词 → 乱码
            if digit_ratio > 0.35:
                words = re.findall(r'[a-zA-Z]{3,}', uname)
                if len(words) == 0:
                    return True

        # 4. 键盘顺序字母 (随意输入)
        if re.match(r'^(asdfgh|qwerty|zxcvbn|qazwsx)[a-z]*$', uname, re.IGNORECASE):
            return True

        # 5. 字母 + 4位以上数字 (机器注册号: 短字母+长数字)
        #    排除正常取名如 MiXeD2024 (字母占比高且形成单词)
        m = re.match(r'^([a-zA-Z]+)(\d{4,})$', uname)
        if m:
            letters_part = m.group(1)
            digits_part = m.group(2)
            digit_pct = len(digits_part) / len(uname)
            # 数字 >= 60% 且字母 <= 3 → 几乎必是机器号
            if digit_pct >= 0.60 and len(letters_part) <= 3:
                return True
            # 数字 >= 50% 且字母部分无明显单词 → 机器号
            if digit_pct >= 0.50 and len(letters_part) <= 4:
                return True

        return False

    # ================================================================
    #  Feature 12: Account Skeleton (账号骨架检测)
    # ================================================================

    # B站默认个性签名（未修改过）
    _DEFAULT_SIGN = "这个人没有填简介啊~~~"

    def _f12_account_skeleton(self, mid: int, user_comms: list) -> float:
        """
        特征12: 账号骨架检测。

        规则: 无头像 + 用户名乱码 + 无动态 + 无投稿 + 默认签名 → 空壳号

        五要素每项 0.20 分:
          1. 无头像 (复用 F4 逻辑)
          2. 用户名乱码 (机器生成/默认名/纯数字/键盘乱按)
          3. 无动态 (帖子数=0)
          4. 无投稿 (视频数=0)
          5. 默认签名 (B站默认"这个人没有填简介啊~~~"，从未修改过)

        5/5 命中 → 1.0 (百分百空壳水军号)
        4/5 命中 → 0.80
        3/5 命中 → 0.60
        2/5 命中 → 0.40
        1/5 命中 → 0.20
        """
        user = self.users.get(mid, {})

        score = 0.0

        # 1. 无头像
        face = user.get("face", "")
        if not face or "noface" in face:
            score += 0.20

        # 2. 用户名乱码（不是 MID ID）
        uname = user_comms[0].get("uname", "") if user_comms else ""
        if self._is_garbled_name(uname):
            score += 0.20

        # 3. 无动态: 优先用 API 返回的 post_count, 其次用 user_posts 列表长度
        post_count = user.get("post_count")
        if post_count is None:
            posts = self._user_posts.get(mid, [])
            post_count = len(posts)
        if post_count == 0:
            score += 0.20

        # 4. 无投稿
        uploads = user.get("upload_count", -1)
        if uploads == -1:
            # 数据不足时不扣此项 (保守策略)
            pass
        elif uploads == 0:
            score += 0.20

        # 5. 默认签名 (B站默认签名=从未修改过个人简介)
        sign = user.get("sign", "")
        if sign == self._DEFAULT_SIGN:
            score += 0.20

        return score

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

    # ================================================================
    #  Feature 16: Comment Time Regularity (评论时间规律性) — v2.10
    #  Source: CleanX 机器人判断增强版 — analyzeUserBehavior
    # ================================================================

    def _f16_time_regularity(self, user_comms: list) -> float:
        """
        特征16: 评论时间规律性。

        来自 CleanX 脚本的行为分析:
        - 真实用户评论时间间隔随机，标准差大
        - 机器人/水军按固定频率发帖，时间间隔标准差小 ("上班式"规律)
        - 仅适用于 ≥ 3 条评论的用户

        算法:
          1. 提取所有评论时间戳，按时间排序
          2. 计算相邻时间间隔
          3. 计算间隔的变异系数 (CV = std/mean)
          4. CV < 0.5 → 高度规律 → 高分

        归一化: CV=0 → 1.0, CV=1.0 → 0.0
        """
        if len(user_comms) < 3:
            return 0.0

        timestamps = []
        for c in user_comms:
            # Support both ctime and created_at field names
            ts = c.get("ctime") or c.get("created_at") or c.get("timestamp")
            if ts:
                try:
                    timestamps.append(int(ts))
                except (ValueError, TypeError):
                    pass

        if len(timestamps) < 3:
            return 0.0

        timestamps.sort()
        intervals = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]

        if not intervals:
            return 0.0

        mean_interval = sum(intervals) / len(intervals)
        if mean_interval == 0:
            return 0.0  # Same-second timestamps → unreliable

        variance = sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)
        std_dev = variance ** 0.5
        coefficient_of_variation = std_dev / mean_interval  # CV = 相对离散度

        # CV < 0.3 → 高度规律(0.9), CV < 0.5 → 中等规律(0.6), CV < 0.8 → 轻微规律(0.3)
        if coefficient_of_variation < 0.3:
            return 0.9
        elif coefficient_of_variation < 0.5:
            return 0.6
        elif coefficient_of_variation < 0.8:
            return 0.3
        return 0.0

    # ================================================================
    #  Feature 18: Signature Troll Detection (v2.16)
    # ================================================================

    def _f18_signature_troll(self, mid: int) -> float:
        """
        特征18: 签名引战度。

        检测目标账号的个性签名是否包含挑衅/引战话术。
        水军引战号会在签名中预设攻击对象（"查你爹成分"）、
        嘲讽点进主页的人（"可怜的自尊心"）、或宣称无所谓态度（"随便骂"）。

        使用三级关键词库递增评分:
        - 一类 (直接挑衅): 单个 +0.25, 两个+ +0.50
        - 二类 (防御嘲讽): 单个 +0.15, 两个+ +0.30
        - 三类 (引战宣言): 单个 +0.10, 两个+ +0.20

        总分 = 0.0~1.0 (封顶1.0)
        典型"精神胜利法"签名：含2个一类+2个二类+1个三类 → 0.50+0.30+0.10=0.90
        """
        user = self.users.get(mid, {})
        if not user:
            return 0.0

        sign = user.get("sign", "")
        if not sign:
            return 0.0  # 无签名 = 不触发引战检测（由F4三无检测覆盖）

        # 默认签名（从未修改过个人简介）= 不触发引战，归 F12 账号骨架
        if sign == self._DEFAULT_SIGN:
            return 0.0

        score = 0.0

        # 一类: 直接挑衅 — 最高权重
        d1 = sum(1 for kw in self._SIGN_TROLL_DIRECT_KW if kw in sign)
        if d1 == 1:
            score += 0.25
        elif d1 >= 2:
            score += 0.50

        # 二类: 防御/嘲讽 — 中等权重
        d2 = sum(1 for kw in self._SIGN_TROLL_DEFENSIVE_KW if kw in sign)
        if d2 == 1:
            score += 0.15
        elif d2 >= 2:
            score += 0.30

        # 三类: 引战宣言 — 较低权重
        d3 = sum(1 for kw in self._SIGN_TROLL_PROVOKE_KW if kw in sign)
        if d3 == 1:
            score += 0.10
        elif d3 >= 2:
            score += 0.20

        return min(1.0, score)
    #  Helper: Levenshtein Ratio (编辑距离相似度)
    # ================================================================

    @staticmethod
    def _levenshtein_ratio(s1: str, s2: str) -> float:
        """
        计算两个字符串的 Levenshtein 相似度比率。

        返回值: 0.0 (完全不同) ~ 1.0 (完全相同)

        使用双行滚动数组优化空间复杂度 O(min(m,n))。
        最大比较长度 2000 字符（性能保护）。
        """
        # Truncate long strings for performance
        s1 = s1[:2000]
        s2 = s2[:2000]

        if s1 == s2:
            return 1.0
        if not s1 or not s2:
            return 0.0

        # Ensure s1 is the shorter string for O(min) space
        if len(s1) > len(s2):
            s1, s2 = s2, s1

        len1, len2 = len(s1), len(s2)

        # Rolling array: previous row
        prev = list(range(len2 + 1))

        for i in range(1, len1 + 1):
            curr = [i] + [0] * len2
            for j in range(1, len2 + 1):
                cost = 0 if s1[i - 1] == s2[j - 1] else 1
                curr[j] = min(
                    prev[j] + 1,       # deletion
                    curr[j - 1] + 1,   # insertion
                    prev[j - 1] + cost # substitution
                )
            prev = curr

        distance = prev[len2]
        max_len = max(len1, len2)
        return 1.0 - (distance / max_len) if max_len > 0 else 1.0


# ================================================================
#  F1 辅助函数: MID 号段 → 近似注册年份
# ================================================================

def _mid_to_approx_year(mid: int) -> int:
    """根据 B站 UID 号段反推大致注册年份，精度 ±1 年（B站 API regtime 恒为 0 的兜底方案）。"""
    if mid < 1000000:
        return 2010
    elif mid < 10000000:
        return 2011
    elif mid < 100000000:
        return 2013
    elif mid < 200000000:
        return 2015
    elif mid < 300000000:
        return 2017
    elif mid < 400000000:
        return 2018
    elif mid < 500000000:
        return 2019
    elif mid < 600000000:
        return 2020
    elif mid < 700000000:
        return 2021
    elif mid < 800000000:
        return 2022
    elif mid < 1000000000:
        return 2023
    elif mid < 1200000000:
        return 2024
    else:
        return 2025  # 12 亿+ = 最近注册


def _account_age_score(account_days: int, comment_count: int) -> float:
    """根据账号天数+评论数计算 F1 分数。新号+多评论=高风险。"""
    if account_days < 30:
        return 0.9 if comment_count >= 5 else 0.7
    elif account_days < 90:
        return 0.6 if comment_count >= 5 else 0.4
    elif account_days < 180:
        return max(0.3, comment_count * 0.03)
    elif account_days < 365:
        return max(0.15, comment_count * 0.02)
    elif account_days < 730:
        return max(0.05, comment_count * 0.01)
    return 0.0
