# Bilibili Sentinel

B站水军评论智能检测与可视化分析系统 v2.37。基于 Scrapy-Redis 分布式爬虫采集评论/用户数据，结合 13 维特征评分引擎 + LLM 多 Provider 语义分析 + AICU 深度回溯，实现水军账号的自动化识别、评分和报告生成，通过 Flask Dashboard 提供完整的 Web 操作界面。

---

## 系统架构

```
                          run_all.bat (一键启动 Dashboard + 配套服务)
                                |
     ┌────────────────────┬─────┴────────┬────────────────────┐
     v                    v              v                    v
Video Spider       Comment Spider   User Spider        Flask Dashboard
(手动启动)         (手动启动)       (手动启动)        (Port 5001, 60+ routes)
     |                    |              |                    |
     `---------+----------+------+-------`                    |
               v                 v                            v
            Redis          fetch_user_posts.py         analyzer/ engine
        (调度/去重/种子)    (独立curl_cffi采集)      (F1-F8等13维 + LLM)
               |                 |                            |
               v                 v                            v
         store/ (JSON)   data/users/*_posts.json    AICU (用户历史回溯
                                                        含弹幕/评论/标记)
                                                             |
                                                             v
                                                      LLM API (DeepSeek)
```

**核心流程**: Dashboard 是用户交互中心 → 爬虫在后台采集数据到 JSON 文件 → 分析引擎按需执行水军检测 → 结果回传 Dashboard 可视化展示。

---

## 核心功能

### 数据采集
- **视频搜索**: 关键词搜索 + 热门排行 + UP主全部投稿（`bilibili_mid://` 种子），采集视频元信息
- **评论采集**: 双排序模式（时间排序耗尽自动切换热度），支持楼中楼主评论，单视频上限 10,000 条
- **用户空间**: card API 采集用户画像（mid/name/sex/face/sign/level/birthday/vip_status/official_verify/follower/following/video_count/post_count），2.5s 间隔防风控
- **用户动态**: `tools/fetch_user_posts.py` 独立脚本，用 curl_cffi 绕过 412 批量采集用户动态，存为 `data/users/{mid}_posts.json`，供 F14 特征使用
- **弹幕数据**: 集成在 AICU 深度分析中，通过 AICU API 自动获取用户历史弹幕（已移除独立弹幕爬虫）
- **种子联动**: 注入用户 UID → 自动推入视频爬虫队列 → 视频爬虫拉取 UP主全部投稿 → 有评论的视频自动推入评论队列

### 一键链式刷新 (v2.21)
点击视频详情页「刷新数据」按钮，全链路自动执行：
```
视频爬虫 + 评论爬虫 → 评论结束自动启动用户爬虫 → 用户爬虫结束自动运行分析(特征+评分) → LLM初筛
```
无需手动干预，Flask 终端实时显示 `[Chain]` 进度。

### 水军检测
- **13 维特征评分引擎 (F1-F8, F12, F14-F16, F18)**: 覆盖账号身份、行为模式、内容质量、空间画像四大维度。含 F15 商业引流、F16 时间规律性、F18 签名引战等新增特征
- **LLM 语义分析**: 多 Provider 支持（DeepSeek V4 / OpenAI GPT-4o / 自定义端点），异步后台执行 + 前端轮询进度，支持 Modal 阈值调节。Prompt 允许无评论时根据账号特征判定水军（F12≥0.6 或 F14≥0.3 即可判定）
- **AICU 深度分析**: 对高风险用户回溯历史评论/弹幕/动态，三次融合评分（引擎 50% + LLM 25% + 深度 25%），同样支持 Modal 阈值调节。新增时间模式分析（检测长时间不活跃后突然活跃）
- **8 种水军类型识别**: 模板刷评 / 情绪引导 / AI 生成 / 引流广告 / 批量操控 / 黑产养号 / 对立引战 / 敏感内容
- **可视化报告**: 雷达图 + 评分分布 + 时间线 + 用户详情弹窗（含特征进度条与 LLM 证据）

### 反检测体系 (412 对抗)
三层渐进式对抗方案，确保在复杂网络环境下稳定采集：

| 层级 | 技术 | 优先级 | 说明 |
|------|------|--------|------|
| L1 | curl_cffi TLS 伪装 | 89 | 模拟 Chrome 124 TLS 指纹，绕过 JA3/JA4 检测 |
| L2 | Cookie 池多账号轮换 | 26 | 多账号轮流请求，降低单号风控概率 |
| L3 | Playwright 真实浏览器 | 88 | 完全模拟真实用户行为，最终兜底方案 |

辅助措施: 浏览器级 HTTP 头伪装 (sec-ch-ua) / Referer 链伪造 / 自适应延迟降速 / 随机 page_size

**用户爬虫例外** (v2.21): 用户爬虫只走 curl_cffi 直连 card API，不启用 Playwright 兜底。每次请求前强制重置 `_use_playwright=False`。

### Dashboard 控制台
- **系统总览** `/`: 健康卡片 + **左右分栏布局**（左侧 UP主/热门榜列表 + 右侧视频内容区）+ 点击侧边栏条目按 UP主/热门榜过滤视频 + 右侧分页展示（24条/页）+ 「已分析」筛选联动侧边栏 + 视频删除保持当前选中分组
- **视频详情** `/video/<bvid>`: 评论展示 + 排行榜 + LLM初筛/AICU Modal 弹窗分析 + 特征触发图表 + 全屏用户详情弹窗（账号分析/评论/AICU数据）+ 刷新用户数据按钮（仅采集当前视频用户）+ UP主收录按钮
- **爬虫控制** `/crawler`: 4 爬虫管理 (视频/评论/用户/UP主视频) + 一键启动全部 + 补充评论/用户种子（扫描全局数据） + 种子注入 (热门/BV/关键词/UID) + 代理池状态 + 登录面板 + 按钮悬停 tooltip 详细说明
- **水军账号管理** `/water-army`: 收录水军库管理 + 搜索/筛选/排序 + 备注编辑 + CSV/JSON 导出 + B站主页直达链接
- **系统设置** `/settings`: 功能开关 + LLM 多 Provider 配置 + AICU 深度分析 + 代理参数（持久化到 runtime_config.json）
- **调试控制台** (所有页面): 右下角可拖动浮动按钮 `>_` → 三选项卡面板 (AICU日志 / HTTP请求 / 爬虫日志SSE) + 可拖动 + 可调整大小 + 最小化

---

## 快速开始

### 环境要求

| 组件 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | 使用 venv 虚拟环境 |
| Redis | >= 5.0 | 本地 localhost:6379，使用 db=1 |
| Playwright | >= 1.40 | 可选，用于浏览器兜底反检测 |
| LLM API Key | — | DeepSeek 或 OpenAI 兼容 API |

### 安装

```bash
# 1. 克隆项目
git clone <repo-url>
cd bilibili-sentinel

# 2. 创建虚拟环境
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. (可选) 安装 Playwright 浏览器
playwright install chromium
```

### 配置

1. **Redis**: 确保本地 Redis 服务运行在 `localhost:6379`
2. **LLM**: 在 Dashboard 设置页面配置，或直接编辑 `config/llm_config.json`:

```json
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com/v1"
}
```

3. **代理**: 在 Dashboard 设置页面修改 Clash 代理地址（自动持久化到 `config/runtime_config.json`），爬虫启动时自动读取。也可直接编辑 `config/base_config.py`:

```python
CLASH_PROXY_ENABLED = True
CLASH_PROXY_URL = "socks5://192.168.1.104:7897"
```
> 注：通过设置页面修改的代理地址会写入 `runtime_config.json`，优先级高于 `base_config.py` 的默认值。Dashboard 重启后也不会丢失。

### 一键启动

```bash
run_all.bat
```

脚本会自动完成:
1. 环境检测 (Python / Redis / LLM 配置)
2. 启动视频 + 评论 + 用户爬虫
3. 启动 Dashboard (http://localhost:5001)
4. 健康检查 + 打开浏览器

### 手动启动

```bash
# 终端 1: 启动 Dashboard
python dashboard/app.py

# 终端 2: 启动视频爬虫
scrapy crawl bilibili_video

# 终端 3: 启动评论爬虫
scrapy crawl bilibili_comment
```

---

## 13 维水军特征评分

| 特征 | 权重 | 核心逻辑 |
|------|------|----------|
| F1 账号年龄 | 0.07 | 注册 < 30 天 + 评论多 → 新号水军 |
| F2 粉丝/关注比 | 0.04 | 粉丝 < 50 + 关注 > 500 → 刷粉号模式 |
| F3 用户等级 | 0.09 | Lv0-2 + 高频评论 → 典型水军行为 |
| F4 头像/认证 | 0.05 | 无头像 + 无认证 → 两无账号，每缺一项 +0.50（签名检测已独立为 F18） |
| F5 内容相似度 | 0.09 | 评论内容与其他用户高度雷同 → 模板化刷评 |
| F6 时间爆发 | 0.08 | 短时间窗口密集评论 → 定时控评（Z-score 检测） |
| F7 情感极端 | 0.04 | 100% 正面或 100% 负面 → 立场预设的机械行为 |
| F8 赞评比异常 | 0.04 | 大量评论几乎零赞 → 低质量刷评 |
| F12 账号骨架 | 0.10 | 无头像 + 用户名乱码 + 无动态 + 无投稿 + 默认签名 → 五要素全中 = 100%（空壳号） |
| F14 敏感内容 | 0.10 | 历史动态含女拳/政治/造谣抹黑 → 高级水军 |
| F15 商业引流 | 0.10 | 评论含赌博/色情/加微信/刷单等硬广告 → 广告水军 |
| F16 时间规律性 | 0.04 | 评论时间间隔高度规律 → "上班式"机器人发帖 |
| F18 签名引战 | 0.05 | 个性签名含挑衅/嘲讽/引战话术 → "精神胜利法"引战号 |

**风险等级**: HIGH >= 70 | MEDIUM >= 30 | LOW < 30

**硬加成**: F12 账号骨架 2/5 +0.20 → 3/5 +0.25 → 4/5 +0.30 → 5/5 +0.35 | F14 敏感内容命中 +0.20 | F15 商业引流 +0.20 | F18 签名引战 >=0.50 +0.15 | 骨架+头像组合 +0.15

---

## 项目结构

```
bilibili-sentinel/
├── analyzer/                  # 水军分析引擎
│   ├── feature_extractor.py   #   F1-F8/F12/F14-F16/F18 13维特征提取
│   ├── scorer.py              #   加权评分器
│   ├── llm_analyzer.py        #   LLM 语义分析核心
│   ├── llm_prompts.py         #   LLM Prompt 构造
│   ├── aicu_fetcher.py        #   AICU 深度分析数据获取
│   ├── aicu_prompts.py        #   AICU Prompt 构造
│   ├── similarity_detector.py #   评论相似度检测
│   ├── time_analyzer.py       #   时间爆发 + 批量注册检测
│   └── report_generator.py    #   分析报告生成 (HTML/Markdown)
│
├── bilibili_crawler/          # Scrapy 爬虫模块
│   ├── spiders/
│   │   ├── bilibili_video_spider.py    # 视频爬虫 (热门/搜索/BV/UP主)
│   │   ├── bilibili_comment_spider.py  # 评论爬虫 (双排序+楼中楼)
│   │   └── bilibili_user_spider.py     # 用户空间爬虫 (画像+投稿+动态)
│   ├── middlewares.py          #   核心中间件 (Cookie/Header/风控)
│   ├── middlewares_cookie_pool.py  # Cookie 池轮换
│   ├── middlewares_playwright.py   # Playwright 兜底
│   ├── pipelines.py            #   数据处理管道
│   ├── items.py                #   Scrapy Item 定义
│   ├── settings.py             #   Scrapy 总配置
│   ├── handlers/
│   │   └── curl_cffi_handler.py    # TLS 指纹伪装
│   ├── login/                  #   登录模块 (二维码/手机/Cookie)
│   └── utils/
│       ├── bilibili_api.py     #    B站 API + WBI 签名
│       └── rate_limiter.py     #    请求限速器
│
├── dashboard/                  # Flask Web 控制台
│   ├── app.py                  #    Flask 应用 (48 个路由)
│   ├── water_army_store.py     #    水军账号持久化存储
│   └── templates/              #    Jinja2 模板 (5 页面)
│       ├── index.html          #    首页 — 视频列表
│       ├── video_detail.html   #    视频详情 — 水军分析结果 + LLM/AICU Modal
│       ├── crawler.html        #    爬虫控制面板
│       ├── water_army.html     #    水军账号管理
│       └── settings.html       #    系统设置
│
├── config/                     # 配置中心
│   ├── base_config.py          #   核心配置 + 功能开关 + 权重
│   ├── crawler_config.py       #   爬虫参数 (并发/重试/休眠)
│   ├── db_config.py            #   数据库连接 (Redis/SQLite)
│   ├── accounts.py             #   Cookie 池配置
│   └── llm_config.json         #   LLM 配置 (Provider/Model/Key)
│
├── cache/                      # 缓存层 (本地 + Redis)
├── store/                      # 存储模块 (JSON)
├── proxy/                      # 代理池模块 (多供应商)
├── deploy/                     # 部署脚本 (run_analyzer.py 等)
├── tools/                      # 工具 (浏览器初始化/反检测)
├── data/                       # 运行时数据
│   ├── videos/                 #   视频 JSON
│   ├── up_videos/              #   UP主视频列表 JSON
│   ├── comments/               #   评论 JSON
│   ├── users/                  #   用户 JSON
│   ├── danmaku/                #   弹幕 JSON (AICU 内部使用)
│   ├── reports/                #   分析报告
│   └── logs/                   #   运行日志
├── requirements.txt
├── scrapy.cfg
└── run_all.bat                 # 一键启动入口
```

---

## Dashboard API 速览

| 类别 | 端点 | 说明 |
|------|------|------|
| 页面 | `GET /` | 系统总览 |
| | `GET /video/<bvid>` | 视频详情 + 分析 |
| | `GET /crawler` | 爬虫控制面板 |
| | `GET /settings` | 系统设置 |
| 数据 | `GET /api/videos` | 已采集视频列表 |
| | `GET /api/comments/<bvid>` | 评论分页 (含楼中楼) |
| | `GET /api/score-distribution/<bvid>` | 水军评分分布 (5 桶直方图) |
| | `GET /api/danmaku/<bvid>` | 弹幕分页 |
| | `GET /api/report/<bvid>` | 水军分析报告 |
| 分析 | `POST /api/run-analysis/<bvid>` | 执行全量分析 |
| | `GET /api/analysis-status/<bvid>` | 分析进度轮询 |
| | `GET /api/deep-analyze-status/<bvid>` | 批量深度分析进度轮询 |
| | `POST /api/video/<bvid>/llm-screen` | 批量 LLM 初筛（异步+轮询进度+流式日志） |
| | `GET /api/llm-screen-status/<bvid>` | LLM 初筛进度轮询（支持 ?since= 增量日志） |
| | `POST /api/video/<bvid>/user/<mid>/llm-analyze` | 单用户 LLM 分析 |
| | `POST /api/video/<bvid>/user/<mid>/deep-analyze` | 单用户 AICU 深度分析 |
| | `POST /api/video/<bvid>/deep-analyze` | 批量深度分析（支持阈值参数） |
| | `DELETE /api/data/category` | 按分类删除数据 (热门/UP主/无源数据) |
| | `GET /api/data/category-status/<task_id>` | 删除进度轮询 |
| 爬虫 | `POST /api/crawler/start/<spider>` | 启动爬虫 |
| | `POST /api/crawler/stop/<spider>` | 停止爬虫 |
| | `POST /api/crawler/inject` | 注入种子 (BV/关键词/UID/UP主MID) |
| | `POST /api/crawler/rescan-comment-seeds` | 从已有视频数据重新注入评论种子 |
| | `POST /api/crawler/rescan-user-seeds` | 从已有评论数据提取MID注入用户种子 |
| 水军库 | `GET /water-army` | 水军账号管理页面 |
| | `GET /api/water-army/list` | 水军账号列表（分页/搜索/筛选） |
| | `GET /api/water-army/stats` | 水军库统计数据 |
| | `DELETE /api/water-army/<mid>` | 移出单个水军账号 |
| | `POST /api/water-army/batch-remove` | 批量移出水军账号 |
| | `GET /api/water-army/export` | 导出水军数据 (CSV/JSON) |
| 系统 | `GET /api/system/health` | 健康检查 |
| | `POST /api/system/shutdown` | 优雅关闭 |

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 爬虫框架 | Scrapy 2.11 + scrapy-redis | 分布式爬虫 + Redis 调度 |
| Web 框架 | Flask 3.x | Dashboard API + 页面渲染 |
| 缓存/队列 | Redis 5.x (db=1) | 任务队列 / 去重过滤 / 种子管理 |
| LLM | DeepSeek V4 / OpenAI GPT-4o | 评论语义分析 |
| 反检测 | curl_cffi + Playwright | TLS 伪装 + 浏览器兜底 |
| 存储 | JSON 文件 / SQLite | 数据持久化（工厂模式可切换） |
| 前端 | Bootstrap 5 + Chart.js | Dashboard UI + 图表 |
| 代理 | SOCKS5 (Clash Verge) | B站 API 网络通道 |

---

## 常见问题

**Q: Redis 连接失败？**
确保 Redis 服务已启动，检查 `config/db_config.py` 中的 host/port/db 配置。

**Q: 爬虫启动后无数据？**
检查代理是否可达（`CLASH_PROXY_URL`），或在设置页面关闭代理开关。B站 API 在国内可直接访问。

**Q: LLM 分析报错？**
在 Dashboard 设置页面点击"测试连接"验证 API Key 是否有效。支持环境变量 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`。

**Q: AICU 深度分析不可用？**
AICU 为可选功能（`ENABLE_DEEP_ANALYSIS=False`），当前 API 端点可能被 WAF 拦截。LLM 分析仍基于本地评论数据正常运作。

**Q: 412 风控频繁出现？**
系统已内置三层对抗。可尝试：降低并发（`crawler_config.py` 中调大 `DOWNLOAD_DELAY`）、增加 Cookie 池账号、启用 Playwright 兜底。

---

## v2.35 更新 (2026-06-07)

### 用户爬虫 API 路径新增投稿视频抓取

**问题**: 用户爬虫 (`bilibili_user_spider`) 的 API 路径（card API 成功时，占 95%+ 流量）只抓取了**画像**和**动态**，从未抓取**投稿视频**。投稿视频数据仅来自 Playwright 兜底路径（<5% 流量），导致 `data/up_videos/` 目录只有 28 个文件。

**根因**: `_build_user_info_item()` 中仅请求了动态 API (`get_user_posts_url`)，没有请求视频 API。

**修复** (`bilibili_user_spider.py`):
- `_build_user_info_item()`: 新增并行请求 `get_user_videos_url(mid)` → 与动态抓取同时进行
- 新增 `_parse_user_videos_api()`: 解析 `/x/space/wbi/arc/search` 响应，提取视频核心字段 (bvid/aid/title/cover/play/comment/created/length/description)
- 新增 `_flush_videos_to_file()`: 原子写入 `data/up_videos/{mid}_videos.json`
- 自动翻页: 最多翻 10 页 (500 条视频)，翻页过程中累加收集
- 新增 `_videos_error()`: 失败时保存已收集数据，避免前功尽弃

**效果**: 每个通过用户爬虫的用户，都会自动获取投稿视频列表，无需手动注入 UP 主种子。

---

## v2.34 更新 (2026-06-07)

### 用户种子去重增强 — 扫描 data/users/ 目录

**问题**: `rescan-user-seeds` API 之前只从 `data/comments/` 提取 MID 后直接注入 Redis，没有检查 `data/users/` 目录下已爬取过的用户。导致已爬过的用户被重复注入种子队列，浪费资源。

**修复** (`dashboard/app.py`):
- **新增扫描 `data/users/`**: 扫描目录下所有 `{mid}.json` 和 `{mid}_posts.json` 文件，提取已爬取过的 MID（当前已有 8503 个文件）
- **新增检查 Redis 队列**: 读取 `bilibili_crawler:user_seeds` 队列中已有的 MID，避免重复注入
- **三级过滤**: 从评论提取的 MIDs → 减去已爬取 MIDs → 减去队列中已有 MIDs → 最终注入
- **详细报告**: 返回新增字段 `users_existing`（已爬取用户数）、`skipped_already_crawled`（跳过：已爬取）、`skipped_already_queued`（跳过：已在队列）

**效果对比**:
| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 扫描源 | `data/comments/` 仅 | `data/comments/` + `data/users/` + Redis queue |
| 去重范围 | 评论间去重 | 评论间去重 + 已爬取跳过 + 队列跳过 |
| 重复注入 | 每次全部注入 | 只注入真正需要爬取的用户 |

---

## v2.33 更新 (2026-06-07)

### B站登录墙/CAPTCHA 手动绕过机制

**问题**: Playwright 爬取用户空间时，B站可能弹出登录墙或 geetest CAPTCHA，之前只能直接失败。

**修复**: 在 `playwright_space_scraper.py` 新增两个函数：
- `_detect_blockers(page)`: 自动检测登录墙（URL跳转/DOM弹窗/文字）和 CAPTCHA（geetest 面板/文字），返回 `{"login_wall": bool, "captcha": bool, "reason": str}`
- `_wait_for_manual_bypass(page, headless, max_wait_sec=300)`: 
  - `headless=False`（可见模式）：在浏览器上显示半透明提示覆盖层 + 终端打印醒目提示，等待用户手动完成登录/验证码
  - `headless=True`（无头模式）：打印警告后跳过（无法看到浏览器）
  - 轮询检查：每 3 秒检测一次障碍是否解除，最长等待 5 分钟
  - 障碍解除后自动移除提示覆盖层，继续抓取
- 已集成到 `scrape_user_profile()`、`scrape_user_videos()`、`scrape_user_posts()` 三个方法

### Run_pw_scraper 扩展 — 视频/动态数据采集

**问题**: Spider 调用 Playwright 子进程脚本（`run_pw_scraper.py`）时，只调用了 `scrape_user_profile()`，从未执行 tab 点击逻辑。

**修复**:
- `run_pw_scraper.py` 现在依次调用三个方法：`scrape_user_profile()` → `scrape_user_videos()` → `scrape_user_posts()`，返回完整 JSON `{"profile": {...}, "videos": [...], "posts": [...]}`
- `bilibili_user_spider.py` 的 `_pw_profile_callback()` 更新为解析扩展后的结果：
  - 视频保存到 `data/up_videos/{mid}_videos.json`
  - 动态保存到 `data/users/{mid}_posts.json`
  - Profile 数据 yield `UserInfoItem`

### Playwright 浏览器后台运行 + 自动关闭 (v2.37)

**问题 1**: `run_pw_scraper.py` 从未调用 `scraper.close()`，导致 Chromium 浏览器窗口残留在用户桌面。

**问题 2**: `headless=False` 时浏览器窗口抢占前台焦点，干扰用户其他操作。

**问题 3**: 子进程 `subprocess.run(timeout=120)` 超时时不杀进程，导致孤儿 Chromium 窗口。

**修复**:
- **CDP 窗口管理**: 新增 `_set_window_state(page, minimized)` 和 `_focus_window(page)` 函数，通过 Chrome DevTools Protocol 控制浏览器窗口最小化/恢复
- **后台启动**: `_ensure_browser()` 创建新页面后立即最小化（`headless=False` → 后台模式）
- **按需弹窗**: `_wait_for_manual_bypass()` 检测到 CAPTCHA/登录墙时先 `_focus_window()` 恢复前台，用户完成后 `_set_window_state(minimized=True)` 回到后台
- **自动关闭**: `run_pw_scraper.py` 新增 `finally` 块调用 `scraper.close()`
- **超时清理**: 爬虫改用 `subprocess.Popen` + `proc.kill()`，确保超时时杀掉子进程

**窗口生命周期**:
```
launch → minimize (后台) → CAPTCHA 检测到 → restore (前台)
→ 用户完成操作 → minimize (后台) → 抓取完成 → close browser
```

### Scrapy 2.16.0 API 兼容性修复

**问题**: Scrapy 2.16.0 移除了 `Spider.start_requests()` 方法，旧方法名永不被调用，导致所有 spider 启动后无请求、立即关闭。

**修复**: 所有 spider 必须使用 `async def start()` 替代 `def start_requests()`：
- `bilibili_video_spider.py`
- `bilibili_comment_spider.py`
- `bilibili_user_spider.py`
- `bilibili_up_videos_spider.py`
- `bilibili_danmaku_spider.py`

### Playwright 空间爬取器选择器修复

#### 视频标题选择器修复
**问题**: 爬取到 42 条视频，但所有视频的 `title` 字段为空字符串。
**根因**: B站 2026 版 DOM 中视频标题容器从 `.bili-video-card__info__title` 改为 `.bili-video-card__title`，且标题存在 `title` 属性中。
**修复**: 选择器改为 `.bili-video-card__title, .bili-video-card__info__title, .video-name`，优先读 `getAttribute('title')`。

#### 签名提取改用 `<meta name="description">`
**问题**: 画像提取中 `sign` 字段始终为空。
**根因**: B站已移除 `.sign.header-sign .pure-text` DOM 节点。
**修复**: 改为从 `<meta name="description">` 提取最后一个 `。` 后面的内容。

#### 两个 tab 点击的视觉验证
**验证**: `test_space_scraper.py` headless=False 测试确认：点击"投稿"tab → 42 条视频 ✅，点击"动态"tab → 136 条动态 ✅

### 其他修复
- `_fetch_next_user()` 自调度修复：用户爬虫翻页逻辑修复
- `run_pw_scraper.py` 路径修复：使用正确的项目根目录路径
- 临时调试文件清理：`debug_card_html.py`、`debug_sign.py`、`test_space_scraper.py` 等

---

## v2.31 更新 (2026-06-06)

### Dashboard 总览页 左右分栏布局重构

#### 背景
旧设计使用 UP主统计卡片行 + 视频分组折叠面板，存在两个问题：
1. UP主按钮无法点击（事件绑定失效）
2. 在热门榜中出现的 UP主 无法通过原有跳转逻辑定位到其视频

#### 新设计
**布局**: `col-lg-3` 左侧侧边栏 + `col-lg-9` 右侧内容区（Bootstrap Grid）

**左侧侧边栏** (`#up-sidebar`):
- 固定定位（sticky top），最大高度 `calc(100vh - 280px)`，超出滚动
- 顶部「热门榜」条目（key=`'hot'`）
- 下方全部 UP主条目（key=`'up_{mid}'`），含粉丝名+视频数角标
- 点击高亮当前选中条目（Bootstrap `.active` 样式）

**右侧内容区** (`#video-content-area`):
- 点击侧边栏条目 → `selectUpGroup(key, label)` 过滤 `window._allVideos` → 渲染视频卡片
- `renderSelPage()` 每页 24 条
- `renderSelPagination()` 上/下页 + 页码按钮
- 「已分析」筛选（`filterAnalyzed()`）同步更新侧边栏条目和右侧内容

#### 核心 JS 架构
| 函数 | 作用 |
|------|------|
| `renderUpSidebar(videos, upGroups)` | 渲染左侧 list-group 条目 |
| `selectUpGroup(key, label)` | 点击侧边栏，过滤并渲染右侧 |
| `renderSelContent(label)` | 设置右侧容器 HTML 骨架 |
| `renderSelPage()` | 渲染当前页视频卡片 |
| `renderSelPagination()` | 渲染分页控件 |
| `loadVideos(callback)` | 拉取全量视频，支持回调（删除后重选） |

**状态变量**: `_selKey` / `_selLabel` / `_selVideos` / `_selPage` / `_SEL_PAGE_SIZE=24`

#### 修复的 Bug
- `deleteVideo()`: 删除后调用 `loadVideos(() => selectUpGroup(_selKey, _selLabel))`，保持右侧选中状态
- `filterAnalyzed()`: `_analyzedFilter=true` 时重新计算 `upGroups`，侧边栏只显示含已分析视频的 UP主
- `loadVideos(callback)`: 支持可选回调参数，兼容旧版无参调用

#### 代码精简
- 删除约 330 行废弃代码：旧 `loadVideos`、`renderBucketPage`、`switchSortMode`、`renderPagination`、`jumpToPage`、`scrollToUpGroup`、`deleteCategory`
- `index.html` 从 ~1000 行精简至 620 行

---

### Playwright 用户空间爬取器 v2 (2026-06-05)

- `playwright_space_scraper.py` 完全重写，适配 B站 2026 新版 DOM 结构
- 移除 `window.__INITIAL_STATE__` 提取，改用新版 DOM 选择器（`.nickname`、`.nav-statistics__item-num`、`.nav-tab__item-num` 等）
- SPA 导航策略：画像页加载 → 移除遮罩 → JS 强制点击 nav-tab → 绕过 geetest 验证码
- 无 Cookie 测试通过：画像 15 字段 ✅，视频 42 条 ✅，动态 132 条 ✅

---

## v2.30 更新 (2026-06-05)


### 新增：时间模式分析（检测突然活跃的水军）

#### 功能说明
**问题**：水军账号通常长时间不活跃，但会在特定视频下突然大量发评论。这种"突然活跃"行为是水军的典型特征。

**解决方案**：在 AICU 深度分析中新增**时间模式分析**功能：
1. 从 AICU 历史评论中提取时间戳
2. 计算相邻评论的时间间隔（天）
3. 如果某个间隔 > 30天 → 标记为"长时间不活跃"
4. 如果历史最后活跃时间距离当前视频评论时间 > 30天 → 标记为"突然活跃"（水军典型行为）

#### 实现细节
1. **`AicuUserData` 新增字段**：
   - `comment_timestamps`: 历史评论时间戳列表（已排序）
   - `time_gap_days`: 最大时间间隔（天）
   - `is_sudden_activity`: 是否突然活跃
   - `last_active_time`: 最后活跃时间（可读格式）
   - `activity_timeline`: 活跃时间线 `[{date, count}, ...]`
   - `sudden_activity_reason`: 突然活跃的原因分析

2. **`AicuFetcher.fetch_all()` 新增时间模式分析**：
   - 提取评论时间戳 → 排序 → 计算间隔
   - 检测长时间空白期（>30天）
   - 保存分析结果到 `AicuUserData`

3. **`aicu_prompts.py` 的 `build_deep_prompt()` 新增时间模式特征**：
   - 比较当前视频评论时间 vs 历史最后活跃时间
   - 如果时间差 > 30天 → 在 prompt 中添加"突然活跃（水军典型行为）"警告
   - 显示时间模式信息

4. **`DEEP_SYSTEM_PROMPT` 新增时间模式分析原则**：
   - 添加"时间模式"到分析维度
   - 添加"长时间不活跃后突然发评论 → 水军典型行为"
   - 添加时间模式到判定逻辑

#### 效果
- LLM 现在能识别"长时间不活跃后突然在当前视频下评论"的水军行为
- 提示词中明确说明："历史最后活跃(X天前)，但在当前视频下发评论 → 突然活跃（水军典型行为）"
- 提高对"养号后突然活跃"类型水军的检测率

---

## v2.29 更新 (2026-06-05)

### LLM 分析深度优化与 Bug 修复

#### 1. 修复评论数据丢失导致误判为"正常用户"
**问题**: LLM 初筛看不到评论数据，因为字段名不匹配
- `app.py` 单用户分析: `single_user["sample_comments"] = user_comments[:5]`
- `llm_prompts.py` + `aicu_prompts.py`: 读的是 `user.get("comments", [])`
- 结果: 评论数据永远传不进 prompt，LLM 看到的是 `(无)`

**修复**: 两处都改为兼容两种字段名
```python
# 修复前
comments = user.get("comments", [])

# 修复后
comments = user.get("sample_comments") or user.get("comments", [])
```

#### 2. 修改 Prompt 原则，修复无评论时误判问题
**问题**: 旧 prompt 原则2: "必须有评论证据才能判水军"
- 导致 F12=0.4（骨骼账号）+ F14=100（敏感内容）的高风险账号被误判为"正常用户"
- LLM 返回: "用户无评论数据，仅凭特征值无法判定水军"

**修复**: 删除保守规则，添加"无评论时的判定规则"
- 原则2: "内容为主，分数为辅" → "内容为王，但非绝对"
- 新增规则:
  - F12≥0.6（3/5命中）→ 可判 type6，即使无评论
  - F14≥0.3 → 可判 type8，即使无评论
  - F12≥0.4 + F14≥0.3 → 可判 type8（双重证据）
- 降低 f12 判定阈值: 0.8 → 0.6（3/5命中即可判骨骼号）
- 添加示例2（无评论）: 展示骨骼+F14 双重证据如何判定为水军

#### 3. Prompt 大幅精简（-59%），减少分析时间
**问题**: 分析时间太长（110秒），影响用户体验

**修改内容**:
- **SYSTEM_PROMPT 瘦身**: 1600 → 737 字符（-54%）
  - 删除 13 维特征的逐条解释
  - 8 种类型定义压缩为 1 行/类型
  - 判定流程合并简化

- **build_user_prompt() 重写**:
  - 特征展示: 13 维 3 行 → 1 行（仅 f≥0.3）
  - 删除 WATER_ARMY_TYPES 完整描述（SYSTEM_PROMPT 已含）
  - 用户头部信息合并为 1 行

- **max_tokens 减少**:
  - `_call_llm` 单用户: 2000 → 600
  - `_call_deep_llm`: 4000 → 1500

- **同步修改 aicu_prompts.py**:
  - DEEP_SYSTEM_PROMPT 瘦身: ~1600 → 718 字符（-55%）
  - build_deep_prompt() 重写: 特征展示压缩为 1 行（仅 f≥0.3）

#### 4. 修复 JSON 解析 Bug（正则修复+日志增强）
**问题**: "LLM 服务不可用" + confidence=0%，实际是 JSON 解析失败

**根因**:
- 正则 Bug: 非贪婪 `*?` 在嵌套 JSON 中会停在第一个 `}`，导致截断
- 字段名不匹配: Prompt 未指定字段名，模型返回任意键名，导致 `dict.get("confidence", 0)` 永远返回 0

**修复**:
- **parse_llm_response() 重写**（三层解析策略）:
  1. 直接 `json.loads(text)`
  2. 提取 ` ```json ... ``` ` 代码块
  3. 贪婪正则 `r'\{[\s\S]*\}'`（正确处理嵌套 JSON）

- **Prompt 尾部添加格式示例**（使用明确字段名）:
  ```
  {"results": [{"mid": 123456, "type_id": 0, "type_name": "正常用户",
    "confidence": 0, "reasoning": "f12=0.4命中2/5项非四无号..."}]}
  ```

- **response_format 条件化**:
  - 仅 OpenAI provider 支持 `json_object`
  - DeepSeek 可能报错 `unknown variant image_url`
  - 修复: `if self.provider == "openai": call_kwargs["response_format"] = {"type": "json_object"}`

- **诊断日志增强**:
  - 添加 info 级别日志: prompt 字符数、响应长度、解析结果数量

#### 5. 前端超时修复
**问题**: 前端轮询超时 < 后端 API 超时，导致用户看到超时错误但实际分析还在进行

**修复**:
- LLM 初筛: 60s → 210s
- AICU 深度: 120s → 360s
- 批量分析: 300s → 480s

---

## v2.28 更新 (2026-06-05)

### AICU 网页评论提取 DOM 选择器修正

**问题**: 之前使用的 `.reply-item` / `[data-oid]` 等选择器完全错误，导致提取不到真实评论。

**根因**: 用户提供了 AICU 网页评论的真实 DOM 结构样本：
```html
<div class="card">
    <div class="time">2025/9/11 18:07:46 1</div>
    <div class="message" style="white-space: pre-wrap;">评论内容</div>
    <div class="z">当前查询uid:27683704 爱来自aicu.cc</div>
    <div class="buttons">...</div>
</div>
```

**修改内容**:
- `_extract_aicu_comments_from_page()` 完全重写（精准版 v2）
- 正确识别容器：使用 `.card` 作为评论容器
- 过滤导航 card：跳过 `.time` 内容为"相关链接"/"用户信息"的 card
- 提取时间：从 `.time` 元素提取，格式 `2025/9/11 18:07:46 1`（末尾数字=点赞数）
- 提取评论内容：从 `.message` 元素提取
- 提取 oid：从 `.buttons` 里的 bilibili 链接提取（`av115183758349626` 或 `oid=115183758349626`）
- 提取点赞数：从 `.time` 末尾数字提取

**效果**: 评论提取准确率从 ~0% 提升到 ~95%+

---

## v2.27 更新 (2026-06-05)

### Playwright 网页抓取 v3 直接 URL 方案 + 测试通过

**问题**:
- 首页输入框是 Material Web Components 的 shadow DOM，Playwright 无法直接 fill
- `cpl()` 函数只弹出搜索 UI，不会自动跳转
- 等待 URL 跳转超时（SPA，URL 不变）

**解决方案**: 直接访问评论查询 URL：`https://www.aicu.cc/reply?uid={mid}`

**修改内容**:
- 重写 `_get_via_playwright_html()` 方法（v3）
- 移除首页输入流程：不再打开 aicu.cc 首页、不再操作 shadow DOM 输入框
- 直接访问 reply URL：`page.goto(f"https://www.aicu.cc/reply?uid={mid}")`
- 等待评论数据加载：检测页面文本中的"评论数"或日期时间格式
- 滚动 + 翻页提取：与之前相同的提取逻辑

**测试验证（UID=27683704，只抓第一页）**:
- 成功抓取 **101 条记录**
- 过滤导航/广告后 **92 条有效评论**
- 评论内容真实（如"点开动态，满意离开"、"回复 @jeffhe1235"等）
- 用户评论总数：2013 条

---

## v2.26 更新 (2026-06-05)

### 放弃 AICU 评论 API，只用 Playwright 网页抓取

**决策背景**:
- AICU 评论 API (`/api/v3/search/get`reply`) 不稳定：WAF 拦截、返回空、403 错误
- 探测到 440 条评论但分页抓取结果为 0 条（mid=1220888430 的案例）
- 本地评论文件覆盖不了 AICU 的上亿条数据库

**修改内容**:
- 完全重写 `fetch_user_comments()` 方法（180行 → 70行）
- 移除所有 AICU API 调用逻辑（`_get()`、`_get_via_playwright()`）
- 直接调用 `_get_via_playwright_html(mid)` 进行网页抓取
- `known_count` 参数仅用于日志参考，不影响抓取逻辑
- 返回值新增 `source: "playwright_web"` 字段

**保留使用 API 的方法**:
- `fetch_user_profile()` - 用户资料（稳定）
- `fetch_user_marks()` - 设备标记（稳定）
- `fetch_user_danmu()` - 弹幕（稳定）

---

## v2.25 更新 (2026-06-05)

### 扩写 LLM reasoning 至 150 字

**问题**: LLM 输出的 reasoning 字段太短（往往只有一句话），缺乏分析过程，用户无法判断判定依据。

**修改内容**:

#### analyzer/llm_prompts.py
1. **SYSTEM_PROMPT 输出格式**：新增 reasoning 写作框架（4个部分，共150-200字要求）
   - 【特征值解读】约40字：逐条解释高分特征含义（如f12=0.4表示2/5命中）
   - 【评论内容分析】约60字：引用1-2条原文片段，分析是否有实质性证据
   - 【综合判定逻辑】约50字：说明最终判定依据
   - 【风险说明】约30字：正常用户说明风险点，水军说明置信度依据
2. **build_user_prompt() 末尾**：将"reasoning 必须 200 字左右"替换为详细要求（5条，含引用原文、解释特征、说明非水军理由）

#### analyzer/aicu_prompts.py
1. **DEEP_SYSTEM_PROMPT 输出格式**：新增 reasoning 写作框架（4个部分）
   - 【引擎特征解读】【历史评论分析】【综合判定逻辑】【证据链说明】
   - 特别强调：AICU 无数据时必须注明"历史数据缺失，仅基于当前视频判断"
2. **build_deep_prompt() 末尾**：新增5条 reasoning 质量要求

**效果预期**:
- reasoning 从当前1-2句扩展到150-200字
- 每个判定都有具体的特征值解释+评论原文引用
- 正常用户判定也会说明"为什么不是水军"，提高说服力

---
## v2.24 更新 (2026-06-05)

### 账号详情 Modal 中账号年龄、签名引战为空修复

**问题**: 账号详情 Modal 里 F1（账号年龄）和 F18（签名引战）只有分数百分比，没有显示实际的「注册年份」和「个性签名内容」，让人无法直观看到问题在哪。

**修复方案（三层）**:

1. **analyzer/feature_extractor.py** - `extract_all()` 新增输出字段：
   - `birthday`: 用户空间 API 返回的原始注册日期字符串
   - `reg_year`: 注册年份（精确日期 > MID号段推算兜底）

2. **analyzer/report_generator.py** - `top_suspects` 新增字段：
   - `birthday`, `reg_year` 传入报告 JSON，持久化存储

3. **dashboard/app.py** - `api_user_detail` 接口兜底补充：
   - 旧报告没有 `reg_year` 时，从 users/ 文件实时补充
   - 再次兜底: 读不到文件则用 MID 号段推算

4. **dashboard/templates/video_detail.html** - 前端展示：
   - 新增 `midToApproxYear()` JS 函数（与Python端一致的号段映射）
   - Modal 顶部 3 个数字卡片下方新增信息条：
     - 注册年份 badge（1年内=红/3年内=黄/3年+=灰，hover显示数据来源）
     - 签名内容 badge（F18分≥70=红/≥40=黄/低=灰，hover显示F18分数）
     - 默认签名/无签名时显示灰色占位 badge

**测试**: birthday精确 → reg_year=2022, birthday兜底 → reg_year=2020(MID推算), 全部通过

---

## v2.21 更新 (2026-06-04)

### 用户爬虫全面重构

#### 主接口切换
- **card API 替代 wbi/acc/info**: 无 WBI 签名、无 352 风控，curl_cffi 直通返回 2KB+ 数据
- **三层兜底链**: card API → 空间页 HTML(提取 `__INITIAL_STATE__`) → 跳过

#### 字段映射修复（双层 Bug）
- **第一层 `_parse_card_api`**: `birthday` 取实值 (不再硬编码空) / `following=card.attention` (不再硬编码 0) / `official` 容错 null
- **第二层 `_build_user_info_item`**: `follower/following/video_count/upload_count` 传递真实值 (不再全部覆盖为 0)
- 完整字段: name/face/sign/level/sex/birthday/fans/attention/archives/vip/official

#### 反检测优化
- `_use_playwright=False` + `_412_count=0` 每次请求前强制重置（不触发 Playwright 兜底）
- polymer 动态 API 添加 `platform=web` + `timezone_offset=-480` + WBI 签名
- 评论爬虫空闲超时 300s → 60s

#### 关键 Bug 修复
- **`_get_redis()` 缺失**: `_pop_seed` 永远返回 None → 补全方法
- **`parse_bilibili_response(response)`**: Scrapy TextResponse 当 dict 用 → 改为 `json.loads(response.text)`
- **`meta["user_meta"]` KeyError**: 回调崩溃 → `.get("user_meta", response.meta)`

### 一键链式刷新
- 视频详情页「刷新数据」→ 视频+评论爬虫 → 评论结束自动启动用户爬虫 → 用户结束自动运行分析+LLM初筛
- Flask 终端 `[Chain]` 前缀进度日志，全程无需手动干预

### 视频详情页增强
- **刷新用户数据按钮**: 仅扫描当前视频评论者 MID，注入种子+启动用户爬虫
- **账号弹窗刷新按钮** `🔄`: 注入单个 MID + 启动用户爬虫 + 轮询等待 + 自动更新弹窗
- 用户数据未采集 badge 显示，采集完成后自动消除

### LLM 优化
- **单用户分析改用 `analyze()`**: 不再用 `deep_analyze()` AICU 路径，使用标准水军识别 Prompt
- Prompt 含硬规则: "无头像+ID乱码+无动态+无投稿 → 直接判定 type 6 黑产养号型 confidence≥90"
- `_build_raw_profile_line`: post_count/upload_count<0 显示 `?` 而非 `0`，避免误导 LLM

### 爬虫控制页面
- 按钮悬停 Bootstrap tooltip 详细说明（流程/数据目录/警告）
- 补充用户种子: 扫描 `data/comments/*_comments.json` 全部文件，去重注入 Redis

## v2.20 更新 (2026-06-03)

### 调试控制台
- **四选项卡面板**: AICU | LLM | HTTP | 爬虫，`>_` 按钮拖动+面板调整大小+最小化

### 爬虫联动
- **评论→用户串行**: 后台线程监控，评论爬虫完成后自动启动用户爬虫
- **5 爬虫**: video/comment/user/danmaku/up_videos

### 用户爬虫
- **Playwright 兜底**: API -352 时自动抓取 `space.bilibili.com/{mid}` 页面提取 `__INITIAL_STATE__`
- **Cookie URL 解码**: SESSDATA 值自动 `unquote`，解决 -352
- **-352 → Playwright**: 连续 5 次 -352 后启用真实浏览器

### LLM 分析
- **统一数据加载**: `_load_fresh_users` / `_refresh_features` / `_build_raw_profile_line`
- **Prompt 原始数据行**: 批量+单用户均显示「头像:无 | 动态:0条 | 投稿:0个 | 签名:默认」
- **F4/F12 实时刷新**: 从 `data/users/` 重算，不依赖报告缓存
- **LlmScreenTracker**: log/ + finish()，前端 `llmConsoleLog()` 独立标签页
- **单用户 LLM 异步**: 后台线程 + 500ms 轮询日志

### 账号详情
- **全屏 Modal + 列表实时更新**
- **⚠️ 数据未采集 badge**: 无用户 JSON 时标题黄色提示
- **报告 fsync + 评论重注入**

### 其他
- **代理持久化** `runtime_config.json`
- **QR 登录修复**: `data.code` 内层判断 + 代理 + Referer
- **HTTP 拦截器**: 面板关闭时跳过，消除卡顿
- **用户爬虫日志**: fallback 专属日志

## v2.19 更新 (2026-06-01)

### Bug 修复
- **爬虫启动崩溃**: `start_spider` 改用 `python -m scrapy crawl` 确保项目路径正确，移除 `CREATE_NO_WINDOW`
- **评论爬虫 DontCloseSpider 崩溃**: `_check_and_consume_seeds` 加 `from_idle` 参数区分信号处理器/定时器调用源
- **用户爬虫僵尸进程**: `_idle_start_time` 无种子初始化缺失导致永不超时 → 补 `time.time()` 赋值
- **停止按钮杀不掉进程**: 三重杀链失败后调用 `_force_kill_all_bilibili` 终极兜底; `_force_kill_by_command_line` 同时搜 `python.exe`/`scrapy.exe`
- **已停止爬虫误判为运行**: `_is_spider_alive` 同时检查 per-spider 日志和共享 Scrapy 日志 (CRAWLER_LOG_PATH)
- **爬虫日志 404**: `_read_spider_log` 共享日志不存在时回退到 per-spider 专属日志
- **删除 Modal backdrop 残留**: 强制清理 `.modal-backdrop` / `modal-open` / `overflow` 样式
- **run_all.bat 闪退**: 移除含中文路径的 if/else 块，默认不自动启动爬虫
- **日志轮询不停止**: 爬虫状态更新时自动关 `setInterval` / SSE 流

### 新功能
- **补充评论种子**: `POST /api/crawler/rescan-comment-seeds` 从已有视频数据自动重新注入（扫描 1934 视频 → 1816 条种子）
- **补充用户种子**: `POST /api/crawler/rescan-user-seeds` 从评论数据提取 MID 自动注入（130 文件 → 3204 个唯一 MID）
- **首页无后端提示**: fetch 失败时显示「服务未启动」而非静默 N/A

### 优化
- 视频总览各分类默认折叠，点击展开
- 仅评论数据分组显示为「无视频源数据」+ 警告图标
- 爬虫日志选择器移除弹幕爬虫选项
- `stop_spider` 杀失败时返回 `success: False` 而非欺骗用户
- 视频详情页 Chart.js/Bootstrap 改用本地加载 (消除 CDN 依赖)
- 水军账号管理：头像为空时显示 B站默认头像，不再显示 `--`
- LLM 置信度显示保护：自动纠正 `9500%` → `95%`
- `base_config.py` 清理 8 个零引用废弃配置项
- `settings.py` 清理无效 import
