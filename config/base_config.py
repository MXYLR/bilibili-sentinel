"""
核心配置模块 — MediaCrawler 风格配置中心

所有功能开关、平台参数、全局常量的集中管理。
参考: MediaCrawler/config/base_config.py
"""

import os

# ============================================================
#  项目根路径
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
#  平台标识
# ============================================================
PLATFORM = "bilibili"

# ============================================================
#  功能开关 (ENABLE_* 模式，参考 MediaCrawler)
# ============================================================

# IP代理池
ENABLE_IP_PROXY = False              # 是否启用IP代理池 (proxy/ 模块使用)

# Clash Verge 代理 (用于 requests 库直连 B站 API)
# 运行时配置可覆盖此默认值 (通过 Dashboard 设置页面修改)
CLASH_PROXY_ENABLED = True                  # 是否启用 Clash 代理
CLASH_PROXY_URL = "socks5://192.168.1.104:7897"  # Clash Verge SOCKS5 代理地址

# ★ 从运行时配置文件覆盖默认值 (Dashboard 设置页面写入)
import json, os as _os
_RUNTIME_CFG = _os.path.join(_os.path.dirname(__file__), "runtime_config.json")
if _os.path.exists(_RUNTIME_CFG):
    try:
        with open(_RUNTIME_CFG, "r", encoding="utf-8") as _f:
            _overrides = json.load(_f)
        for _k, _v in _overrides.items():
            if _k in ("CLASH_PROXY_URL", "CLASH_PROXY_ENABLED") and _v is not None:
                globals()[_k] = _v
    except Exception:
        pass  # 配置文件损坏时静默忽略

# CDP 浏览器反检测
ENABLE_CDP_MODE = True              # 是否启用CDP浏览器模式

# 数据采集
ENABLE_GET_COMMENTS = True           # 是否爬取评论
ENABLE_GET_SUB_COMMENTS = True       # 是否爬取子评论（楼中楼）

# 数据存储
SAVE_DATA_OPTION = "json"            # 存储后端: json, sqlite
ENABLE_CACHE_DEDUP = True            # 是否启用缓存去重

# 登录
SAVE_LOGIN_STATE = True             # 是否持久化登录态
LOGIN_TYPE = "qrcode"                # 登录方式: qrcode, phone, cookie

# LLM 水军分析
ENABLE_LLM_ANALYSIS = True          # 是否启用大语言模型语义分析

# AICU 深度分析
ENABLE_DEEP_ANALYSIS = False         # 是否启用 AICU 历史评论深度分析
AICU_COOKIE = ""                     # AICU 登录 Cookie (可选, 提升数据质量)

# 用户数据采集 (v2.1: F12-F14 数据源)
ENABLE_USER_CRAWL = True            # 是否启用用户空间数据采集 (画像+动态)
USER_CRAWL_MAX_USERS = 500           # 单次运行最大用户数
USER_CRAWL_MAX_POSTS = 50            # 每个用户最多采集动态条数

# ============================================================
#  B站 API 基础配置
# ============================================================
BILIBILI_API_BASE = "https://api.bilibili.com"
BILIBILI_REFERER = "https://www.bilibili.com"

# 请求限速 (B站限制约 3-5 req/s，保守设 3 req/s)
REQUEST_INTERVAL = 0.34              # 秒，约 3 req/s
COMMENT_PAGE_SIZE = 20               # B站评论API固定每页20条
COMMENT_MAX_PAGES = 100              # 每个视频最大评论页数
COMMENT_MAX_TOTAL = 10000            # 单个视频评论采集上限
COMMENT_DUAL_SORT = True             # 时间排序耗尽后自动切换热度排序
COMMENT_SUB_MAX_PAGES = 5            # 子评论最大翻页数
# [已废弃 v2.2] MAX_COMMENT_PAGES → COMMENT_MAX_PAGES, MAX_SUB_REPLIES → COMMENT_SUB_MAX_PAGES

# WBI 签名 — 混肴密钥表 (B站前端源码提取)
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]

# ============================================================
#  数据存储路径
# ============================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
COMMENT_DIR = os.path.join(DATA_DIR, "comments")
USER_DIR = os.path.join(DATA_DIR, "users")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# ============================================================
#  登录持久化路径
# ============================================================
BROWSER_DATA_DIR = os.path.join(DATA_DIR, "browser_data")
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.json")

# ============================================================
#  水军检测权重 (可调)
# ============================================================
DEFAULT_WEIGHTS = {
    # --- 核心身份特征 (v2.16: 18维, +F18签名引战, F4弱化为仅头像/认证) ---
    "f1_account_age":         0.06,  # 账号年龄: 注册<30天+评论多 → 新号水军 (0.07→0.06)
    "f2_follow_ratio":        0.01,  # 粉丝/关注比: 粉丝极少+关注极多 → 引流号
    "f3_level_score":         0.09,  # 用户等级: Lv0-2低等级水军概率高
    "f4_avatar_verify":       0.05,  # 头像/认证: 无头像+无认证 → "双无"账号 (0.08→0.05, 签名独立为F18)
    "f5_content_similarity":  0.08,  # 内容相似度: 评论与其他人高度雷同 → 模板化 (0.09→0.08)
    "f6_time_burst":          0.08,  # 时间爆发: 短时间集中刷评 → 操控迹象
    "f7_sentiment_extreme":   0.01,  # 情感极端: 100%正面或负面 → 非自然表达
    "f8_like_ratio":          0.04,  # 赞评比: 零赞评论 → 无人认同
    "f9_registration_batch":  0.01,  # 批量注册: 注册日期高度集中 → 工业号
    "f10_interaction_ring":   0.01,  # 互动小圈子: @提及集中在少数账号
    "f11_vip_anomaly":        0.03,  # 大会员异常: 低等级+VIP → 伪装嫌疑

    # --- 账号空间画像 ---
    "f12_account_skeleton":   0.15,  # 账号骨架: 无头像+用户名乱码+无动态+无投稿+默认签名
    "f13_lottery_repost":     0.05,  # 转发模式: 全转发动态(抽奖/投票/纯转发)
    "f14_sensitive_content":  0.10,  # 敏感内容: 历史动态含女拳/以乌/造谣抹黑
    "f15_commercial_spam":    0.10,  # 商业引流: 赌博/色情/加微信/刷单等硬广告

    # --- v2.10 新增: CleanX 行为模式分析 ---
    "f16_time_regularity":    0.04,  # 时间规律性: 低StdDev=机器人规律发帖
    "f17_self_similarity":    0.04,  # 自评相似度: 高重复率=模板复制

    # --- v2.16 新增: 签名引战检测 ---
    "f18_signature_troll":    0.05,  # 签名引战度: 个性签名含挑衅/嘲讽/引战话术
}

# 风险等级阈值 (v2.16: HIGH 70→60, 更积极捕获水军)
RISK_HIGH = 60
RISK_MEDIUM = 30
