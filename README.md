# Bilibili Sentinel

B站水军评论智能检测与可视化分析系统 v2.19。基于 Scrapy-Redis 分布式爬虫采集评论/用户数据，结合 18 维特征评分引擎 + LLM 多 Provider 语义分析 + AICU 深度回溯，实现水军账号的自动化识别、评分和报告生成，通过 Flask Dashboard 提供完整的 Web 操作界面。

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
        (调度/去重/种子)    (独立curl_cffi采集)      (F1-F18 特征 + LLM 语义)
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
- **用户空间**: 三阶段采集（用户画像 → 投稿列表 → 动态列表），注入 UID 后自动联动视频+评论爬虫
- **用户动态**: `tools/fetch_user_posts.py` 独立脚本，用 curl_cffi 绕过 412 批量采集用户动态，存为 `data/users/{mid}_posts.json`，供 F13/F14 特征使用
- **弹幕数据**: 集成在 AICU 深度分析中，通过 AICU API 自动获取用户历史弹幕（已移除独立弹幕爬虫）
- **种子联动**: 注入用户 UID → 自动推入视频爬虫队列 → 视频爬虫拉取 UP主全部投稿 → 有评论的视频自动推入评论队列

### 水军检测
- **18 维特征评分引擎 (F1-F18)**: 覆盖账号身份、行为模式、内容质量、空间画像四大维度
- **LLM 语义分析**: 多 Provider 支持（DeepSeek V4 / OpenAI GPT-4o / 自定义端点），异步后台执行 + 前端轮询进度，支持 Modal 阈值调节
- **AICU 深度分析**: 对高风险用户回溯历史评论/弹幕/动态，三次融合评分（引擎 50% + LLM 25% + 深度 25%），同样支持 Modal 阈值调节
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

### Dashboard 控制台
- **系统总览** `/`: 健康卡片 + 热门榜独立分类（按时间/播放量/评论数排序）+ UP主分组折叠面板 + 播放量/评论数分桶 + 桶内独立翻页 + 页码跳转 + 分类删除按钮
- **视频详情** `/video/<bvid>`: 评论展示 + 排行榜 + LLM初筛（异步轮询进度）/AICU Modal 弹窗分析 + 特征触发图表 + 用户详情弹窗 + UP主收录按钮
- **爬虫控制** `/crawler`: 3 爬虫管理 (视频/评论/用户) + 进程存活日志回退检测 + 种子注入 + 代理池状态 + 登录面板
- **水军账号管理** `/water-army`: 收录水军库管理 + 搜索/筛选/排序 + 备注编辑 + CSV/JSON 导出 + B站主页直达链接
- **系统设置** `/settings`: 功能开关 + LLM 多 Provider 配置 + AICU 深度分析 + 代理参数

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

3. **代理**: 如需代理访问 B 站 API，修改 `config/base_config.py`:

```python
CLASH_PROXY_ENABLED = True
CLASH_PROXY_URL = "socks5://192.168.1.104:7897"
```

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

## 18 维水军特征评分

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
| F9 批量注册 | 0.04 | 注册日期高度集中在某几天 → 批量注册账号池 |
| F10 互动小圈子 | 0.04 | 反复 @ 相同账号互相评论 → 小团体刷量 |
| F11 VIP 异常 | 0.04 | 低等级 + 购买大会员 → 伪装水军（月费比年费更可疑） |
| F12 账号骨架 | 0.10 | 无头像 + 用户名乱码 + 无动态 + 无投稿 + 默认签名 → 五要素全中 = 100%（空壳号） |
| F13 转发模式 | 0.07 | 动态中以转发为主：抽奖 > 投票 > 纯转发，三级信号递增（v2.17 扩展） |
| F14 敏感内容 | 0.10 | 历史动态含女拳/政治/造谣抹黑 → 高级水军 |
| F15 商业引流 | 0.10 | 评论含赌博/色情/加微信/刷单等硬广告 → 广告水军 |
| F16 时间规律性 | 0.04 | 评论时间间隔高度规律 → "上班式"机器人发帖 |
| F17 自评相似度 | 0.04 | 自己多条评论内容高度相似 → 模板复制粘贴 |
| F18 签名引战 | 0.05 | 个性签名含挑衅/嘲讽/引战话术 → "精神胜利法"引战号 |

**风险等级**: HIGH >= 70 | MEDIUM >= 30 | LOW < 30

**硬加成**: F12 账号骨架 2/5 +0.20 → 3/5 +0.25 → 4/5 +0.30 → 5/5 +0.35 | F14 敏感内容命中 +0.20 | F15 商业引流 +0.20 | F18 签名引战 >=0.50 +0.15 | 骨架+头像组合 +0.15

---

## 项目结构

```
bilibili-sentinel/
├── analyzer/                  # 水军分析引擎
│   ├── feature_extractor.py   #   F1-F18 特征提取
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
| | `POST /api/video/<bvid>/llm-screen` | 批量 LLM 初筛（异步+轮询进度） |
| | `GET /api/llm-screen-status/<bvid>` | LLM 初筛进度轮询 |
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
