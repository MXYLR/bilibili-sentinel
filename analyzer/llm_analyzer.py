"""
B站水军 LLM 分析器 — 多 Provider 集成 (DeepSeek / OpenAI / 自定义)

使用 OpenAI 兼容 API 对中高风险用户进行语义级水军识别。
与现有 14 特征评分引擎融合，提升检测精度。

支持的 Provider:
  - deepseek: DeepSeek V4 (deepseek-v4-pro / deepseek-v4-flash)
  - openai:   OpenAI (gpt-4o / gpt-4o-mini ...)
  - custom:   任意 OpenAI 兼容端点 (自部署模型等)

流程:
  1. 接收 scored_users (已有特征评分)
  2. 筛选 medium/high 风险用户
  3. 批量调用 LLM API 分析
  4. 解析返回的水军类型+置信度
  5. 融合: final_score = engine_score * 0.75 + llm_confidence * 0.25
  6. 返回增强后的用户列表 + AI 摘要
"""

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
#  Provider 默认配置
# ============================================================

PROVIDER_DEFAULTS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "default_model": "deepseek-v4-pro",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o3-mini"],
        "default_model": "gpt-4o-mini",
    },
}

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"


# API Key 加载优先级:
#   1. 环境变量 (DEEPSEEK_API_KEY 或 OPENAI_API_KEY)
#   2. 持久化配置文件 config/llm_config.json
LLM_API_KEY = ""
LLM_PROVIDER = DEFAULT_PROVIDER
LLM_MODEL = DEFAULT_MODEL
LLM_BASE_URL = ""

# 尝试从持久化配置文件加载
_cfg_path = Path(__file__).resolve().parent.parent / "config" / "llm_config.json"
_cfg = {}
if _cfg_path.exists():
    try:
        with open(_cfg_path, "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
    except Exception:
        pass

# 1) Provider
LLM_PROVIDER = _cfg.get("provider") or DEFAULT_PROVIDER

# 2) Base URL: 优先显式 base_url，否则从 provider 推导
LLM_BASE_URL = _cfg.get("base_url", "")
if not LLM_BASE_URL and LLM_PROVIDER in PROVIDER_DEFAULTS:
    LLM_BASE_URL = PROVIDER_DEFAULTS[LLM_PROVIDER]["base_url"]

# 3) Model: 优先配置文件，否则用 provider 默认
LLM_MODEL = _cfg.get("model") or DEFAULT_MODEL

# 4) API Key: 支持多种环境变量
_env_keys = []
if LLM_PROVIDER == "deepseek":
    _env_keys = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]
elif LLM_PROVIDER == "openai":
    _env_keys = ["OPENAI_API_KEY", "DEEPSEEK_API_KEY"]
else:
    _env_keys = ["OPENAI_API_KEY", "DEEPSEEK_API_KEY"]

for _key in _env_keys:
    LLM_API_KEY = os.environ.get(_key, "")
    if LLM_API_KEY:
        break

# 环境变量未设置时回退到配置文件
if not LLM_API_KEY and _cfg.get("enabled") and _cfg.get("api_key"):
    LLM_API_KEY = _cfg["api_key"]
    # 同步到环境变量供子进程使用
    os.environ["DEEPSEEK_API_KEY"] = LLM_API_KEY
    os.environ["OPENAI_API_KEY"] = LLM_API_KEY

# 成本控制
MAX_USERS_PER_BATCH = 5       # 每批最多 5 个用户
MAX_BATCHES_PER_RUN = 10      # 单次分析最多 10 批
LLM_TIMEOUT = 60              # API 超时
LLM_RETRY = 2                 # 重试次数

# 融合权重
ENGINE_WEIGHT = 0.75
LLM_WEIGHT = 0.25

# 筛选阈值：只对评分 >= 此值的用户调用 LLM (v2.8: 30→20)
LLM_SCORE_THRESHOLD = 20

# ============================================================
#  AICU 深度分析配置
# ============================================================
#  注意: 深度分析已改为手动触发 (Dashboard → 深度分析按钮)
#  DEEP_ANALYSIS_ENABLED 不再控制自动触发，
#  仅用于指示 AICU 是否已配置 (Dashboard 状态显示用)
#  手动触发时 deep_analyze() 不再检查此开关

# 深度分析开关（从 base_config 读取，默认关闭）
DEEP_ANALYSIS_ENABLED = False
# 深度分析阈值：只对引擎评分 >= 此值的用户做深度分析
DEEP_SCORE_THRESHOLD = 70
# 深度分析成本控制
DEEP_MAX_USERS = 10           # 单次最多深度分析 10 人
DEEP_USERS_PER_BATCH = 3      # 每批送 3 人（每人含历史数据，token 消耗大）
# 深度分析融合权重（覆盖原权重）
DEEP_ENGINE_WEIGHT = 0.50
DEEP_LLM1_WEIGHT = 0.25       # 初筛 LLM 权重
DEEP_LLM2_WEIGHT = 0.25       # 深度 LLM 权重
# AICU Cookie
AICU_COOKIE = ""

# 尝试从配置文件加载 AICU 配置
if _cfg_path.exists():
    try:
        with open(_cfg_path, "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        if _cfg.get("deep_analysis_enabled"):
            DEEP_ANALYSIS_ENABLED = True
        AICU_COOKIE = _cfg.get("aicu_cookie", "")
        if _cfg.get("deep_score_threshold"):
            DEEP_SCORE_THRESHOLD = int(_cfg["deep_score_threshold"])
    except Exception:
        pass

# 尝试从 base_config 覆盖（优先级更高）
try:
    from config import ENABLE_DEEP_ANALYSIS
    DEEP_ANALYSIS_ENABLED = ENABLE_DEEP_ANALYSIS
except ImportError:
    pass
try:
    from config import AICU_COOKIE as _aicu_cookie_cfg
    if _aicu_cookie_cfg:
        AICU_COOKIE = _aicu_cookie_cfg
except ImportError:
    pass


class LLMAnalyzer:
    """
    基于 LLM 的水军语义分析器（支持 DeepSeek / OpenAI / 自定义端点）。

    使用方式:
        analyzer = LLMAnalyzer(api_key="sk-xxx", provider="deepseek")
        enhanced = analyzer.analyze(scored_users, comments_data)
    """

    def __init__(
        self,
        api_key: str = None,
        provider: str = None,
        model: str = None,
        base_url: str = None,
    ):
        self.provider = provider or LLM_PROVIDER
        self.api_key = api_key or LLM_API_KEY
        self.base_url = base_url or LLM_BASE_URL

        # Model: 优先显式传入 > 全局配置 > provider 默认
        if model:
            self.model = model
        elif self.provider in PROVIDER_DEFAULTS and self.provider != LLM_PROVIDER:
            # 不同 provider：用该 provider 的默认模型
            self.model = PROVIDER_DEFAULTS[self.provider]["default_model"]
        else:
            self.model = LLM_MODEL

        self._total_calls = 0
        self._total_tokens = 0
        self._results_cache = {}  # mid -> LLM 结果

        # 延迟导入 openai (可选依赖)
        self._openai = None

    def _get_client(self):
        """懒初始化 OpenAI 兼容客户端"""
        if self._openai is None:
            if not self.api_key:
                logger.error(f"[LLM] Provider={self.provider}: 未配置 API Key")
                return None
            if not self.base_url:
                logger.error(f"[LLM] Provider={self.provider}: base_url 为空")
                return None
            try:
                import openai
                self._openai = openai.OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=LLM_TIMEOUT,
                )
                logger.info(
                    f"[LLM] 客户端已初始化: provider={self.provider}, "
                    f"model={self.model}, base_url={self.base_url}"
                )
            except ImportError:
                logger.error("openai 模块未安装，无法使用 LLM 分析。pip install openai")
                return None
            except Exception as e:
                logger.error(f"OpenAI 客户端初始化失败: {e}")
                return None
        return self._openai

    @property
    def is_available(self) -> bool:
        """LLM 是否可用"""
        return bool(self.api_key) and self._get_client() is not None

    # ============================================================
    #  主分析入口
    # ============================================================

    def analyze(
        self,
        scored_users: list,
        comments_data: list,
        video_info: dict = None,
        batch_size: int = None,
    ) -> dict:
        """
        对用户进行 LLM 水军分析 + 分数融合。

        Args:
            scored_users: WaterArmyScorer.score_users() 的输出
            comments_data: 该视频的全部评论列表
            video_info: 视频信息（可选，用于上下文）
            batch_size: 批大小（默认 MAX_USERS_PER_BATCH）

        Returns:
            {
                "enhanced_users": [...],    # 增强后的用户列表
                "llm_summary": "...",        # AI 总结
                "stats": {...},             # 分析统计
                "llm_available": bool,      # LLM 是否可用
            }
        """
        if not self.is_available:
            return {
                "enhanced_users": scored_users,
                "llm_summary": "LLM 分析未启用 (缺少 API Key)",
                "stats": {"llm_analyzed": 0, "llm_positive": 0},
                "llm_available": False,
            }

        batch_size = batch_size or MAX_USERS_PER_BATCH

        # ---- 1. 筛选需要 LLM 分析的用户 ----
        candidates = [
            u for u in scored_users
            if u.get("suspicious_score", 0) >= LLM_SCORE_THRESHOLD
        ]

        if not candidates:
            logger.info(f"[LLM] 无中高风险用户 (阈值={LLM_SCORE_THRESHOLD})，跳过")
            return {
                "enhanced_users": scored_users,
                "llm_summary": "所有用户风险评分较低，未触发 LLM 深度分析",
                "stats": {"llm_analyzed": 0, "llm_positive": 0},
                "llm_available": True,
            }

        # ---- 2. 准备用户分析数据 ----
        # 按 mid 组织评论
        comments_by_mid = {}
        for c in comments_data:
            mid = c.get("mid", 0)
            if mid not in comments_by_mid:
                comments_by_mid[mid] = []
            comments_by_mid[mid].append(c)

        # 限制批数
        limited_candidates = candidates[: batch_size * MAX_BATCHES_PER_RUN]
        if len(limited_candidates) < len(candidates):
            logger.warning(
                f"[LLM] 候选用户 {len(candidates)} 超过上限，"
                f"仅分析前 {len(limited_candidates)} 个"
            )

        # ---- 3. 分批调用 LLM ----
        all_results = {}
        total_batches = (len(limited_candidates) + batch_size - 1) // batch_size

        logger.info(f"[LLM] 开始分析 {len(limited_candidates)} 个用户 ({total_batches} 批)")

        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            end = min(start + batch_size, len(limited_candidates))
            batch_users = limited_candidates[start:end]

            # 构建用户数据
            users_payload = []
            for u in batch_users:
                mid = u.get("mid", 0)
                user_comments = comments_by_mid.get(mid, [])
                users_payload.append({
                    "mid": mid,
                    "uname": u.get("uname", ""),
                    "level": u.get("level", 0),
                    "comments": user_comments,
                    "features": u.get("features", {}),
                    "suspicious_score": u.get("suspicious_score", 0),
                    "sign": u.get("sign", ""),  # v2.16
                })

            # 调用 API
            batch_results = self._call_llm(users_payload)

            for r in batch_results:
                mid = r.get("mid", 0)
                all_results[mid] = r

            logger.info(
                f"[LLM] 批 {batch_idx + 1}/{total_batches} 完成 "
                f"({len(batch_results)} 结果)"
            )

            # 批次间短暂延迟
            if batch_idx < total_batches - 1:
                time.sleep(1.0)

        # ---- 4. 融合评分 ----
        enhanced_users = []
        llm_positive = 0

        # Bug #4 fix: 提前导入，避免在循环内重复导入
        from config import RISK_HIGH, RISK_MEDIUM

        for u in scored_users:
            mid = u.get("mid", 0)
            llm_result = all_results.get(mid)

            enhanced = dict(u)  # copy
            enhanced["llm_analysis"] = llm_result  # 可能为 None
            engine_score_raw = u.get("suspicious_score", 0)
            # ★ 归一化到 0-1（报告存 0-100，内部计算用 0-1）
            engine_score = engine_score_raw / 100.0 if engine_score_raw > 1.0 else engine_score_raw

            if llm_result and llm_result.get("type_id", 0) > 0:
                # 融合评分
                llm_confidence = llm_result.get("confidence", 0)
                fused = engine_score * ENGINE_WEIGHT + llm_confidence * LLM_WEIGHT

                enhanced["engine_score_raw"] = engine_score
                enhanced["suspicious_score"] = round(fused, 1)
                enhanced["llm_type_id"] = llm_result["type_id"]
                enhanced["llm_type_name"] = llm_result.get("type_name", "")
                enhanced["llm_confidence"] = llm_confidence
                enhanced["llm_reasoning"] = llm_result.get("reasoning", "")
            else:
                enhanced["llm_reasoning"] = ""
                # 引擎高分用户仍保留评分和风险等级提示
                if engine_score >= RISK_HIGH:
                    enhanced["risk_level"] = "high"
                elif engine_score >= RISK_MEDIUM:
                    enhanced["risk_level"] = "medium"

            enhanced_users.append(enhanced)

        # 按融合分数重新排序
        enhanced_users.sort(key=lambda x: x.get("suspicious_score", 0), reverse=True)

        # ---- 5. 生成 AI 摘要 ----
        llm_summary = self._generate_summary(enhanced_users, all_results, video_info)

        logger.info(
            f"[LLM] 分析完成: {len(limited_candidates)} 候选 → "
            f"{llm_positive} 水军确认, {self._total_calls} 次API调用"
        )

        return {
            "enhanced_users": enhanced_users,
            "llm_summary": llm_summary,
            "stats": {
                "llm_analyzed": len(limited_candidates),
                "llm_positive": llm_positive,
                "total_calls": self._total_calls,
            },
            "llm_available": True,
        }

    # ============================================================
    #  深度分析 — AICU 历史数据增强
    # ============================================================

    def deep_analyze(
        self,
        scored_users: list,
        comments_data: list,
        video_info: dict = None,
        threshold_override: float = None,
        progress_callback=None,
        log_callback=None,
    ) -> dict:
        """
        对高风险用户进行第二轮深度 LLM 分析（基于 AICU 历史数据）。

        流程:
          1. 筛选 engine_score >= threshold 的用户（上限 DEEP_MAX_USERS）
          2. 对每人调用 AicuFetcher 获取历史评论 + 画像
          3. 构建深度分析提示词（含初筛结果 + AICU 历史数据）
          4. 分批送 LLM 进行深度分析
          5. 三次融合: engine×0.50 + llm1×0.25 + llm2×0.25

        Args:
            scored_users: 已通过 LLM 初筛增强的用户列表
            comments_data: 评论数据（用于按 mid 关联）
            video_info: 视频信息
            threshold_override: 手动覆盖分数阈值（Dashboard 手动触发用），不传则用 DEEP_SCORE_THRESHOLD

        Returns:
            {
                "enhanced_users": [...],   # 深度增强后的用户列表
                "stats": {                 # 深度分析统计
                    "deep_analyzed": int,
                    "deep_confirmed": int,
                    "aicu_success": int,
                    "aicu_failed": int,
                },
                "deep_summary": str,       # 深度分析摘要
            }
        """
        # 注意: 深度分析已改为手动触发 (Dashboard 按钮)
        #       不再有自动触发路径，DEEP_ANALYSIS_ENABLED 不再阻断 deep_analyze()
        #       手动触发时 threshold_override 始终非 None
        if not self.is_available:
            logger.warning("[Deep] LLM 不可用，跳过深度分析")
            return {
                "enhanced_users": scored_users,
                "stats": {"deep_analyzed": 0, "deep_confirmed": 0},
                "deep_summary": "",
            }

        # ---- 1. 筛选高风险用户 ----
        _threshold = threshold_override if threshold_override is not None else DEEP_SCORE_THRESHOLD
        candidates = [
            u for u in scored_users
            if u.get("suspicious_score", 0) >= _threshold
        ]

        if not candidates:
            logger.info(f"[Deep] 无高风险用户 (阈值={_threshold})，跳过")
            return {
                "enhanced_users": scored_users,
                "stats": {"deep_analyzed": 0, "deep_confirmed": 0},
                "deep_summary": "",
            }

        # 限制数量
        limited = candidates[:DEEP_MAX_USERS]
        if len(limited) < len(candidates):
            logger.info(
                f"[Deep] 高风险用户 {len(candidates)} 超过上限，"
                f"仅深度分析前 {len(limited)} 个"
            )

        logger.info(
            f"[Deep] 开始 AICU 深度分析: {len(limited)} 个高风险用户 "
            f"(阈值≥{_threshold})"
        )
        if log_callback:
            log_callback("info", f"AICU 深度分析启动: {len(limited)} 个候选, 阈值≥{_threshold}")

        # ---- 2. AICU 数据抓取 ----
        aicu_data_map = {}  # mid -> AicuUserData
        aicu_success = 0
        aicu_failed = 0
        aicu_waf_blocked = False

        from analyzer.aicu_fetcher import AicuFetcher
        fetcher = AicuFetcher(cookie=AICU_COOKIE, timeout=15, log_callback=log_callback)

        for idx, u in enumerate(limited):
            mid = u.get("mid", 0)
            uname = u.get("uname", str(mid))
            if not mid:
                continue

            logger.info(f"[Deep] 抓取 AICU 数据: mid={mid} ({uname})")
            if log_callback:
                log_callback("info", f"[{idx+1}/{len(limited)}] 调用 AICU API: mid={mid} ({uname})")

            data = fetcher.fetch_all(mid)
            aicu_data_map[mid] = data

            if data.fetch_ok:
                aicu_success += 1
                if log_callback:
                    extra = []
                    if data.comment_count > 0:
                        extra.append(f"评论{data.comment_count}条")
                    if data.danmu_count > 0:
                        extra.append(f"弹幕{data.danmu_count}条")
                    if data.device_name:
                        extra.append(f"设备:{data.device_name}")
                    if data.history_names:
                        extra.append(f"曾用名:{len(data.history_names)}个")
                    detail = ", ".join(extra) if extra else "无有效数据"
                    log_callback("success", f"  AICU 返回 (mid={mid}): {detail}")
            else:
                aicu_failed += 1
                reason = "WAF拦截" if data.waf_blocked else (data.fetch_error or "API 无响应")
                if log_callback:
                    log_callback("warn", f"  抓取失败 (mid={mid}): {reason}")

            if progress_callback:
                progress_callback(aicu_success + aicu_failed, uname)

            if data.waf_blocked:
                aicu_waf_blocked = True

        logger.info(
            f"[Deep] AICU 抓取完成: 成功={aicu_success}, 失败={aicu_failed}"
        )

        # ---- 3. 构建深度分析用户数据 ----
        # 即使 AICU 失败，也允许纯 LLM 深度分析
        # 注意：aicu_data_map 可能不完整
        comments_by_mid = {}
        for c in comments_data:
            mid = c.get("mid", 0)
            if mid not in comments_by_mid:
                comments_by_mid[mid] = []
            comments_by_mid[mid].append(c)

        deep_candidates = []
        for u in limited:
            mid = u.get("mid", 0)
            user_info = dict(u)
            user_info["comments"] = comments_by_mid.get(mid, [])
            aicu_data = aicu_data_map.get(mid)
            deep_candidates.append((user_info, aicu_data))  # aicu_data 可以是 None

        # ---- 4. 分批深度 LLM 调用 ----
        from analyzer.aicu_prompts import DEEP_SYSTEM_PROMPT, build_deep_prompt

        deep_results = {}  # mid -> deep result
        batch_size = DEEP_USERS_PER_BATCH
        total_batches = (len(deep_candidates) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            end = min(start + batch_size, len(deep_candidates))
            batch = deep_candidates[start:end]

            if log_callback:
                log_callback("info", f"LLM 深度分析 批次 {batch_idx+1}/{total_batches} (共{len(batch)}人)")

            # 构建合并 prompt（每人独立区块）
            prompt_parts = [
                f"## 深度分析批次 {batch_idx + 1}/{total_batches}\n"
            ]
            for i, (user_data, aicu_data) in enumerate(batch, 1):
                prompt_parts.append(f"\n{'=' * 60}\n")
                prompt_parts.append(build_deep_prompt(user_data, aicu_data))

            full_prompt = "\n".join(prompt_parts)

            # 调用 LLM
            client = self._get_client()
            if client is None:
                logger.error("[Deep] LLM 客户端不可用，终止深度分析")
                if log_callback:
                    log_callback("error", "LLM 客户端不可用，深度分析终止")
                break

            batch_results = self._call_deep_llm(client, DEEP_SYSTEM_PROMPT, full_prompt)

            for r in batch_results:
                mid = int(r.get("mid", 0))
                deep_results[mid] = r

            logger.info(
                f"[Deep] 批 {batch_idx + 1}/{total_batches} 完成 "
                f"({len(batch_results)} 结果)"
            )
            if log_callback:
                type_counts = Counter(r.get("deep_type_name", "未知") for r in batch_results)
                summary = ", ".join(f"{k}:{v}" for k, v in type_counts.most_common(3))
                log_callback("success", f"  批次{batch_idx+1}完成: {summary}")

            if batch_idx < total_batches - 1:
                time.sleep(1.5)

        # ---- 5. 三次融合评分 ----
        enhanced_users = []
        deep_confirmed = 0

        # Bug #4 fix: 提前导入，避免在循环内重复导入
        try:
            from config import RISK_HIGH, RISK_MEDIUM
        except ImportError:
            RISK_HIGH, RISK_MEDIUM = 70, 40

        for u in scored_users:
            mid = u.get("mid", 0)
            deep_result = deep_results.get(int(mid)) or deep_results.get(str(mid)) or deep_results.get(mid)

            enhanced = dict(u)
            enhanced["deep_analysis"] = deep_result

            if deep_result:
                # ★ 只要 LLM 成功返回了深度分析结果，就标记为已分析
                #   deep_type_id=0 表示"正常用户/非水军"，也是有效的分析结果
                enhanced["deep_analyzed"] = True
                enhanced["deep_type_id"] = deep_result.get("deep_type_id", 0)
                enhanced["deep_type_name"] = deep_result.get("deep_type_name", "") or ("正常用户" if deep_result.get("deep_type_id", 0) == 0 else "")
                enhanced["deep_confidence"] = deep_result.get("deep_confidence", 0)
                enhanced["deep_reasoning"] = deep_result.get("deep_reasoning", "")
                enhanced["deep_key_evidence"] = deep_result.get("key_evidence", [])
                enhanced["deep_risk_confirmed"] = deep_result.get("risk_confirmed", False)

                # 附加 AICU 元数据（无论 type_id 是什么）
                aicu_info = aicu_data_map.get(mid)
                if aicu_info is not None:
                    enhanced["aicu_comment_count"] = aicu_info.comment_count
                    enhanced["aicu_stats"] = aicu_info.stats
                    enhanced["aicu_device"] = aicu_info.device_name
                    enhanced["aicu_names"] = aicu_info.history_names
                    if aicu_info.waf_blocked:
                        enhanced["aicu_waf_blocked"] = True

                # 三次融合 + 重新评估风险等级（只在判定为水军时）
                if deep_result.get("deep_type_id", 0) > 0:
                    engine_score = u.get("engine_score_raw", u.get("suspicious_score", 0))
                    llm1_confidence = u.get("llm_confidence", 0)
                    deep_confidence = deep_result.get("deep_confidence", 0)

                    fused = (
                        engine_score * DEEP_ENGINE_WEIGHT
                        + llm1_confidence * DEEP_LLM1_WEIGHT
                        + deep_confidence * DEEP_LLM2_WEIGHT
                    )

                    enhanced["suspicious_score"] = round(fused, 1)

                    if fused >= RISK_HIGH:
                        enhanced["risk_level"] = "high"
                    elif fused >= RISK_MEDIUM:
                        enhanced["risk_level"] = "medium"
                    else:
                        enhanced["risk_level"] = "low"

                    deep_confirmed += 1

            # 未被深度分析到但 AICU 抓取成功，也附加完整元数据
            elif mid in aicu_data_map and aicu_data_map[mid].fetch_ok:
                aicu_info = aicu_data_map[mid]
                enhanced["aicu_comment_count"] = aicu_info.comment_count
                enhanced["aicu_stats"] = aicu_info.stats
                enhanced["aicu_device"] = aicu_info.device_name
                enhanced["aicu_names"] = aicu_info.history_names
            elif mid in aicu_data_map:
                aicu_info = aicu_data_map[mid]
                if aicu_info.waf_blocked:
                    enhanced["aicu_waf_blocked"] = True

            enhanced_users.append(enhanced)

        # 按融合分数重新排序
        enhanced_users.sort(key=lambda x: x.get("suspicious_score", 0), reverse=True)

        # ---- 6. 生成深度分析摘要 ----
        deep_summary = self._generate_deep_summary(
            deep_candidates, deep_results, aicu_success, aicu_failed, video_info
        )

        logger.info(
            f"[Deep] 深度分析完成: {len(limited)} 候选 → "
            f"{deep_confirmed} 深度确认, "
            f"AICU 成功={aicu_success}/失败={aicu_failed}"
        )
        if log_callback:
            log_callback("success",
                f"深度分析完成: {len(limited)}候选 → {deep_confirmed}确认水军, "
                f"AICU成功={aicu_success}/失败={aicu_failed}")

        return {
            "enhanced_users": enhanced_users,
            "stats": {
                "deep_analyzed": len(limited),
                "deep_confirmed": deep_confirmed,
                "aicu_success": aicu_success,
                "aicu_failed": aicu_failed,
            },
            "deep_summary": deep_summary,
        }

    def _call_deep_llm(self, client, system_prompt: str, user_prompt: str) -> list:
        """
        调用 LLM 进行深度分析（含重试）。

        Returns:
            [{"mid": ..., "deep_type_id": ..., ...}, ...]
        """
        for attempt in range(LLM_RETRY + 1):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=4000,
                )

                self._total_calls += 1
                if hasattr(response, "usage") and response.usage:
                    self._total_tokens += response.usage.total_tokens

                content = response.choices[0].message.content

                # 解析 JSON
                from analyzer.llm_prompts import parse_llm_response
                raw_results = parse_llm_response(content)

                # 映射字段名: type_id → deep_type_id 等
                mapped = []
                for r in raw_results:
                    if not isinstance(r, dict) or "mid" not in r:
                        continue
                    mapped.append({
                        "mid": int(r.get("mid", 0)),
                        "deep_type_id": r.get("deep_type_id", r.get("type_id", 0)),
                        "deep_type_name": r.get("deep_type_name", r.get("type_name", "")),
                        "deep_confidence": r.get("deep_confidence", r.get("confidence", 0)),
                        "deep_reasoning": r.get("deep_reasoning", r.get("reasoning", "")),
                        "risk_confirmed": r.get("risk_confirmed", False),
                        "key_evidence": r.get("key_evidence", []),
                    })

                if mapped:
                    return mapped
                else:
                    logger.warning(f"[Deep] 解析结果为空 (attempt {attempt + 1})")

            except Exception as e:
                logger.error(f"[Deep] API 调用失败 (attempt {attempt + 1}): {e}")
                if attempt < LLM_RETRY:
                    time.sleep(2.0 * (attempt + 1))
                else:
                    return []

        return []

    def _generate_deep_summary(
        self,
        deep_candidates: list,
        deep_results: dict,
        aicu_success: int,
        aicu_failed: int,
        video_info: dict = None,
    ) -> str:
        """生成深度分析摘要"""
        total = len(deep_candidates)
        confirmed = sum(
            1 for r in deep_results.values()
            if r.get("deep_type_id", 0) > 0 and r.get("risk_confirmed")
        )
        suspicious = sum(
            1 for r in deep_results.values()
            if r.get("deep_type_id", 0) > 0 and not r.get("risk_confirmed")
        )

        from collections import Counter

        parts = [f"## AICU 深度分析摘要\n"]
        parts.append(
            f"对 **{total}** 名高风险用户进行 AICU 历史数据深度分析。\n"
        )
        parts.append(
            f"- AICU 数据抓取: 成功 {aicu_success} / 失败 {aicu_failed}\n"
        )
        parts.append(
            f"- 深度确认水军: **{confirmed}** 人\n"
        )
        if suspicious > 0:
            parts.append(
                f"- 高度可疑（待进一步确认）: {suspicious} 人\n"
            )

        # 类型分布
        type_counts = Counter()
        for r in deep_results.values():
            if r.get("deep_type_id", 0) > 0:
                type_counts[r.get("deep_type_name", "未知")] += 1

        if type_counts:
            parts.append("\n### 深度分析水军类型分布\n")
            for tname, count in type_counts.most_common():
                parts.append(f"- **{tname}**: {count} 例\n")

        parts.append(
            f"\n> 融合公式: 特征引擎×{int(DEEP_ENGINE_WEIGHT*100)}% "
            f"+ LLM初筛×{int(DEEP_LLM1_WEIGHT*100)}% "
            f"+ AICU深度×{int(DEEP_LLM2_WEIGHT*100)}%\n"
            f"> 分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        return "\n".join(parts)

    # ============================================================
    #  内部方法 (LLM 调用)
    # ============================================================

    def _call_llm(self, users_data: list) -> list:
        """
        调用 LLM API 分析一批用户。

        Returns:
            [{"mid": ..., "type_id": ..., ...}, ...]
        """
        from analyzer.llm_prompts import SYSTEM_PROMPT, build_user_prompt

        user_prompt = build_user_prompt(users_data)

        client = self._get_client()
        if client is None:
            return self._fallback_results(users_data)

        for attempt in range(LLM_RETRY + 1):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1 if len(users_payload) == 1 else 0.3,
                    max_tokens=2000,
                )

                self._total_calls += 1
                if hasattr(response, "usage") and response.usage:
                    self._total_tokens += response.usage.total_tokens

                content = response.choices[0].message.content
                logger.debug(f"[LLM Raw] {content[:300]}")  # ★ 调试 LLM 原始响应

                # 解析 JSON
                from analyzer.llm_prompts import parse_llm_response
                results = parse_llm_response(content)

                # 验证
                valid = [
                    r for r in results
                    if isinstance(r, dict) and "mid" in r
                ]
                # ★ 调试: 检查 reasoning 字段
                for r in valid:
                    if not r.get("reasoning") or len(str(r.get("reasoning", "")).strip()) < 5:
                        logger.warning(f"[LLM] mid={r.get('mid')} has empty/short reasoning: {r.get('reasoning','<NONE>')}")

                if valid:
                    return valid
                else:
                    logger.warning(f"[LLM] 解析结果为空，尝试重试 (attempt {attempt + 1})")

            except Exception as e:
                logger.error(f"[LLM] API 调用失败 (attempt {attempt + 1}): {e}")
                if attempt < LLM_RETRY:
                    time.sleep(2.0 * (attempt + 1))
                else:
                    return self._fallback_results(users_data)

        return self._fallback_results(users_data)

    def _fallback_results(self, users_data: list) -> list:
        """LLM 不可用时的回退结果"""
        return [
            {
                "mid": u.get("mid", 0),
                "type_id": 0,
                "type_name": "未分析",
                "confidence": 0,
                "reasoning": "LLM 服务不可用",
            }
            for u in users_data
        ]

    def _generate_summary(
        self,
        enhanced_users: list,
        llm_results: dict,
        video_info: dict = None,
    ) -> str:
        """生成 AI 分析摘要"""
        total = len(enhanced_users)
        high = sum(1 for u in enhanced_users if u.get("risk_level") == "high")
        medium = sum(1 for u in enhanced_users if u.get("risk_level") == "medium")
        positive = sum(1 for r in llm_results.values() if r.get("type_id", 0) > 0)

        # 按类型统计
        from collections import Counter
        type_counts = Counter(
            r.get("type_name", "未知") for r in llm_results.values()
            if r.get("type_id", 0) > 0
        )

        provider_label = {
            "deepseek": "DeepSeek V4",
            "openai": "OpenAI",
        }.get(self.provider, self.provider.title())

        parts = [f"## AI 水军分析摘要\n"]

        parts.append(
            f"本次共分析 **{total}** 名评论用户，其中 "
            f"**{high}** 名高风险、**{medium}** 名中风险。"
        )

        if positive > 0:
            parts.append(
                f"\nLLM 深度分析确认 **{positive}** 名用户存在水军特征。"
            )
            if type_counts:
                parts.append("\n### 水军类型分布\n")
                for tname, count in type_counts.most_common():
                    parts.append(f"- **{tname}**: {count} 例")

        else:
            parts.append(
                "\n经 LLM 深度语义分析，未发现明显的水军行为模式。"
                "现有评分主要基于统计特征，建议结合更多维度的数据进行判断。"
            )

        parts.append(
            f"\n> 分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"引擎: {provider_label} + 13维评分 | "
            f"融合权重: 特征{int(ENGINE_WEIGHT*100)}% + LLM{int(LLM_WEIGHT*100)}%"
        )

        return "\n".join(parts)


# ============================================================
#  快捷函数
# ============================================================

def create_llm_analyzer(
    api_key: str = None,
    provider: str = None,
    model: str = None,
    base_url: str = None,
) -> Optional[LLMAnalyzer]:
    """创建 LLM 分析器实例"""
    analyzer = LLMAnalyzer(
        api_key=api_key,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    if analyzer.is_available:
        return analyzer
    provider_name = provider or LLM_PROVIDER
    logger.warning(
        f"LLM 分析器不可用: Provider={provider_name}, "
        f"请设置 API Key 并确保配置正确"
    )
    return None
