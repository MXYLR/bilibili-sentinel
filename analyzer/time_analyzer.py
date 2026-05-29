"""
时间维度分析引擎

两个核心算法:
  1. 时间爆发检测 — 滑动窗口 + Z-score
  2. 注册日期集中度 — 按天统计 + 异常峰值检测
"""

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, stdev


class TimeAnalyzer:
    """
    评论时间模式分析器。

    输入:
      comments: CommentItem 列表 (含 ctime)
      users: {mid: UserInfoItem} 映射 (含 birthday, 可选)
    """

    def __init__(self, comments: list, users: dict = None):
        self.comments = comments
        self.users = users or {}
        self._comment_times = None  # cached sorted times

    # ================================================================
    #  Time Burst Detection
    # ================================================================

    def detect_time_burst(self, window_minutes: int = 10) -> dict:
        """
        时间爆发检测 — 滑动窗口 + Z-score。

        算法:
          1. 将评论按 ctime 排序
          2. 滑动窗口 (默认 10 分钟) 统计每个窗口评论数
          3. 计算均值 μ 和标准差 σ
          4. Z-score = (count - μ) / σ
          5. Z > 2.0 → 爆发窗口
          6. 对爆发窗口内的用户按评论占比打分

        Args:
            window_minutes: 滑动窗口大小 (分钟)

        Returns:
            {mid: burst_score}  分数范围 0.0 ~ 1.0
        """
        if not self.comments:
            return {}

        # Parse and sort
        times = []
        for c in self.comments:
            ctime = c.get("ctime", 0)
            try:
                times.append(datetime.fromtimestamp(ctime))
            except (ValueError, OSError):
                times.append(datetime.now())

        times.sort()

        if len(times) < 2:
            return {}

        # Sliding window
        window = timedelta(minutes=window_minutes)
        window_counts = []
        window_users = []  # list of sets of mids for each window

        t_start = times[0]
        t_end = times[-1]

        # Step through time
        current = t_start
        while current < t_end:
            current_end = current + window
            mid_set = set()
            for c in self.comments:
                ctime = c.get("ctime", 0)
                try:
                    ct = datetime.fromtimestamp(ctime)
                except (ValueError, OSError):
                    continue
                if current <= ct < current_end:
                    mid_set.add(int(c.get("mid", 0)))

            window_counts.append(len(mid_set))
            window_users.append(mid_set)
            current += window

        # Calculate stats
        if len(window_counts) < 2:
            return {}

        try:
            avg = mean(window_counts)
            std = stdev(window_counts) if len(window_counts) > 1 else 1
        except Exception:
            return {}

        if std == 0:
            return {}

        # Find burst windows
        burst_mids = defaultdict(float)
        for i, count in enumerate(window_counts):
            z_score = (count - avg) / std
            if z_score > 2.0 and count > 0:
                # This window is a burst
                for mid in window_users[i]:
                    burst_mids[mid] += min(1.0, z_score / 5.0)

        # Normalize to 0-1
        if burst_mids:
            max_score = max(burst_mids.values())
            if max_score > 0:
                for mid in burst_mids:
                    burst_mids[mid] = burst_mids[mid] / max_score

        return dict(burst_mids)

    # ================================================================
    #  Registration Date Batch Detection
    # ================================================================

    def detect_registration_batch(self, threshold_days: int = 3) -> dict:
        """
        注册日期集中度检测。

        算法:
          1. 从 users 提取每个用户的 birthday (B站注册日期)
          2. 按天统计注册人数
          3. 找出峰值天数 (人数 > 均值 + 2σ)
          4. 在峰值日期注册的用户 → 高分

        Args:
            threshold_days: 视为"同批"的天数范围

        Returns:
            {mid: batch_score}  分数范围 0.0 ~ 1.0
        """
        if not self.users:
            return {}

        # Extract registration dates
        reg_by_day = defaultdict(int)
        mid_to_day = {}

        for mid, user in self.users.items():
            birthday = user.get("birthday", "")
            if not birthday:
                continue

            try:
                # B站 birthday 格式可能不同，常见: "1970-01-01"
                if isinstance(birthday, str):
                    dt = datetime.strptime(birthday[:10], "%Y-%m-%d").date()
                elif isinstance(birthday, int):
                    dt = datetime.fromtimestamp(birthday).date()
                else:
                    continue
            except (ValueError, TypeError):
                continue

            reg_by_day[dt] += 1
            mid_to_day[mid] = dt

        if not reg_by_day:
            return {}

        counts = list(reg_by_day.values())
        if len(counts) < 2:
            return {}

        try:
            avg = mean(counts)
            std = stdev(counts)
        except Exception:
            return {}

        if std == 0:
            return {}

        # Find peak days
        threshold = avg + 2 * std
        peak_days = {day for day, cnt in reg_by_day.items() if cnt > threshold}

        if not peak_days:
            return {}

        # Score users registered on peak days
        result = {}
        peak_counts = {d: reg_by_day[d] for d in peak_days}
        max_count = max(peak_counts.values())

        for mid, day in mid_to_day.items():
            if day in peak_days:
                # Proportional score based on how concentrated the batch is
                result[mid] = min(1.0, peak_counts[day] / max_count)

        return result

    # ================================================================
    #  Comment Timeline (for visualization)
    # ================================================================

    def get_comment_timeline(self, bin_minutes: int = 30) -> list:
        """
        生成评论时间线 (供 Dashboard 图表使用)。

        Returns:
            [
              {"time": "2024-01-01 12:00", "count": 5},
              ...
            ]
        """
        if not self.comments:
            return []

        times = []
        for c in self.comments:
            ctime = c.get("ctime", 0)
            try:
                times.append(datetime.fromtimestamp(ctime))
            except (ValueError, OSError):
                times.append(datetime.now())

        times.sort()
        if not times:
            return []

        # Bin by interval
        binned = defaultdict(int)
        t_start = times[0].replace(second=0, microsecond=0)
        t_end = times[-1]

        current = t_start
        while current <= t_end:
            slot_key = current.strftime("%Y-%m-%d %H:%M")
            current_end = current + timedelta(minutes=bin_minutes)
            for t in times:
                if current <= t < current_end:
                    binned[slot_key] += 1
            current = current_end

        return [
            {"time": k, "count": v}
            for k, v in sorted(binned.items())
        ]
