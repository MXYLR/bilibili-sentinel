"""
评论相似度检测引擎

使用 difflib.SequenceMatcher 检测模板化评论。

核心算法:
  1. 预处理评论文本 (去表情、去@、去空白)
  2. 两两比较 (NxN, 跳过太短/太长的文本)
  3. SequenceMatcher.ratio() >= 阈值 (默认0.75) → 标记为相似
  4. Union-Find 聚类相似评论组
"""

import difflib
import re
from collections import defaultdict


class SimilarityDetector:
    """
    评论相似度检测器。

    检测流程:
      1. build_matrix() — 构建两两相似度矩阵
      2. find_clusters() — 基于矩阵的连通分量聚类
      3. get_user_similarity_score(mid) — 查询某用户的相似度评分
    """

    def __init__(self, comments: list, threshold: float = 0.75):
        """
        Args:
            comments: CommentItem dict 的列表
            threshold: 相似度阈值 (0-1), 默认 0.75
        """
        self.comments = comments
        self.threshold = threshold
        self.similarity_matrix = {}  # {(rpid_a, rpid_b): ratio}
        self.clusters = []           # [[rpid, rpid, ...], ...]
        self._preprocessed = {}      # {rpid: clean_text}

    # ================================================================
    #  Text Preprocessing
    # ================================================================

    @staticmethod
    def preprocess(text: str) -> str:
        """预处理评论文本: 去表情、去@、去空白"""
        text = re.sub(r'\[.*?\]', '', text)      # B站表情 [doge]
        text = re.sub(r'@\S+', '', text)         # @提及
        text = re.sub(r'\s+', '', text)           # 空白
        return text.strip()

    def _get_text(self, rpid: int) -> str:
        if rpid not in self._preprocessed:
            for c in self.comments:
                if c.get("rpid") == rpid:
                    self._preprocessed[rpid] = self.preprocess(
                        c.get("content", "")
                    )
                    break
            else:
                self._preprocessed[rpid] = ""
        return self._preprocessed[rpid]

    # ================================================================
    #  Similarity Matrix
    # ================================================================

    def build_matrix(self):
        """
        构建完整相似度矩阵 (NxN).

        优化:
        - 跳过长度 < 5 的评论 (太短无意义)
        - 跳过长度 > 500 的评论 (截断)
        - 限制最大比较数 (MAX_COMPARISONS)
        """
        texts = {}
        for c in self.comments:
            rpid = c.get("rpid")
            content = self.preprocess(c.get("content", ""))
            if len(content) < 5:
                continue
            texts[rpid] = content[:500]  # truncate
            self._preprocessed[rpid] = content

        rpids = list(texts.keys())
        total = len(rpids)
        compared = 0
        MAX_COMPARISONS = 50000  # safety: ~N=317 的完整矩阵

        for i in range(total):
            for j in range(i + 1, total):
                compared += 1
                if compared > MAX_COMPARISONS:
                    break

                ratio = difflib.SequenceMatcher(
                    None, texts[rpids[i]], texts[rpids[j]]
                ).ratio()
                if ratio >= self.threshold:
                    self.similarity_matrix[(rpids[i], rpids[j])] = ratio

            if compared > MAX_COMPARISONS:
                break

    # ================================================================
    #  Clustering (Union-Find)
    # ================================================================

    def find_clusters(self) -> list:
        """
        用 Union-Find 算法基于相似度矩阵找出评论组。

        Returns:
            [[rpid, rpid, ...], ...]  每个子列表是一个相似评论组
        """
        if not self.similarity_matrix:
            return []

        # Union-Find DS
        parent = {}

        def find(x):
            if x not in parent:
                parent[x] = x
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for (a, b), _ in self.similarity_matrix.items():
            union(a, b)

        # Group by root
        groups = defaultdict(list)
        for node in parent:
            groups[find(node)].append(node)

        # Filter: only groups with >= 3 similar comments are meaningful
        self.clusters = [
            members for members in groups.values() if len(members) >= 3
        ]
        # Sort by size desc
        self.clusters.sort(key=len, reverse=True)

        return self.clusters

    # ================================================================
    #  Per-user Scoring
    # ================================================================

    def get_user_similarity_score(self, mid: int) -> float:
        """
        计算某用户在相似集群中的参与度。

        规则: 该用户在相似集群中的评论数 / 该用户总评论数

        Returns:
            0.0 (无相似) ~ 1.0 (全部是模板)
        """
        user_comments = [
            c for c in self.comments if int(c.get("mid", 0)) == int(mid)
        ]
        if not user_comments:
            return 0.0

        user_rpids = {c["rpid"] for c in user_comments}

        # Count how many of user's comments are in clusters
        clustered_set = set()
        for cluster in self.clusters:
            for rpid in cluster:
                clustered_set.add(rpid)

        in_cluster = len(user_rpids & clustered_set)
        return in_cluster / len(user_rpids)
