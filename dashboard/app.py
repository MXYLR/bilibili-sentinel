"""
B站哨兵 — Flask Dashboard (v2.0)

基于 MediaCrawler 6-Phase 优化后的全面重写。
整合：代理池 / 存储层 / 缓存层 / 登录 / 反检测 所有新模块。
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import threading
import logging
from pathlib import Path
from datetime import datetime

import logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# ---- 简易 Markdown → HTML（不依赖第三方包）---


def _md_to_html(text: str) -> str:
    """极简 Markdown 渲染：加粗/斜体/代码/标题/列表/换行。"""
    if not text:
        return ""
    import re as _re
    lines = text.split("\n")
    out = []
    in_ul = False

    def _inline(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    for line in lines:
        # 标题
        m = _re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        # 无序列表
        m = _re.match(r"^- (.+)", line)
        if m:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        # 有序列表
        m = _re.match(r"^\d+\.\s+(.+)", line)
        if m:
            if not in_ul:
                out.append("<ol>")
                in_ul = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        # 列表结束
        if in_ul:
            out.append("</ul>" if "</ol>" not in out[-1] else "</ol>")
            in_ul = False
        # 空行 → 段落分隔
        if not line.strip():
            out.append("<br><br>")
            continue
        # 普通段落
        out.append(f"<p>{_inline(line)}</p>")

    if in_ul:
        out.append("</ul>" if "<ul>" in "".join(out[-5:]) else "</ol>")
    return "\n".join(out)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, render_template, jsonify, request, Response, stream_with_context, make_response

from config import DATA_DIR, VIDEO_DIR, COMMENT_DIR, REPORT_DIR

app = Flask(__name__)
app.secret_key = "bilibili-sentinel-dashboard-v2"


@app.template_filter("tojson_lite")
def _tojson_lite(value):
    """内联 JSON 过滤器：剔除大字段（features/sample_comments 等），保留图表所需的 top_features。"""
    import json as _json
    # 大字段（单条可达数十KB），剔除后页面 JS 显著缩小
    LARGE_FIELDS = {"features", "sample_comments", "llm_key_evidence", "deep_key_evidence",
                    "aicu_stats", "aicu_names", "aicu_device", "deep_reasoning", "llm_reasoning",
                    "sign", "rank"}
    # top_features 保留：每个用户只有 ~10 条小的 {name, score}，特征图表依赖它
    if isinstance(value, list):
        value = [{k: v for k, v in (u.items() if isinstance(u, dict) else {})
                  if k not in LARGE_FIELDS} for u in value]
    return _json.dumps(value, ensure_ascii=False)

SCRAPY_EXE = os.path.join(PROJECT_ROOT, "venv", "Scripts", "scrapy.exe")
SCRAPY_CWD = PROJECT_ROOT
CRAWLER_LOG_PATH = os.path.join(DATA_DIR, "logs", "bilibili_crawler.log")


def _read_spider_log(spider_name: str, tail_lines: int = 50) -> dict:
    """读取爬虫日志：优先共享 LOG_FILE，回退到 per-spider 日志文件。

    Scrapy LOG_FORMAT: "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    爬虫的 logger name 即爬虫名,所以过滤 [bilibili_video] / [bilibili_comment]
    如果共享日志不存在（run_all.bat 启动时清理了旧日志），回退到 start_spider 创建的专属日志。
    """
    # 优先读共享日志
    if os.path.exists(CRAWLER_LOG_PATH):
        try:
            with open(CRAWLER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            marker = f"[{spider_name}]"
            matched = [ln for ln in all_lines if marker in ln]
            if matched:  # ★ 有匹配才返回，否则继续 fallback
                recent = "".join(matched[-tail_lines:])
                return {
                    "log_file": CRAWLER_LOG_PATH,
                    "total_lines": len(matched),
                    "recent": recent,
                }
        except Exception:
            pass
    # 回退：找最新的 per-spider 日志文件
    log_dir = os.path.join(DATA_DIR, "logs")
    pattern = f"{spider_name}_*.log"
    try:
        candidates = sorted(
            [f for f in os.listdir(log_dir) if f.startswith(spider_name + "_") and f.endswith(".log")],
            key=lambda x: os.path.getmtime(os.path.join(log_dir, x)),
            reverse=True,
        )
        if candidates:
            fallback_path = os.path.join(log_dir, candidates[0])
            with open(fallback_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # per-spider 日志没有 [name] 标记，直接返回尾部
            lines = content.split("\n")
            recent = "\n".join(lines[-tail_lines:])
            return {
                "log_file": fallback_path,
                "total_lines": len(lines),
                "recent": recent,
            }
    except Exception:
        pass
    return {"total_lines": 0, "recent": "", "log_file": CRAWLER_LOG_PATH}


# ============================================================
#  System Monitor — 全局模块健康聚合
# ============================================================

class SystemMonitor:
    """
    统一采集所有子模块的健康指标。

    懒初始化各模块单例，避免 Dashboard 启动时因缺少可选依赖而崩溃。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self._cache_time = 0
        self._cache_ttl = 5  # 缓存5秒

    def _cached(self, key, factory):
        now = time.time()
        if key in self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache[key]
        with self._lock:
            val = factory()
            self._cache[key] = val
            self._cache_time = now
            return val

    def get_proxy_status(self) -> dict:
        def _fetch():
            try:
                from proxy.proxy_ip_pool import ProxyIPPool
                pool = ProxyIPPool.get_instance() if hasattr(ProxyIPPool, "get_instance") else None
                if pool is None:
                    return {
                        "available": False,
                        "enabled": False,
                        "message": "代理池未初始化",
                        "active": 0, "total": 0, "failed": 0, "providers": [],
                    }
                return {
                    "available": True,
                    "enabled": True,
                    "message": "代理池正常",
                    "active": getattr(pool, "_active_count", 0),
                    "total": getattr(pool, "_proxy_queue", None) and pool._proxy_queue.qsize() or 0,
                    "failed": getattr(pool, "_failed_count", 0),
                    "providers": [],
                }
            except ImportError:
                return {"available": False, "enabled": False, "message": "代理模块加载失败", "active": 0, "total": 0, "failed": 0, "providers": []}
            except Exception as e:
                return {"available": False, "enabled": False, "message": str(e), "active": 0, "total": 0, "failed": 0, "providers": []}

        return self._cached("proxy", _fetch)

    def get_cache_status(self) -> dict:
        def _fetch():
            try:
                from cache.local_cache import LocalCache
                cache = LocalCache()
                stats = cache.stats()
                return {
                    "available": True,
                    "message": "缓存正常",
                    "size": stats.get("size", 0),
                    "max_size": stats.get("max_size", 10000),
                    "hit_rate": stats.get("hit_rate", 0),
                    "enabled": True,
                }
            except ImportError:
                return {"available": False, "enabled": False, "message": "缓存模块未安装", "size": 0, "max_size": 0, "hit_rate": 0}
            except Exception as e:
                return {"available": False, "enabled": False, "message": str(e), "size": 0, "max_size": 0, "hit_rate": 0}

        return self._cached("cache", _fetch)

    def get_store_status(self) -> dict:
        def _fetch():
            try:
                from store.store_factory import StoreFactory
                factory = StoreFactory()
                return {
                    "available": True,
                    "current": getattr(factory, "_current_type", "json"),
                    "supported": list(StoreFactory.STORES.keys()),
                    "message": "存储层正常",
                }
            except ImportError:
                return {"available": False, "current": "unknown", "supported": [], "message": "存储模块未安装"}
            except Exception as e:
                return {"available": False, "current": "unknown", "supported": [], "message": str(e)}

        return self._cached("store", _fetch)

    def get_login_status(self) -> dict:
        # ★ 登录状态不缓存，每次实时读取文件
        try:
            from bilibili_crawler.login.login_manager import LoginManager
            mgr = LoginManager()
            return {
                "is_logged_in": mgr.is_logged_in(),
                "has_sessdata": mgr.get_sessdata() is not None,
                "message": "已登录" if mgr.is_logged_in() else "未登录",
            }
        except ImportError:
            return {"is_logged_in": False, "has_sessdata": False, "message": "登录模块未安装"}
        except Exception as e:
            return {"is_logged_in": False, "has_sessdata": False, "message": str(e)}

    def get_llm_status(self) -> dict:
        def _fetch():
            try:
                from analyzer.llm_analyzer import create_llm_analyzer
                analyzer = create_llm_analyzer()
                if analyzer:
                    provider_label = analyzer.provider.upper() if analyzer.provider != "custom" else "自定义"
                    return {
                        "available": True,
                        "model": analyzer.model,
                        "provider": analyzer.provider,
                        "message": f"LLM 已就绪 ({provider_label}, 模型: {analyzer.model})",
                    }
                return {
                    "available": False,
                    "model": None,
                    "provider": None,
                    "message": "未配置 LLM API Key (DEEPSEEK_API_KEY / OPENAI_API_KEY)",
                }
            except ImportError:
                return {"available": False, "model": None, "provider": None, "message": "openai 模块未安装"}
            except Exception as e:
                return {"available": False, "model": None, "provider": None, "message": str(e)}

        return self._cached("llm", _fetch)

    def get_config_summary(self) -> dict:
        def _fetch():
            try:
                from config.base_config import (
                    ENABLE_IP_PROXY, ENABLE_CDP_MODE, SAVE_DATA_OPTION,
                    ENABLE_CACHE_DEDUP, SAVE_LOGIN_STATE, LOGIN_TYPE,
                    ENABLE_LLM_ANALYSIS,
                    CLASH_PROXY_ENABLED, CLASH_PROXY_URL,
                )
                from config.crawler_config import MAX_CONCURRENCY_NUM, RETRY_TIMES
                return {
                    "switches": {
                        "ENABLE_IP_PROXY": ENABLE_IP_PROXY,
                        "ENABLE_CDP_MODE": ENABLE_CDP_MODE,
                        "SAVE_DATA_OPTION": SAVE_DATA_OPTION,
                        "ENABLE_CACHE_DEDUP": ENABLE_CACHE_DEDUP,
                        "SAVE_LOGIN_STATE": SAVE_LOGIN_STATE,
                        "LOGIN_TYPE": LOGIN_TYPE,
                        "ENABLE_LLM_ANALYSIS": ENABLE_LLM_ANALYSIS,
                        "CLASH_PROXY_ENABLED": CLASH_PROXY_ENABLED,
                        "CLASH_PROXY_URL": CLASH_PROXY_URL,
                    },
                    "crawler": {
                        "MAX_CONCURRENCY_NUM": MAX_CONCURRENCY_NUM,
                        "RETRY_TIMES": RETRY_TIMES,
                    },
                }
            except Exception as e:
                return {"switches": {}, "crawler": {}, "error": str(e)}

        return self._cached("config", _fetch)

    def get_full_status(self) -> dict:
        return {
            "proxy": self.get_proxy_status(),
            "cache": self.get_cache_status(),
            "store": self.get_store_status(),
            "login": self.get_login_status(),
            "llm": self.get_llm_status(),
            "modules_healthy": True,
        }

    def refresh_proxy(self) -> dict:
        try:
            from proxy.proxy_ip_pool import ProxyIPPool
            pool = ProxyIPPool.get_instance() if hasattr(ProxyIPPool, "get_instance") else None
            if pool is None:
                return {"success": False, "message": "代理池未初始化"}
            return {"success": True, "message": "代理池刷新请求已发出"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def clear_cache(self) -> dict:
        try:
            from cache.local_cache import LocalCache
            cache = LocalCache()
            cache.clear()
            self._cache.clear()
            return {"success": True, "message": "缓存已清空"}
        except Exception as e:
            return {"success": False, "message": str(e)}


system_monitor = SystemMonitor()


# ============================================================
#  Spider Manager — 进程管理
# ============================================================

class SpiderManager:
    STATE_FILE = os.path.join(DATA_DIR, "spider_state.json")

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)

    def _read_state(self) -> dict:
        if not os.path.exists(self.STATE_FILE):
            return {}
        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _write_state(self, state: dict):
        with open(self.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _is_process_alive(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    def start_spider(self, spider_name: str) -> dict:
        with self._lock:
            state = self._read_state()
            existing = state.get(spider_name, {})
            if existing.get("status") == "running":
                pid = existing.get("pid")
                if pid and self._is_process_alive(pid):
                    return {
                        "success": False,
                        "message": f"爬虫 {spider_name} 已在运行中 (PID: {pid})",
                        "pid": pid,
                    }
            if not os.path.exists(SCRAPY_EXE):
                return {"success": False, "message": f"Scrapy 可执行文件未找到: {SCRAPY_EXE}"}
            try:
                log_file = os.path.join(
                    DATA_DIR, "logs", f"{spider_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                )
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                with open(log_file, "w", encoding="utf-8") as log_f:
                    # 使用 python -m scrapy crawl (非 scrapy.exe) 以确保 project root 在 Python path
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "scrapy", "crawl", spider_name],
                        cwd=SCRAPY_CWD,
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                    )
                entry = {
                    "status": "running", "pid": proc.pid,
                    "started_at": datetime.now().isoformat(), "log_file": log_file,
                }
                state[spider_name] = entry
                self._write_state(state)
                return {
                    "success": True, "message": f"爬虫 {spider_name} 已启动",
                    "pid": proc.pid, "log_file": log_file,
                }
            except Exception as e:
                return {"success": False, "message": f"启动失败: {str(e)}"}

    def stop_spider(self, spider_name: str) -> dict:
        """停止爬虫（自适应升级：PID→WINDOWTITLE→命令行扫描 → Redis 清队列）。

        即使进程卡在 spider_idle / DontCloseSpider 循环中，清空 Redis 队列
        也会让爬虫在下次 idle 检查时自行退出，无需要单独的"强制停止"按钮。
        """
        with self._lock:
            state = self._read_state()
            entry = state.get(spider_name, {})
            if not entry or entry.get("status") != "running":
                # 状态不是 running，但进程可能仍存活（stale state），尝试清扫
                pid = entry.get("pid")
                cleaned_pid = pid and self._kill_process(pid)
                cleaned_cmdline = self._force_kill_by_command_line(spider_name)
                if cleaned_pid or cleaned_cmdline:
                    # 清工作队列，保留种子
                    self._nuke_redis_queues(spider_name, keep_seeds=True)
                    entry["status"] = "stopped"
                    entry["stopped_at"] = datetime.now().isoformat()
                    entry["pid"] = None
                    state[spider_name] = entry
                    self._write_state(state)
                    return {"success": True, "message": f"爬虫 {spider_name} 已停止（状态记录曾标记为 {entry.get('status', '?')}，但进程已被杀死）"}
                # 最终兜底：全杀 scrapy.exe
                self._force_kill_all_bilibili()
                return {"success": False, "message": f"爬虫 {spider_name} 未在运行（已尝试全面清理）"}

            pid = entry.get("pid")
            # 三重杀链：PID → 窗口标题 → 命令行扫描
            killed_by_pid = self._kill_process(pid)
            killed_by_name = False
            if not killed_by_pid and pid:
                killed_by_name = self._force_kill_by_name(spider_name)
            killed_by_cmdline = False
            if not killed_by_pid and not killed_by_name:
                killed_by_cmdline = self._force_kill_by_command_line(spider_name)
            killed = killed_by_pid or killed_by_name or killed_by_cmdline

            # 终极兜底：全杀 scrapy.exe（无论前面杀了没）
            if not killed:
                self._force_kill_all_bilibili()

            # 清空工作队列（dupefilter），但保留种子队列。
            # taskkill /T /F 已强杀进程树，不需要靠空队列来逼退。
            # 保留 start_urls / comment_seeds 避免用户刚注入的种子被误删。
            self._nuke_redis_queues(spider_name, keep_seeds=True)

            if killed:
                entry["status"] = "stopped"
                entry["stopped_at"] = datetime.now().isoformat()
                entry["pid"] = None
                state[spider_name] = entry
                self._write_state(state)
                return {"success": True, "message": f"爬虫 {spider_name} 已停止（队列已清空）"}
            else:
                # 杀进程失败——不清 Redis 队列，等下次 idle 超时自然退出
                return {
                    "success": False,
                    "message": f"无法停止爬虫 {spider_name}：进程杀灭失败。"
                               f"请使用「强制停止」按钮，或等待爬虫 idle 超时后自动退出。",
                }

    def stop_all(self) -> dict:
        """批量停止所有爬虫，强制清理所有 bilibili 相关进程 + Redis 队列"""
        with self._lock:
            state = self._read_state()
            results = {}
            for name in ["bilibili_video", "bilibili_comment"]:
                entry = state.get(name, {})
                pid = entry.get("pid")
                if pid and self._is_process_alive(pid):
                    self._kill_process(pid)
                # 也尝试通过命令行匹配杀
                self._force_kill_by_command_line(name)
                # 清空工作队列，但保留种子队列
                self._nuke_redis_queues(name, keep_seeds=True)
                entry["status"] = "stopped"
                entry["stopped_at"] = datetime.now().isoformat()
                state[name] = entry
                results[name] = "stopped"
            # 最终清扫：窗口标题 + 命令行双重兜底
            self._force_kill_all_bilibili()
            self._write_state(state)
        return {"success": True, "message": "所有爬虫已停止（队列已清空）", "details": results}

    def force_stop(self, spider_name: str) -> dict:
        """核武器停止：清除所有队列 + 杀进程 + 重置状态，确保 spider_idle 无法继续。

        用于当爬虫卡在 spider_idle / DontCloseSpider 循环中时，
        常规 stop 可能因 PID 丢失或进程树残留而无法终止的情况。
        """
        with self._lock:
            state = self._read_state()
            entry = state.get(spider_name, {})
            pid = entry.get("pid")

            # Step 1: 杀进程（三重保障）
            killed = False
            if pid:
                killed = self._kill_process(pid)
            if not killed:
                killed = self._force_kill_by_name(spider_name)
            if not killed:
                killed = self._force_kill_by_command_line(spider_name)

            # Step 2: 清空相关 Redis 队列，让 spider_idle 里的 Redis 检查直接失败
            self._nuke_redis_queues(spider_name)

            # Step 3: 重置状态文件
            entry["status"] = "stopped"
            entry["stopped_at"] = datetime.now().isoformat()
            entry["force_stopped"] = True
            state[spider_name] = entry
            self._write_state(state)

            return {
                "success": True,
                "message": f"爬虫 {spider_name} 已强制停止（进程{'已' if killed else '未检测到'}，队列已清空）",
                "killed": killed,
            }

    # ---- Internal kill helpers ----

    def _kill_process(self, pid: int) -> bool:
        """强制终止进程树（Windows: taskkill /T /F）"""
        if not pid or not self._is_process_alive(pid):
            return True
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
            time.sleep(1)
            return not self._is_process_alive(pid)
        except Exception:
            return False

    def _force_kill_by_name(self, spider_name: str) -> bool:
        """通过窗口标题匹配强行终止指定爬虫（PID 丢失时兜底）。

        使用 taskkill /FI WINDOWTITLE 而非 WMI/PowerShell，避免触发杀软误报。
        """
        title_map = {
            "bilibili_video": "Bilibili Video Spider",
            "bilibili_comment": "Bilibili Comment Spider",
        }
        window_title = title_map.get(spider_name)
        if not window_title:
            return False
        try:
            subprocess.run(
                ["taskkill", "/FI", f"WINDOWTITLE eq {window_title}*", "/F"],
                capture_output=True, timeout=10,
            )
            return True
        except Exception:
            return False

    def _force_kill_by_command_line(self, spider_name: str) -> bool:
        """通过命令行参数匹配杀死 Scrapy 爬虫进程。

        在 spider_idle 场景下，scrapy 进程可能没有可见窗口（CREATE_NO_WINDOW），
        WINDOWTITLE 过滤会失效。此方法使用 PowerShell Get-CimInstance 扫描命令行，
        同时搜索 python.exe 和 scrapy.exe 进程，然后用 taskkill 杀进程。
        """
        spider_to_spider = {
            "bilibili_video": "bilibili_video",
            "bilibili_comment": "bilibili_comment",
            "bilibili_user": "bilibili_user",
            "bilibili_danmaku": "bilibili_danmaku",
        }
        target = spider_to_spider.get(spider_name)
        if not target:
            return False
        killed_any = False
        for exe_name in ("python.exe", "scrapy.exe"):
            try:
                ps_cmd = (
                    f'Get-CimInstance Win32_Process -Filter "Name=\'{exe_name}\'" | '
                    f'Where-Object {{ $_.CommandLine -like \'*scrapy*crawl*{target}*\' }} | '
                    f'Select-Object -ExpandProperty ProcessId'
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                )
                pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
                for pid in pids:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", pid, "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                        killed_any = True
                    except Exception:
                        continue
            except Exception:
                continue
        return killed_any

    def _nuke_redis_queues(self, spider_name: str, keep_seeds: bool = False):
        """清空指定爬虫的 Redis 工作队列。

        视频爬虫: dupefilter（种子队列 start_urls 仅在 force 模式下清除）
        评论爬虫: 仅在 force 模式下清除 comment_seeds

        keep_seeds=True: 保留种子队列，只清除工作队列（dupefilter）
        keep_seeds=False: 全清（force_stop 场景）
        """
        try:
            import redis
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            r.ping()
            if spider_name == "bilibili_video":
                r.delete("bilibili_video:dupefilter")
                if not keep_seeds:
                    r.delete("bilibili_crawler:start_urls")
            elif spider_name == "bilibili_comment":
                if not keep_seeds:
                    r.delete("bilibili_crawler:comment_seeds")
        except Exception:
            pass  # Redis 不可用就跳过，不影响进程杀灭

    def _force_kill_all_bilibili(self):
        """最终兜底：杀光所有 bilibili scrapy 进程（窗口标题 + scrapy.exe 全杀）。"""
        titles = [
            "Bilibili Video Spider",
            "Bilibili Comment Spider",
            "Bilibili User Spider",
            "Bilibili Danmaku Spider",
        ]
        for t in titles:
            try:
                subprocess.run(
                    ["taskkill", "/FI", f"WINDOWTITLE eq {t}*", "/F"],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        # 终极兜底：杀所有 scrapy.exe 孤儿进程
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", "scrapy.exe"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

    def _is_spider_alive(self, spider_name: str, pid=None, log_file: str = None) -> bool:
        """检测爬虫是否存活：PID 检测 + 日志活跃度回退。

        scrapy.exe 在 Windows 上会 fork 子进程后退出，PID 可能已失效。
        回退方案：先查 per-spider 日志，再查共享 Scrapy 日志。
        排除信号：如果日志尾部包含 [CLOSED]，立即判死。
        """
        if pid and self._is_process_alive(pid):
            return True
        # 先查 per-spider 日志（stdout 重定向），再查共享日志（Scrapy LOG_FILE）
        for log_path in (log_file, CRAWLER_LOG_PATH):
            if not log_path or not os.path.exists(log_path):
                continue
            try:
                mtime = os.path.getmtime(log_path)
                if time.time() - mtime < 15:
                    if self._log_has_closed(log_path):
                        return False
                    return True
            except Exception:
                continue
        return False

    def _log_has_closed(self, log_path: str) -> bool:
        """读取日志文件尾部 4KB，检查是否包含 [CLOSED] 标记。"""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size > 4096:
                    f.seek(size - 4096)
                else:
                    f.seek(0)
                tail = f.read()
                return "[CLOSED]" in tail
        except Exception:
            return False

    def get_status(self) -> dict:
        with self._lock:
            state = self._read_state()
            state_changed = False
            spiders = {}
            for name in ["bilibili_video", "bilibili_comment", "bilibili_user", "bilibili_danmaku"]:
                entry = state.get(name) or {}
                pid = entry.get("pid")
                log_file = entry.get("log_file")
                alive = self._is_spider_alive(name, pid, log_file)
                # 自愈：如果状态是 running 但进程已死，自动修正并持久化
                if entry.get("status") == "running" and not alive:
                    entry["status"] = "stopped"
                    entry["stopped_at"] = datetime.now().isoformat()
                    entry["pid"] = None
                    state[name] = entry
                    state_changed = True
                # 如果 entry 不存在（首次启动），创建默认 stopped 状态
                elif not entry:
                    entry = {"status": "stopped", "pid": None, "started_at": None, "stopped_at": None}
                    state[name] = entry
                    state_changed = True
                log_info = _read_spider_log(name, tail_lines=30)
                recent_log = log_info["recent"]
                # 检测 spider_idle 卡住状态
                is_idle_stuck = False
                if alive and recent_log:
                    # 最近日志中出现 spider_idle 且长时间无产出 → 可能卡住
                    has_idle = "[spider_idle]" in recent_log
                    has_closed = "[CLOSED]" in recent_log
                    is_idle_stuck = has_idle and not has_closed
                spiders[name] = {
                    "status": entry.get("status", "stopped"), "pid": pid, "alive": alive,
                    "started_at": entry.get("started_at"), "stopped_at": entry.get("stopped_at"),
                    "log_file": log_info["log_file"], "recent_log": recent_log,
                    "is_idle_stuck": is_idle_stuck,
                }
            if state_changed:
                self._write_state(state)
        return {
            "spiders": spiders,
            "queues": self._get_redis_queues(),
            "stats": self._get_data_stats(),
        }

    def _get_redis_queues(self) -> dict:
        try:
            import redis
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            keys = {
                "video_seeds": "bilibili_crawler:start_urls",
                "comment_seeds": "bilibili_crawler:comment_seeds",
                "user_seeds": "bilibili_crawler:user_seeds",
                "up_video_seeds": "bilibili_crawler:up_video_seeds",
                "pending_requests": "bilibili_crawler:requests",
                "dupefilter": "bilibili_crawler:dupefilter",
            }
            result = {}
            for label, key in keys.items():
                try:
                    rtype = r.type(key)
                    if rtype == "zset": result[label] = r.zcard(key)
                    elif rtype == "set": result[label] = r.scard(key)
                    elif rtype == "list": result[label] = r.llen(key)
                    else: result[label] = 0
                except Exception:
                    result[label] = 0
            result["redis_ok"] = True
            return result
        except ImportError:
            return {"redis_ok": False, "error": "redis 模块未安装"}
        except Exception as e:
            return {"redis_ok": False, "error": str(e)}

    def _get_data_stats(self) -> dict:
        video_count = 0; comment_count = 0; report_count = 0; user_count = 0; danmaku_count = 0
        video_path = Path(DATA_DIR) / "videos"
        comment_path = Path(DATA_DIR) / "comments"
        report_path = Path(DATA_DIR) / "reports"
        user_path = Path(DATA_DIR) / "users"
        danmaku_path = Path(DATA_DIR) / "danmaku"
        if video_path.exists(): video_count = len(list(video_path.glob("*.json")))
        if comment_path.exists(): comment_count = len(list(comment_path.glob("*_comments.json")))
        if report_path.exists(): report_count = len(list(report_path.glob("*_report.json")))
        if user_path.exists(): user_count = len(list(user_path.glob("*.json")))
        if danmaku_path.exists(): danmaku_count = len(list(danmaku_path.glob("*_danmaku.json")))
        return {
            "videos_collected": video_count,
            "comments_collected": comment_count,
            "reports_generated": report_count,
            "users_collected": user_count,
            "danmaku_collected": danmaku_count,
        }

    def inject_seeds(self, seed_type: str, **kwargs) -> dict:
        try:
            import redis
        except ImportError:
            return {"success": False, "message": "redis 模块未安装"}
        try:
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            r.ping()
        except Exception as e:
            return {"success": False, "message": f"Redis 连接失败: {str(e)}"}
        if seed_type == "hot":
            pages = kwargs.get("pages", 3)
            url = f"bilibili_hot://page/1-{pages}"
            r.lpush("bilibili_crawler:start_urls", url)
            return {"success": True, "message": f"已注入热门排行榜种子: 1-{pages} 页 (~{pages * 50} 个视频)", "url": url}
        elif seed_type == "bvid":
            bvid = kwargs.get("bvid", "").strip()
            if not bvid or not bvid.upper().startswith("BV"):
                return {"success": False, "message": "请输入有效的 BV 号 (如 BV1xx411c7mD)"}
            url = f"bilibili_bvid://{bvid}"
            r.lpush("bilibili_crawler:start_urls", url)
            # 同时获取视频 aid 并注入评论种子队列，使评论爬虫可直接使用
            comment_msg = ""
            try:
                import requests as req
                api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
                resp = req.get(api_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                    "Referer": "https://www.bilibili.com",
                }, timeout=10)
                data = resp.json()
                if data.get("code") == 0:
                    vinfo = data["data"]
                    aid = vinfo.get("aid", 0)
                    reply_count = vinfo.get("stat", {}).get("reply", 0)
                    if aid and reply_count > 0:
                        seed = json.dumps({"bvid": bvid, "aid": aid, "reply_count": reply_count})
                        r.lpush("bilibili_crawler:comment_seeds", seed)
                        comment_msg = f"，已同步注入评论种子 (aid={aid}, {reply_count}条评论)"
                    elif aid:
                        comment_msg = f"，视频 aid={aid}（暂无评论数据）"
            except Exception as e:
                comment_msg = f"（获取评论种子失败: {str(e)[:50]}）"
            return {"success": True, "message": f"已注入 BV号种子: {bvid}{comment_msg}", "url": url}
        elif seed_type == "keyword":
            keyword = kwargs.get("keyword", "").strip()
            if not keyword:
                return {"success": False, "message": "请输入搜索关键词"}
            pages = kwargs.get("pages", 1)
            for p in range(1, pages + 1):
                r.lpush("bilibili_crawler:start_urls", f"bilibili_search://{keyword}/page/{p}")
            return {"success": True, "message": f"已注入搜索种子: \"{keyword}\" ({pages} 页)"}
        elif seed_type == "user":
            # 从 unique_mids.json 加载 MIDs 注入用户种子队列（保留旧逻辑，供 API 调用）
            mids_file = os.path.join(DATA_DIR, "users", "unique_mids.json")
            if not os.path.exists(mids_file):
                return {"success": False, "message": "未找到 unique_mids.json，请先运行评论爬虫收集用户ID"}
            with open(mids_file, "r", encoding="utf-8") as f:
                mids = json.load(f)
            if not mids:
                return {"success": False, "message": "unique_mids.json 为空，暂无用户数据"}
            limit = kwargs.get("limit", 100)
            mids_to_inject = mids[:limit] if limit > 0 else mids
            for mid in mids_to_inject:
                r.lpush("bilibili_crawler:user_seeds", json.dumps({"mid": mid}))
            return {
                "success": True,
                "message": f"已注入 {len(mids_to_inject)} 个用户种子 (来源: unique_mids.json, 共 {len(mids)} 个MID)",
            }
        elif seed_type == "user_uid":
            # 前端 UID 输入框直接注入（支持逗号分隔多个 UID）
            mids = kwargs.get("mids", [])
            if not mids or not isinstance(mids, list):
                return {"success": False, "message": "未提供有效 UID 列表"}
            valid_mids = [int(m) for m in mids if str(m).isdigit() and int(m) > 0]
            if not valid_mids:
                return {"success": False, "message": "未识别到有效 UID"}
            for mid in valid_mids:
                r.lpush("bilibili_crawler:user_seeds", json.dumps({"mid": mid}))
                # v2.17: 联动 — 同时将 MID 注入视频爬虫，使其爬取该 UP主全部投稿
                r.lpush("bilibili_crawler:start_urls", f"bilibili_mid://{mid}")
            return {
                "success": True,
                "message": f"已注入 {len(valid_mids)} 个用户种子 (UID: {valid_mids[:5]}{'...' if len(valid_mids) > 5 else ''})，已联动视频爬虫",
            }
        elif seed_type == "up_videos":
            # UP主视频爬虫种子: 注入 MID 到 up_video_seeds 队列
            mid = kwargs.get("mid", 0)
            try:
                mid = int(mid)
            except (ValueError, TypeError):
                return {"success": False, "message": f"无效的 UID: {mid}"}
            if mid <= 0:
                return {"success": False, "message": "请输入有效的 UP主 UID"}
            r.lpush("bilibili_crawler:up_video_seeds", json.dumps({"mid": mid}))
            return {"success": True, "message": f"已注入 UP主种子: UID={mid} (爬取其所有投稿视频)"}
        elif seed_type == "rescan_users":
            # ★ 从评论数据重新扫描并注入用户种子 (用于自动联动)
            comment_dir = Path(DATA_DIR) / "comments"
            all_mids = set()
            if comment_dir.exists():
                for cf in comment_dir.glob("*_comments.json"):
                    try:
                        with open(cf, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        comments = data if isinstance(data, list) else data.get("comments", [])
                        for c in comments:
                            if isinstance(c, dict) and c.get("mid"):
                                all_mids.add(int(c["mid"]))
                    except Exception:
                        continue
            injected = 0
            for m in all_mids:
                r.rpush("bilibili_crawler:user_seeds", json.dumps({"mid": m}))
                injected += 1
            return {"success": True, "message": f"已从评论数据注入 {injected} 个用户种子", "injected": injected}
        else:
            return {"success": False, "message": f"未知种子类型: {seed_type}"}

    def clear_queues(self) -> dict:
        try:
            import redis
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            keys = [
                "bilibili_crawler:start_urls", "bilibili_crawler:comment_seeds",
                "bilibili_crawler:user_seeds", "bilibili_crawler:up_video_seeds",
                "bilibili_crawler:requests", "bilibili_crawler:dupefilter",
            ]
            deleted = sum(1 for key in keys if r.exists(key) and not r.delete(key))
            return {"success": True, "message": f"已清空 {deleted} 个队列"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def refresh_video(self, bvid: str) -> dict:
        """刷新单个视频：清除去重记录 + 注入种子 + 启动爬虫。

        清除该 BV 号在 dupefilter 中的指纹记录，
        然后注入视频+评论种子队列，启动对应爬虫。
        """
        bvid = bvid.strip()
        if not bvid or not bvid.upper().startswith("BV"):
            return {"success": False, "message": f"无效的 BV 号: {bvid}"}

        try:
            import redis
            import hashlib
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            r.ping()
        except ImportError:
            return {"success": False, "message": "redis 模块未安装"}
        except Exception as e:
            return {"success": False, "message": f"Redis 连接失败: {str(e)}"}

        # 1. 清除该 BV 号在 dupefilter 中的记录
        video_url = f"bilibili_bvid://{bvid}"
        fingerprint = hashlib.sha1(video_url.encode()).hexdigest()
        dupefilter_key = "bilibili_crawler:dupefilter"
        removed_count = 0
        try:
            if r.sismember(dupefilter_key, fingerprint):
                r.srem(dupefilter_key, fingerprint)
                removed_count += 1
        except Exception:
            pass

        # 也清除可能存在的其他格式指纹（兼容不同 scrapy-redis 版本）
        alt_fingerprint = hashlib.sha1(video_url.encode("utf-8")).hexdigest()
        if alt_fingerprint != fingerprint:
            try:
                if r.sismember(dupefilter_key, alt_fingerprint):
                    r.srem(dupefilter_key, alt_fingerprint)
                    removed_count += 1
            except Exception:
                pass

        # 2. 注入视频种子
        r.lpush("bilibili_crawler:start_urls", video_url)

        # 3. 获取 aid 并注入评论种子
        comment_msg = ""
        try:
            import requests as req
            api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            resp = req.get(api_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "https://www.bilibili.com",
            }, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                vinfo = data["data"]
                aid = vinfo.get("aid", 0)
                reply_count = vinfo.get("stat", {}).get("reply", 0)
                if aid and reply_count > 0:
                    seed = json.dumps({"bvid": bvid, "aid": aid, "reply_count": reply_count})
                    r.lpush("bilibili_crawler:comment_seeds", seed)
                    # 清除该 aid 对应的评论去重指纹（评论爬虫用 aid 指纹）
                    comment_fp = hashlib.sha1(f"bilibili_comment://{aid}".encode()).hexdigest()
                    try:
                        if r.sismember(dupefilter_key, comment_fp):
                            r.srem(dupefilter_key, comment_fp)
                    except Exception:
                        pass
                    comment_msg = f"，评论种子已注入 (aid={aid}, {reply_count}条)"
                elif aid:
                    comment_msg = f"，视频 aid={aid}（暂无评论数据）"
        except Exception as e:
            comment_msg = f"（获取评论种子失败: {str(e)[:50]}）"

        # 4. 启动视频和评论爬虫（先清理可能残留的僵尸状态）
        start_results = {}
        for spider_name in ("bilibili_video", "bilibili_comment"):
            # 如果状态是 running 但进程早已死亡，先重置
            st = self._read_state()
            entry = st.get(spider_name, {})
            if entry.get("status") == "running":
                pid = entry.get("pid")
                if not pid or not self._is_process_alive(pid):
                    entry["status"] = "stopped"
                    entry["pid"] = None
                    st[spider_name] = entry
                    self._write_state(st)
            result = self.start_spider(spider_name)
            start_results[spider_name] = {
                "started": result.get("success", False),
                "message": result.get("message", ""),
            }

        spider_failed = not any(r["started"] for r in start_results.values())
        return {
            "success": not spider_failed,
            "message": f"已刷新 {bvid}：去重记录清除了 {removed_count} 条，种子已注入{comment_msg}"
                        + ("，但爬虫启动失败" if spider_failed else ""),
            "bvid": bvid,
            "fingerprint": fingerprint,
            "removed_dupefilter": removed_count,
            "spiders": start_results,
        }


spider_mgr = SpiderManager()


# ============================================================
#  Analysis Manager — 异步分析 + 状态追踪
# ============================================================

class AnalysisManager:
    STATE_FILE = os.path.join(DATA_DIR, "analysis_state.json")

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)

    def _read_state(self) -> dict:
        if not os.path.exists(self.STATE_FILE): return {}
        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _write_state(self, state: dict):
        with open(self.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def get_status(self, bvid: str) -> dict:
        state = self._read_state()
        entry = state.get(bvid, {})
        return {
            "bvid": bvid,
            "status": entry.get("status", "idle"),
            "progress": entry.get("progress", ""),
            "started_at": entry.get("started_at"),
            "finished_at": entry.get("finished_at"),
            "error": entry.get("error"),
            "stats": entry.get("stats"),
        }

    def start_analysis(self, bvid: str) -> dict:
        state = self._read_state()
        existing = state.get(bvid, {})
        if existing.get("status") == "running":
            return {"success": False, "message": f"视频 {bvid} 正在分析中", "status": "running"}
        entry = {
            "status": "running", "progress": "正在初始化...",
            "started_at": datetime.now().isoformat(), "finished_at": None,
            "error": None, "stats": None,
        }
        state[bvid] = entry
        self._write_state(state)
        threading.Thread(target=self._run_analysis, args=(bvid,), daemon=True).start()
        return {"success": True, "message": f"开始分析 {bvid}", "status": "running"}

    def _update_progress(self, bvid: str, progress: str):
        state = self._read_state()
        if bvid in state:
            state[bvid]["progress"] = progress
            self._write_state(state)

    def _run_analysis(self, bvid: str):
        try:
            from deploy.run_analyzer import analyze_video as _analyze
            self._update_progress(bvid, "正在加载评论数据...")
            comment_path = os.path.join(PROJECT_ROOT, "data", "comments", f"{bvid}_comments.json")
            if not os.path.exists(comment_path):
                raise FileNotFoundError(
                    f"评论数据不存在: {bvid}_comments.json。请先在爬虫控制台中采集该视频的评论数据。")
            self._update_progress(bvid, "正在提取特征...")
            report = _analyze(bvid, verbose=False)
            if report is None:
                raise RuntimeError(f"分析 {bvid} 失败：请确保已采集评论数据。")
            state = self._read_state()
            if bvid in state:
                state[bvid].update({
                    "status": "done", "progress": "分析完成 (含LLM)",
                    "finished_at": datetime.now().isoformat(), "error": None,
                })
                if report and "statistics" in report:
                    stats = report["statistics"]
                    state[bvid]["stats"] = {
                        "total_users": stats.get("total_users", 0),
                        "high_risk_count": stats.get("high_risk_count", 0),
                        "medium_risk_count": stats.get("medium_risk_count", 0),
                        "low_risk_count": stats.get("low_risk_count", 0),
                        "avg_score": stats.get("avg_score", 0),
                        "llm_analyzed": (report.get("llm_stats") or {}).get("llm_analyzed", 0),
                        "llm_positive": (report.get("llm_stats") or {}).get("llm_positive", 0),
                    }
                self._write_state(state)
        except Exception as e:
            state = self._read_state()
            if bvid in state:
                state[bvid].update({
                    "status": "error", "progress": "分析出错",
                    "finished_at": datetime.now().isoformat(), "error": str(e),
                })
                self._write_state(state)


# ============================================================
#  LLM 初筛进度追踪器 (异步 + 轮询)
# ============================================================

class LlmScreenTracker:
    """跟踪 LLM 初筛进度，支持前端轮询 + 流式日志。"""

    _tasks: dict = {}  # bvid -> {status, progress, ..., logs: [{level, msg}, ...]}

    _lock = threading.Lock()

    @classmethod
    def start(cls, bvid: str, **initial) -> bool:
        with cls._lock:
            if bvid in cls._tasks and cls._tasks[bvid].get("status") == "running":
                return False
            cls._tasks[bvid] = {
                "status": "running",
                "progress": "正在初始化...",
                "total_batches": 0,
                "done_batches": 0,
                "success_count": 0,
                "total": 0,
                "error": None,
                "identified_types": {},
                "logs": [],  # ★ 流式日志
                **initial,
            }
            return True

    @classmethod
    def update(cls, bvid: str, **kwargs):
        with cls._lock:
            if bvid in cls._tasks:
                cls._tasks[bvid].update(kwargs)

    @classmethod
    def log(cls, bvid: str, msg: str, level: str = "info"):
        """记录流式日志（前端轮询获取）"""
        with cls._lock:
            if bvid in cls._tasks:
                cls._tasks[bvid].setdefault("logs", []).append({"level": level, "msg": msg})

    @classmethod
    def get_status(cls, bvid: str, since_log: int = 0) -> dict:
        with cls._lock:
            t = cls._tasks.get(bvid, {})
        logs = t.get("logs", [])
        return {
            "status": t.get("status", "idle"),
            "progress": t.get("progress", ""),
            "total_batches": t.get("total_batches", 0),
            "done_batches": t.get("done_batches", 0),
            "success_count": t.get("success_count", 0),
            "total": t.get("total", 0),
            "error": t.get("error"),
            "identified_types": t.get("identified_types", {}),
            "logs": logs[since_log:] if since_log < len(logs) else [],
        }

    @classmethod
    def finish(cls, bvid: str, result: dict = None, error: str = None):
        with cls._lock:
            if bvid in cls._tasks:
                if error:
                    cls._tasks[bvid]["status"] = "error"
                    cls._tasks[bvid]["error"] = error
                else:
                    cls._tasks[bvid]["status"] = "done"
                if result:
                    cls._tasks[bvid]["result"] = result


class DeleteTaskTracker:
    """跟踪分类删除进度，支持前端轮询。"""

    _tasks: dict = {}  # task_id -> {status, progress, total, done, error, result}
    _lock = threading.Lock()

    @classmethod
    def start(cls, task_id: str, total: int = 0) -> bool:
        with cls._lock:
            if task_id in cls._tasks and cls._tasks[task_id].get("status") == "running":
                return False
            cls._tasks[task_id] = {
                "status": "running", "progress": f"准备删除 0/{total}...",
                "total": total, "done": 0, "error": None, "result": None,
            }
            return True

    @classmethod
    def update(cls, task_id: str, **kwargs):
        with cls._lock:
            if task_id in cls._tasks:
                cls._tasks[task_id].update(kwargs)

    @classmethod
    def get_status(cls, task_id: str) -> dict:
        with cls._lock:
            t = cls._tasks.get(task_id, {})
        return {
            "status": t.get("status", "idle"),
            "progress": t.get("progress", ""),
            "total": t.get("total", 0),
            "done": t.get("done", 0),
            "error": t.get("error"),
            "result": t.get("result"),
        }


class AicuBatchTracker:
    """跟踪批量 AICU 深度分析进度，支持前端轮询。"""

    _tasks: dict = {}  # bvid -> {status, total, done, current_user, progress, logs}
    _lock = threading.Lock()
    MAX_LOGS = 200  # 最多保留日志条数

    @classmethod
    def _ts(cls) -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S")

    @classmethod
    def _append_log(cls, t: dict, msg: str, level: str = "info"):
        t.setdefault("logs", [])
        t["logs"].append({"ts": cls._ts(), "msg": msg, "level": level})
        if len(t["logs"]) > cls.MAX_LOGS:
            t["logs"] = t["logs"][-cls.MAX_LOGS:]

    @classmethod
    def start(cls, bvid: str, total: int = 0):
        with cls._lock:
            cls._tasks[bvid] = {
                "status": "running", "total": total, "done": 0,
                "current_user": "准备中...", "progress": f"0/{total}",
                "logs": [],
            }
            cls._append_log(cls._tasks[bvid],
                f"开始批量 AICU 深度分析，共 {total} 个候选用户", "info")

    @classmethod
    def update(cls, bvid: str, done: int, current_user: str = ""):
        with cls._lock:
            if bvid in cls._tasks:
                t = cls._tasks[bvid]
                t["done"] = done
                t["current_user"] = current_user
                t["progress"] = f"{done}/{t['total']}"
                # 解析 "done/total uname" 格式
                parts = current_user.split(" ", 1)
                uname = parts[1] if len(parts) > 1 else current_user
                cls._append_log(t, f"[{done}/{t['total']}] 分析用户: {uname}", "info")

    @classmethod
    def log(cls, bvid: str, msg: str, level: str = "info"):
        """外部写入日志条目（用于更细粒度的步骤追踪）。"""
        with cls._lock:
            t = cls._tasks.get(bvid)
            if t:
                cls._append_log(t, msg, level)

    @classmethod
    def finish(cls, bvid: str, result: dict = None, error: str = None):
        with cls._lock:
            if bvid in cls._tasks:
                t = cls._tasks[bvid]
                t["status"] = "error" if error else "done"
                t["error"] = error
                t["result"] = result
                if error:
                    cls._append_log(t, f"分析异常: {error}", "error")
                else:
                    newly = result.get("newly_analyzed", 0) if result else 0
                    confirmed = result.get("deep_confirmed", 0) if result else 0
                    cls._append_log(t,
                        f"分析完成！新增 {newly} 个用户，确认水军 {confirmed} 人", "success")

    @classmethod
    def get_status(cls, bvid: str, since_log: int = 0) -> dict:
        with cls._lock:
            t = cls._tasks.get(bvid, {})
            all_logs = t.get("logs", [])
            new_logs = all_logs[since_log:]
        return {
            "status": t.get("status", "idle"),
            "total": t.get("total", 0),
            "done": t.get("done", 0),
            "current_user": t.get("current_user", ""),
            "progress": t.get("progress", ""),
            "error": t.get("error"),
            "result": t.get("result"),
            "logs": new_logs,
            "log_count": len(all_logs),
        }


analysis_mgr = AnalysisManager()


# ============================================================
#  工具函数
# ============================================================

def _list_video_dirs() -> list:
    """列出所有可展示的视频条目。

    遍历 data/videos/*.json (视频元数据) 为主数据源。
    同时扫描 data/comments/*_comments.json，将"有评论但无视频元数据"
    的视频也纳入展示列表，避免已抓取评论的视频在 Dashboard 上不可见。
    """
    videos = []
    seen_bvids = set()
    video_path = Path(DATA_DIR) / "videos"

    # ---- 第一轮: 遍历视频元数据文件 (主数据源) ----
    if video_path.exists():
        for json_file in sorted(video_path.glob("*.json"), reverse=True):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                bvid = json_file.stem
                seen_bvids.add(bvid)
                info = data.get("video_info", data) if isinstance(data, dict) else data
                report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
                has_report = report_path.exists()
                comment_path = Path(DATA_DIR) / "comments" / f"{bvid}_comments.json"
                comment_count = _load_comment_count(comment_path, bvid)
                source_raw = info.get("source", "")
                videos.append({
                    "bvid": bvid,
                    "title": info.get("title", "N/A"),
                    "owner_name": info.get("owner_name", info.get("owner", {}).get("name", "未知")),
                    "owner_mid": info.get("owner_mid", info.get("owner", {}).get("mid", 0)),
                    "view_count": info.get("view_count", info.get("stat", {}).get("view", 0)),
                    "reply_count": info.get("reply_count", info.get("stat", {}).get("reply", 0)),
                    "danmaku_count": info.get("danmaku_count", info.get("stat", {}).get("danmaku", 0)),
                    "pubdate": info.get("pubdate", info.get("pub_date", 0)),
                    "pic": info.get("pic", ""),
                    "has_report": has_report,
                    "comment_count": comment_count,
                    "source": source_raw,
                })
            except Exception as e:
                print(f"Error parsing {json_file}: {e}")
                continue

    # ---- 第二轮: 补充"有评论但无视频元数据"的视频 ----
    # 跳过最近 5 分钟内修改的仅评论文件——这些是评论爬虫正在写入的活跃文件，
    # 对应的视频元数据可能还未抓取，不应显示为「无视频源数据」。
    _COMMENT_ONLY_GRACE_SECONDS = 300
    comment_path = Path(DATA_DIR) / "comments"
    if comment_path.exists():
        for cf in sorted(comment_path.glob("*_comments.json"), reverse=True):
            # 从文件名提取 bvid: "BVxxx_comments.json" -> "BVxxx"
            bvid = cf.stem.replace("_comments", "")
            if not bvid or bvid in seen_bvids:
                continue  # 已有视频元数据，跳过
            # 跳过"新鲜"的仅评论文件（可能正在被评论爬虫写入）
            try:
                file_age = time.time() - os.path.getmtime(str(cf))
                if file_age < _COMMENT_ONLY_GRACE_SECONDS:
                    continue
            except Exception:
                pass
            seen_bvids.add(bvid)
            comment_count = _load_comment_count(cf, bvid)
            report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
            videos.append({
                "bvid": bvid,
                "title": f"[仅有评论数据] {bvid}",
                "owner_name": "[仅评论]",
                "owner_mid": 0,
                "view_count": 0,
                "reply_count": 0,
                "danmaku_count": 0,
                "pubdate": 0,
                "pic": "",
                "has_report": report_path.exists(),
                "comment_count": comment_count,
                "source": "comment_only",
            })

    return videos


def _load_comment_count(comment_path, bvid: str = "") -> int:
    """安全加载评论文件并返回评论条目数。异常时记录日志并返回 0。"""
    if not comment_path.exists():
        return 0
    try:
        with open(comment_path, "r", encoding="utf-8") as cf:
            comments_data = json.load(cf)
        if isinstance(comments_data, list):
            return len(comments_data)
        elif isinstance(comments_data, dict):
            return len(comments_data.get("comments", []))
        else:
            return 0
    except json.JSONDecodeError as e:
        print(f"[WARN] 评论文件 JSON 解析失败 {comment_path.name}: {e}")
        return 0
    except Exception as e:
        print(f"[WARN] 评论文件加载失败 {comment_path.name}: {type(e).__name__}: {e}")
        return 0


def _load_report(bvid: str) -> dict:
    report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
    if not report_path.exists():
        return None
    with open(report_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_report(bvid: str, report: dict) -> None:
    report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())


def _load_video_info(bvid: str) -> dict:
    video_path = Path(DATA_DIR) / "videos" / f"{bvid}.json"
    if not video_path.exists():
        return None
    with open(video_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_comments(bvid: str) -> list:
    comment_path = Path(DATA_DIR) / "comments" / f"{bvid}_comments.json"
    if not comment_path.exists():
        return []
    with open(comment_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list): return data
    if isinstance(data, dict): return data.get("comments", data.get("replies", []))
    return []


# ============================================================
#  页面路由
# ============================================================

@app.route("/")
def index():
    videos = _list_video_dirs()
    return render_template("index.html", videos=videos)


@app.route("/debug-js")
def debug_js():
    # 加载真实 user_scores 供测试
    user_scores = {}
    import glob, os
    reports = sorted(glob.glob(os.path.join(DATA_DIR, "reports", "*_report.json")))
    if reports:
        with open(reports[0], "r", encoding="utf-8") as f:
            report = json.load(f)
        _src = report.get("scored_users_export") or report.get("scored_users") or report.get("top_suspects") or []
        for u in _src:
            mid = u.get("mid", 0)
            if mid:
                user_scores[mid] = {"score": u.get("suspicious_score", u.get("score", 0)), "level": u.get("risk_level", u.get("level", "low")), "type": u.get("water_army_type", u.get("llm_type_name", ""))}
    resp = make_response(render_template("video_debug.html", user_scores=user_scores))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route("/video/<bvid>")
def video_detail(bvid: str):
    video_info = _load_video_info(bvid)
    report = _load_report(bvid)
    comments = _load_comments(bvid)

    # ---- 降级处理: 有评论但无视频元数据 ----
    if not video_info:
        if not comments:
            return render_template("error.html", message=f"视频 {bvid} 不存在（无视频元数据也无评论）"), 404
        # 构造默认 video_info：优先从评论中推导基本信息
        first_comment = comments[0] if isinstance(comments, list) else {}
        video_info = {
            "bvid": bvid,
            "title": f"[仅有评论数据] {bvid}",
            "desc": "",
            "pic": "",
            "owner_name": first_comment.get("uname", "") if isinstance(first_comment, dict) else "",
            "owner_mid": int(first_comment.get("mid", 0)) if isinstance(first_comment, dict) else 0,
            "view_count": 0,
            "danmaku_count": 0,
            "reply_count": 0,
            "favorite_count": 0,
            "coin_count": 0,
            "share_count": 0,
            "like_count": 0,
            "duration": 0,
            "pubdate": "",
            "tname": "",
            "tags": [],
            "crawl_time": "",
            "_fallback": True,  # 标记为降级数据
        }

    comment_count = len(comments)

    # ---- 后端预渲染 AI 摘要 Markdown → HTML ----
    ai_summary_html = ""
    if report and report.get("ai_summary"):
        # 修复 LLM 置信度显示异常（如 9500% → 95%）
        import re as _re
        ai_text = report["ai_summary"]
        ai_text = _re.sub(r'(\d{3,})%', lambda m: str(int(int(m.group(1))/100)) + '%' if int(m.group(1)) > 100 else m.group(0), ai_text)
        try:
            ai_summary_html = _md_to_html(ai_text)
        except Exception:
            ai_summary_html = ai_text.replace("\n", "<br>")

    # ---- 构建全量 user_scores（供前端 riskMap 使用）----
    user_scores = {}
    # 优先读全量导出字段（report_generator.py 写入）
    _src = (report.get("scored_users_export") or report.get("scored_users") or []) if report else []
    if not _src:
        # 降级：从 top_suspects 构建
        _src = (report.get("top_suspects") or []) if report else []
    for u in _src:
        mid = u.get("mid", 0)
        if mid:
            user_scores[mid] = {
                "score": u.get("suspicious_score", u.get("score", 0)),
                "level": u.get("risk_level", u.get("level", "low")),
                "type": u.get("water_army_type", u.get("llm_type_name", "")),
                "llm_type_id": u.get("llm_type_id", 0),
            }

    resp = make_response(render_template(
        "video_detail.html",
        bvid=bvid,
        video=video_info,
        report=report,
        comments=comments,
        comment_count=comment_count,
        ai_summary_html=ai_summary_html,
        user_scores=user_scores,
    ))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route("/crawler")
def crawler_page():
    return render_template("crawler.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


# ============================================================
#  Dashboard API 路由
# ============================================================

@app.route("/api/videos")
def api_videos():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    all_videos = _list_video_dirs()
    total = len(all_videos)

    # 分页
    start = (page - 1) * per_page
    end = start + per_page
    page_videos = all_videos[start:end]

    # UP主分组统计
    up_groups = {}
    for v in all_videos:
        mid = v.get("owner_mid", 0) or 0
        if mid:
            up_groups.setdefault(mid, {"mid": mid, "name": v.get("owner_name", ""), "count": 0, "total_views": 0})
            up_groups[mid]["count"] += 1
            up_groups[mid]["total_views"] += v.get("view_count", 0) or 0

    return jsonify({
        "success": True,
        "data": page_videos,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "up_groups": sorted(up_groups.values(), key=lambda x: x["count"], reverse=True),
    })


@app.route("/api/report/<bvid>")
def api_report(bvid: str):
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "message": f"报告 {bvid} 不存在"}), 404
    return jsonify({"success": True, "data": report})


@app.route("/api/timeline/<bvid>")
def api_timeline(bvid: str):
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "message": "报告不存在"}), 404
    return jsonify({"success": True, "data": report.get("comment_timeline", [])})


@app.route("/api/video/<bvid>/scored-users")
def api_scored_users(bvid: str):
    """返回裁剪后的 top_suspects（与 tojson_lite 相同字段），供前端异步加载。"""
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在"}), 404
    top = report.get("top_suspects", [])
    LARGE_FIELDS = {"features", "sample_comments", "llm_key_evidence", "deep_key_evidence",
                    "aicu_stats", "aicu_names", "aicu_device", "deep_reasoning", "llm_reasoning",
                    "sign", "rank"}
    lite = [{k: v for k, v in u.items() if k not in LARGE_FIELDS} for u in top]
    return jsonify({"success": True, "data": lite})


@app.route("/api/score-distribution/<bvid>")
def api_score_distribution(bvid: str):
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "message": "报告不存在"}), 404
    stats = report.get("statistics", {})

    # score_distribution 由 scorer.get_statistics() 生成，格式为 {"0-20": N, ...}（5桶 dict）
    # 直接使用，若为空则返回5桶默认值
    score_dist = stats.get("score_distribution", {})
    if not score_dist or not isinstance(score_dist, dict):
        score_dist = {"0-20": 0, "20-40": 0, "40-60": 0, "60-80": 0, "80-100": 0}

    return jsonify({
        "success": True,
        "data": {
            "buckets": score_dist,
            "high_risk": stats.get("high_risk_count", 0),
            "medium_risk": stats.get("medium_risk_count", 0),
            "low_risk": stats.get("low_risk_count", 0),
            "total": stats.get("total_users", 0),
            "avg_score": stats.get("avg_score", 0),
        }
    })


@app.route("/api/run-analysis/<bvid>", methods=["POST"])
def api_run_analysis(bvid: str):
    # ★ 自动联动：注入用户种子 + 启动用户爬虫
    user_info = _auto_start_user_spider()
    result = analysis_mgr.start_analysis(bvid)
    result["user_spider"] = user_info  # 返回给前端显示
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 409


@app.route("/api/analysis-status/<bvid>")
def api_analysis_status(bvid: str):
    status = analysis_mgr.get_status(bvid)
    return jsonify({"success": True, "data": status})


@app.route("/api/video/<bvid>/deep-analyze", methods=["POST"])
def api_video_deep_analyze(bvid: str):
    """异步批量深度分析 (AICU)，后台执行 + 前端轮询进度。

    请求体: {"threshold": 30}  (可选, 默认30, 范围10-70)
    立即返回 bvid 作为 task 标识符，前端轮询 GET /api/video/<bvid>/deep-analyze-status
    """
    # ---- 1. 加载数据 ----
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在，请先运行分析"}), 404

    top_suspects = report.get("top_suspects", [])
    if not top_suspects:
        return jsonify({"success": False, "error": "无嫌疑用户数据"}), 400

    comments = _load_comments(bvid)
    if not comments:
        return jsonify({"success": False, "error": "评论数据不存在"}), 404

    video_info = report.get("video_info", {}) or _load_video_info(bvid) or {}

    # ---- 2. 解析阈值 ----
    data = request.get_json(silent=True) or {}
    try:
        threshold = float(data.get("threshold", 30))
        threshold = max(10, min(70, threshold))
    except (TypeError, ValueError):
        threshold = 30

    # ---- 3. 构建 scored_users ----
    scored_users = []
    for u in top_suspects:
        scored_users.append({
            "mid": u.get("mid", 0),
            "uname": u.get("uname", ""),
            "suspicious_score": u.get("score", 0),
            "engine_score_raw": u.get("engine_score_raw", u.get("score", 0)),
            "llm_confidence": u.get("llm_confidence", 0),
            "llm_type_id": u.get("llm_type_id", 0),
            "llm_type_name": u.get("llm_type_name", ""),
            "features": u.get("features", {}),
            "risk_level": u.get("risk_level", "low"),
            "level": u.get("level", 0),
            "comment_count": u.get("comment_count", 0),
            "deep_analyzed": u.get("deep_analyzed", False),
            "deep_type_id": u.get("deep_type_id"),
            "deep_type_name": u.get("deep_type_name", ""),
            "deep_confidence": u.get("deep_confidence"),
            "deep_reasoning": u.get("deep_reasoning", ""),
            "deep_risk_confirmed": u.get("deep_risk_confirmed", False),
            "deep_key_evidence": u.get("deep_key_evidence", []),
        })

    # ---- 4. 创建分析器并校验 ----
    from analyzer.llm_analyzer import create_llm_analyzer
    analyzer = create_llm_analyzer()
    if not analyzer:
        return jsonify({"success": False, "error": "LLM 分析器不可用，请检查 API Key 配置"}), 503

    candidate_count = sum(1 for u in scored_users if u["suspicious_score"] >= threshold)
    if candidate_count == 0:
        return jsonify({
            "success": False,
            "error": f"阈值 {threshold} 下无候选用户",
        }), 400

    # ---- 5. 启动后台线程 + 追踪器 ----
    AicuBatchTracker.start(bvid, total=candidate_count)

    threading.Thread(
        target=_run_aicu_deep_analyze_bg,
        args=(bvid, report, scored_users, comments, video_info, threshold, analyzer),
        daemon=True,
    ).start()

    return jsonify({
        "success": True,
        "message": f"深度分析已启动，{candidate_count} 个候选用户",
        "total": candidate_count,
        "bvid": bvid,
    })


def _run_aicu_deep_analyze_bg(bvid, report, scored_users, comments, video_info, threshold, analyzer):
    """后台执行批量 AICU 深度分析，逐步更新进度追踪器。"""

    def _track_log(level, msg):
        """将日志同时写入 Python logger 和前端追踪器。"""
        if level == "error":
            logger.error(f"[AICU-bg] {msg}")
        elif level == "warn":
            logger.warning(f"[AICU-bg] {msg}")
        else:
            logger.info(f"[AICU-bg] {msg}")
        AicuBatchTracker.log(bvid, msg, level)

    try:
        candidate_users = [u for u in scored_users if u["suspicious_score"] >= threshold]
        total = len(candidate_users)

        result = analyzer.deep_analyze(
            scored_users, comments, video_info,
            threshold_override=threshold,
            progress_callback=lambda done, uname: AicuBatchTracker.update(
                bvid, done=done, current_user=f"{done}/{total} {uname}"
            ),
            log_callback=_track_log,
        )
    except Exception as e:
        logger.exception(f"[AICU-bg] 深度分析异常: {e}")
        AicuBatchTracker.finish(bvid, error=str(e))
        return

    # 合并结果回报告
    enhanced_users = result.get("enhanced_users", [])
    enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}

    _track_log("info", f"增强用户: {len(enhanced_users)} total, {len(enhanced_by_mid)} 有效mid")
    _track_log("info", f"报告用户: {len(report['top_suspects'])} total")

    # ★ 备份 sample_comments（防止合并覆盖）
    sample_comments_backup = {}
    for u in report["top_suspects"]:
        mid = u.get("mid", 0)
        sc = u.get("sample_comments")
        if sc:
            sample_comments_backup[mid] = sc
    _track_log("info", f"备份样本评论: {len(sample_comments_backup)} 个用户有评论数据")

    merged_count = 0
    for u in report["top_suspects"]:
        mid = u.get("mid", 0)
        enhanced = enhanced_by_mid.get(mid)
        if enhanced:
            merged_count += 1
            old_deep = u.get("deep_analyzed", False)
            new_deep = enhanced.get("deep_analyzed", False)
            if new_deep:
                _track_log("info", f"  mid={mid} {u.get('uname','?')}: deep_analyzed {old_deep}→{new_deep}, type={enhanced.get('deep_type_name','?')}")
        mid = u.get("mid", 0)
        enhanced = enhanced_by_mid.get(mid)
        if enhanced:
            new_score = enhanced.get("suspicious_score", u.get("score", u.get("suspicious_score", 0)))
            u["suspicious_score"] = new_score
            u["score"] = new_score
            u["deep_analyzed"] = enhanced.get("deep_analyzed", False)
            u["deep_type_id"] = enhanced.get("deep_type_id")
            u["deep_type_name"] = enhanced.get("deep_type_name", "")
            u["deep_confidence"] = enhanced.get("deep_confidence")
            u["deep_reasoning"] = enhanced.get("deep_reasoning", "")
            u["deep_risk_confirmed"] = enhanced.get("deep_risk_confirmed", False)
            u["deep_key_evidence"] = enhanced.get("deep_key_evidence", [])
            u["aicu_comment_count"] = enhanced.get("aicu_comment_count")
            u["aicu_stats"] = enhanced.get("aicu_stats")
            u["aicu_device"] = enhanced.get("aicu_device", "")
            u["aicu_names"] = enhanced.get("aicu_names", [])

    # ★ 恢复合并中被覆盖的 sample_comments
    restored_count = 0
    for u in report["top_suspects"]:
        mid = u.get("mid", 0)
        if mid in sample_comments_backup and not u.get("sample_comments"):
            u["sample_comments"] = sample_comments_backup[mid]
            restored_count += 1
    if restored_count > 0:
        _track_log("warn", f"恢复了 {restored_count} 个用户的样本评论数据")

    skipped = len(report["top_suspects"]) - merged_count
    if skipped > 0:
        _track_log("warn", f"{skipped} 个用户在增强结果中未找到 (mid不匹配)")

    # ★ 直接从评论文件重新注入 sample_comments，确保永不去失
    try:
        comments = _load_comments(bvid)
        comments_by_mid = {}
        for c in comments:
            mid = c.get("mid", 0)
            if mid not in comments_by_mid:
                comments_by_mid[mid] = []
            if len(comments_by_mid[mid]) < 5:
                comments_by_mid[mid].append(c.get("content", c.get("message", ""))[:200])
        reloaded_count = 0
        for u in report["top_suspects"]:
            mid = u.get("mid", 0)
            if mid in comments_by_mid:
                u["sample_comments"] = comments_by_mid[mid]
                reloaded_count += 1
        _track_log("info", f"从评论文件重新注入: {reloaded_count} 个用户的样本评论")
    except Exception as e:
        _track_log("warn", f"重新注入评论失败: {e}")

    deep_stats = result.get("stats", {})
    report["deep_stats"] = deep_stats
    newly_analyzed = sum(1 for u in report["top_suspects"] if u.get("deep_analyzed"))

    # 验证：保存前记录关键字段状态
    user_with_comments = sum(1 for u in report["top_suspects"] if u.get("sample_comments"))
    _track_log("info", f"报告合并完成: {newly_analyzed} 深度分析用户, {user_with_comments} 用户有样本评论")

    # 保存报告（确保落盘）
    report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            f.flush()
            os.fsync(f.fileno())
    except Exception as save_err:
        logger.exception(f"[AICU-bg] 保存报告失败: {save_err}")
        _track_log("error", f"报告保存失败: {save_err}")
        AicuBatchTracker.finish(bvid, error=f"保存报告失败: {save_err}")
        return

    # 验证：重新读取确认
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            reloaded = json.load(f)
        reloaded_suspects = reloaded.get("top_suspects", [])
        reloaded_analyzed = sum(1 for u in reloaded_suspects if u.get("deep_analyzed"))
        reloaded_with_comments = sum(1 for u in reloaded_suspects if u.get("sample_comments"))
        _track_log("info", f"报告已保存并验证: {reloaded_analyzed} 深度分析用户, {reloaded_with_comments} 用户有样本评论")
    except Exception as verify_err:
        _track_log("warn", f"报告验证失败 (非致命): {verify_err}")

    AicuBatchTracker.finish(bvid, result={
        "deep_stats": deep_stats,
        "threshold": threshold,
        "newly_analyzed": newly_analyzed,
        "deep_confirmed": deep_stats.get("deep_confirmed", 0),
    })


@app.route("/api/video/<bvid>/deep-analyze-status")
def api_deep_analyze_status(bvid: str):
    """轮询 AICU 深度分析进度。支持 ?since=N 增量获取日志, ?key= 指定任务 key。"""
    since = request.args.get("since", 0, type=int)
    key = request.args.get("key", bvid)
    return jsonify({"success": True, "data": AicuBatchTracker.get_status(key, since_log=since)})


def _run_single_aicu_bg(bvid, mid, report, user, comments, video_info, analyzer, task_key):
    """后台执行单用户 AICU 深度分析，逐步更新 AicuBatchTracker。"""
    def _log(level, msg):
        AicuBatchTracker.log(task_key, msg, level)
        logger.info(f"[AICU单用户] {msg}")

    try:
        single_user = {
            "mid": mid,
            "uname": user.get("uname", ""),
            "suspicious_score": user.get("score", 0),
            "engine_score_raw": user.get("engine_score_raw", user.get("score", 0)),
            "llm_confidence": user.get("llm_confidence", 0),
            "llm_type_id": user.get("llm_type_id", 0),
            "llm_type_name": user.get("llm_type_name", ""),
            "comment_count": user.get("comment_count", 1),
        }

        _log("info", f"开始单用户深度分析: mid={mid} score={single_user['suspicious_score']}")

        result = analyzer.deep_analyze(
            [single_user], comments_data=comments, video_info=video_info,
            threshold_override=0,
            log_callback=_log,
        )

        enhanced_users = result.get("enhanced_users", [])
        enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}
        enhanced = enhanced_by_mid.get(mid, single_user)

        _log("info", f"deep_analyze返回: deep_analyzed={enhanced.get('deep_analyzed')}, "
             f"stats={result.get('stats',{})}")

        # 写回报告
        user["deep_analyzed"] = enhanced.get("deep_analyzed", False)
        if enhanced.get("deep_analyzed"):
            deep_conf = enhanced.get("deep_confidence", 0)
            deep_type_id = enhanced.get("deep_type_id", 0)
            deep_type_name = enhanced.get("deep_type_name", "") or ("正常用户" if deep_type_id == 0 else "")

            user["deep_type_id"] = deep_type_id
            user["deep_type_name"] = deep_type_name
            user["deep_confidence"] = deep_conf
            user["deep_reasoning"] = enhanced.get("deep_reasoning", "")
            user["deep_key_evidence"] = enhanced.get("deep_key_evidence", [])
            user["deep_risk_confirmed"] = enhanced.get("deep_risk_confirmed", False)
            user["aicu_comment_count"] = enhanced.get("aicu_comment_count")
            user["aicu_device"] = enhanced.get("aicu_device", "")
            user["aicu_names"] = enhanced.get("aicu_names", [])
            user["aicu_stats"] = enhanced.get("aicu_stats")
            user["aicu_waf_blocked"] = enhanced.get("aicu_waf_blocked", False)
            user["llm_confidence"] = deep_conf
            user["llm_type_id"] = deep_type_id
            user["llm_type_name"] = deep_type_name
            user["score"] = enhanced.get("suspicious_score", user.get("score", 0))

        # ★ 重新注入样本评论（防止保存后丢失）
        try:
            comments_raw = _load_comments(bvid)
            _log("info", f"重新注入: 评论文件有{len(comments_raw)}条记录")
            by_mid = {}
            for c in comments_raw:
                cmid = str(c.get("mid", 0))
                if cmid not in by_mid: by_mid[cmid] = []
                if len(by_mid[cmid]) < 5:
                    by_mid[cmid].append(c.get("content", c.get("message", ""))[:200])
            _log("info", f"重新注入: {len(by_mid)} 个不同的 mid")
            injected = 0
            for u in report["top_suspects"]:
                umid = str(u.get("mid", 0))
                if umid in by_mid:
                    u["sample_comments"] = by_mid[umid]
                    injected += 1
            _log("info", f"重新注入完成: {injected}/{len(report['top_suspects'])} 用户获得样本评论")
        except Exception as e:
            _log("warn", f"重新注入评论失败: {e}")

        # 保存报告
        report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        _log("success", f"单用户深度分析完成: deep_analyzed={enhanced.get('deep_analyzed')}, "
             f"type={enhanced.get('deep_type_name','?')}, "
             f"aicu_comments={enhanced.get('aicu_comment_count',0)}")

        AicuBatchTracker.finish(task_key, result={
            "deep_analyzed": enhanced.get("deep_analyzed", False),
            "deep_type_name": enhanced.get("deep_type_name", ""),
            "deep_confidence": enhanced.get("deep_confidence", 0),
            "aicu_comment_count": enhanced.get("aicu_comment_count", 0),
            "aicu_device": enhanced.get("aicu_device", ""),
            "score": enhanced.get("suspicious_score", 0),
        })
    except Exception as e:
        logger.error(f"单用户 AICU 深度分析失败 (mid={mid}): {e}", exc_info=True)
        _log("error", f"分析失败: {e}")
        AicuBatchTracker.finish(task_key, error=str(e))


@app.route("/api/video/<bvid>/user/<int:mid>/llm-analyze", methods=["POST"])
def api_user_llm_analyze(bvid: str, mid: int):
    """对单个用户执行 LLM 语义分析（异步，前端轮询获取日志和结果）。"""
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在，请先运行分析"}), 404

    top_suspects = report.get("top_suspects", [])
    user = next((u for u in top_suspects if str(u.get("mid")) == str(mid)), None)
    if not user:
        return jsonify({"success": False, "error": f"未找到 MID={mid} 的用户"}), 404

    from analyzer.llm_analyzer import create_llm_analyzer
    analyzer = create_llm_analyzer()
    if not analyzer or not analyzer.is_available:
        return jsonify({"success": False, "error": "LLM 不可用"}), 503

    task_key = f"{bvid}_user_llm_{mid}"
    if LlmScreenTracker._tasks.get(task_key, {}).get("status") == "running":
        return jsonify({"success": True, "status": "running", "message": "LLM 分析已在运行中"})

    LlmScreenTracker.start(task_key, total=1)
    LlmScreenTracker.update(task_key, done_batches=0, progress=f"分析中...")

    threading.Thread(
        target=_run_single_llm_bg,
        args=(bvid, mid, report, user, analyzer, task_key),
        daemon=True,
    ).start()

    return jsonify({"success": True, "task_key": task_key, "message": "单用户 LLM 分析已启动"})


def _run_single_llm_bg(bvid, mid, report, user, analyzer, task_key):
    """后台执行单用户 LLM 分析，逐步更新 LlmScreenTracker。"""
    def _log(level, msg):
        LlmScreenTracker.log(task_key, msg, level)
        logger.info(f"[LLM单用户] {msg}")

    try:
        comments = _load_comments(bvid)
        user_comments = [c for c in comments if str(c.get("mid")) == str(mid)]
        if not user_comments:
            _log("error", "该用户在此视频下无评论")
            LlmScreenTracker.update(task_key, status="error", error="无评论")
            return

        _log("info", f"开始单用户 LLM 分析: mid={mid} score={user.get('score',0)}")

        # 特征刷新
        raw_features = user.get("features", {}) or {}
        fresh_user = _load_fresh_users({str(mid)}).get(str(mid), {})
        if fresh_user:
            raw_features = _refresh_features(raw_features, fresh_user, user.get("uname", ""))
            _log("info", _build_raw_profile_line(fresh_user))

        single_user = {
            "mid": mid, "uname": user.get("uname", ""), "level": user.get("level", 0),
            "suspicious_score": user.get("score", 0),
            "comment_count": user.get("comment_count", len(user_comments)),
            "sample_comments": user_comments[:5],
            "features": raw_features, "sign": user.get("sign", ""),
            "raw_profile": _build_raw_profile_line(fresh_user),
        }

        video_info = report.get("video_info", {}) or _load_video_info(bvid) or {}
        _log("info", "调用 LLM API...")
        result = analyzer.deep_analyze([single_user], comments_data=comments, video_info=video_info, threshold_override=0)

        enhanced_users = result.get("enhanced_users", [])
        enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}
        enhanced = enhanced_by_mid.get(mid, single_user)

        llm_type_id = enhanced.get("llm_type_id", 0)
        llm_type_name = enhanced.get("llm_type_name", "") or ("正常用户" if llm_type_id == 0 else "")
        llm_conf = enhanced.get("llm_confidence", 0)

        _log("success" if llm_type_id > 0 else "info",
             f"分析完成: type={llm_type_name} confidence={llm_conf}%")

        # 写回报告
        user["llm_analyzed"] = True
        user["llm_confidence"] = llm_conf
        user["llm_type_id"] = llm_type_id
        user["llm_type_name"] = llm_type_name
        user["score"] = enhanced.get("suspicious_score", user.get("score", 0))

        report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        LlmScreenTracker.finish(task_key, result={
            "success": True, "mid": mid, "deep_analyzed": True,
            "deep_type_name": llm_type_name, "deep_confidence": llm_conf,
            "llm_type_name": llm_type_name, "llm_confidence": llm_conf,
            "score": enhanced.get("suspicious_score", user.get("score", 0)),
        })
    except Exception as e:
        logger.error(f"单用户 LLM 分析失败 (mid={mid}): {e}", exc_info=True)
        _log("error", f"分析失败: {e}")
        LlmScreenTracker.update(task_key, status="error", error=str(e))

@app.route("/api/video/<bvid>/llm-screen", methods=["POST"])
def api_video_llm_screen(bvid: str):
    """批量 LLM 初筛：异步执行，前端轮询 /api/llm-screen-status/<bvid> 获取进度。"""
    data = request.get_json(silent=True) or {}
    threshold = max(0, int(data.get("threshold", 30)))

    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在"}), 404

    # 检查是否已在运行
    status = LlmScreenTracker.get_status(bvid)
    if status["status"] == "running":
        return jsonify({"success": True, "status": "running", "message": "LLM 初筛已在运行中"})

    # ★ 从 data/users/ 加载所有用户数据
    _fresh_users = _load_fresh_users()
    logger.info(f"[LLM初筛] 加载了 {len(_fresh_users)} 个用户数据")

    # 先收集候选人（验证参数和报告有效性）
    _src = (report.get("scored_users_export") or report.get("scored_users") or report.get("top_suspects") or [])
    _top_map = {u.get("mid"): u for u in (report.get("top_suspects") or []) if u.get("mid")}

    candidates = []
    updated_features = 0
    for u in _src:
        score = u.get("suspicious_score", u.get("score", 0))
        if score < threshold:
            continue
        info = _top_map.get(u.get("mid"), {})
        mid_str = str(u.get("mid", 0))
        raw_features = info.get("features", {}) or {}
        fresh_ud = _fresh_users.get(mid_str, {})
        if fresh_ud:
            raw_features = _refresh_features(raw_features, fresh_ud, info.get("uname", ""))
            updated_features += 1

        first_val = next(iter(raw_features.values()), 0) if raw_features else 0
        features_01 = {k: round(v / 100, 4) for k, v in raw_features.items()} if first_val > 1.0 else raw_features
        candidates.append({
            "mid": u.get("mid"),
            "uname": info.get("uname", u.get("uname", "")),
            "level": info.get("level", u.get("level", 0)),
            "suspicious_score": score,
            "comments": info.get("sample_comments", []),
            "features": features_01,
            "sign": info.get("sign", u.get("sign", "")),
            "raw_profile": _build_raw_profile_line(fresh_ud),  # ★ 原始用户数据
        })

    if updated_features > 0:
        logger.info(f"[LLM初筛] 预加载 {len(_fresh_users)} 个用户文件, 刷新了 {updated_features} 个候选人的 F4/F12")
    else:
        logger.info(f"[LLM初筛] 预加载 {len(_fresh_users)} 个用户文件, 但无候选人匹配")

    if not candidates:
        return jsonify({"success": True, "total": 0, "success_count": 0, "message": "没有需要初筛的用户"})

    # 验证 LLM 可用性
    from analyzer.llm_analyzer import create_llm_analyzer
    analyzer = create_llm_analyzer()
    if not analyzer or not analyzer.is_available:
        return jsonify({"success": False, "error": "LLM 分析未启用，请检查配置"}), 400

    # 启动后台线程
    if not LlmScreenTracker.start(bvid, total=len(candidates)):
        return jsonify({"success": True, "status": "running", "message": "LLM 初筛已在运行中"})

    threading.Thread(
        target=_run_llm_screen_bg,
        args=(bvid, report, candidates, threshold),
        daemon=True,
    ).start()

    return jsonify({"success": True, "status": "started", "total": len(candidates)})


def _run_llm_screen_bg(bvid: str, report: dict, candidates: list, threshold: int):
    """后台执行 LLM 初筛的完整流程。"""
    import math
    from collections import Counter
    batch_size = 5
    total_batches = math.ceil(len(candidates) / batch_size)
    enhanced_by_mid = {}
    comments_data = _load_comments(bvid)

    def _log(level, msg):
        LlmScreenTracker.log(bvid, msg, level)
        logger.info(f"[LLM初筛] {msg}")
        # 双保险：写一条固定日志验证 tracker 可用
        if not hasattr(LlmScreenTracker, '_dbg'):
            LlmScreenTracker._dbg = True
            logger.info(f"[LLM初筛] Tracker log 测试: bvid={bvid} tasks={list(LlmScreenTracker._tasks.keys())}")

    _log("info", f"LLM 初筛启动: {len(candidates)} 个候选用户, {total_batches} 批")
    LlmScreenTracker.update(bvid, total_batches=total_batches, total=len(candidates),
                             progress=f"开始分析 (0/{total_batches})")

    try:
        from analyzer.llm_analyzer import create_llm_analyzer
        analyzer = create_llm_analyzer()

        batch_type_counter = Counter()
        for i in range(0, len(candidates), batch_size):
            batch_num = i // batch_size + 1
            batch = candidates[i:i + batch_size]
            _log("info", f"LLM 批次 {batch_num}/{total_batches} (共{len(batch)}人)")
            LlmScreenTracker.update(bvid, progress=f"分析中 ({batch_num}/{total_batches} 批)",
                                     done_batches=batch_num - 1)
            try:
                result = analyzer.analyze(batch, comments_data)
                for u in result.get("enhanced_users", []):
                    enhanced_by_mid[u["mid"]] = u
                # 统计本批类型
                batch_types = Counter()
                for u in result.get("enhanced_users", []):
                    tid = u.get("llm_type_id", 0)
                    if tid > 0:
                        tname = u.get("llm_type_name", "未知")
                        batch_types[tname] += 1
                        batch_type_counter[tname] += 1
                if batch_types:
                    summary = ", ".join(f"{t}:{c}" for t, c in batch_types.most_common(3))
                    _log("success", f"  批次{batch_num}完成: {summary}")
                else:
                    _log("info", f"  批次{batch_num}完成: 均为正常用户")
                LlmScreenTracker.update(bvid, done_batches=batch_num)
            except Exception as e:
                logger.warning(f"LLM 初筛批次 {batch_num} 失败: {e}")
                _log("error", f"  批次{batch_num}失败: {e}")
                LlmScreenTracker.update(bvid, progress=f"批次 {batch_num} 失败，继续中...",
                                         done_batches=batch_num)

        # 写回报告
        _log("info", "正在保存 LLM 初筛结果...")
        LlmScreenTracker.update(bvid, progress="正在保存结果...")
        updated = 0
        _writeback_src = report.get("scored_users_export") or report.get("scored_users") or report.get("top_suspects") or []
        for user in _writeback_src:
            mid = user.get("mid")
            if mid in enhanced_by_mid:
                enh = enhanced_by_mid[mid]
                user["llm_type_id"] = enh.get("llm_type_id", 0)
                user["llm_type_name"] = enh.get("llm_type_name", "")
                user["llm_confidence"] = enh.get("llm_confidence", 0)
                user["llm_reasoning"] = enh.get("llm_reasoning", "")
                user["llm_key_evidence"] = enh.get("llm_key_evidence", [])
                user["llm_analyzed"] = True
                updated += 1

        if _writeback_src is not report.get("top_suspects"):
            for user in report.get("top_suspects", []):
                mid = user.get("mid")
                if mid in enhanced_by_mid:
                    enh = enhanced_by_mid[mid]
                    user["llm_type_id"] = enh.get("llm_type_id", 0)
                    user["llm_type_name"] = enh.get("llm_type_name", "")
                    user["llm_confidence"] = enh.get("llm_confidence", 0)
                    user["llm_reasoning"] = enh.get("llm_reasoning", "")
                    user["llm_key_evidence"] = enh.get("llm_key_evidence", [])
                    user["llm_analyzed"] = True

        # 统计类型
        type_counts = {}
        for user in _writeback_src:
            tid = user.get("llm_type_id", 0)
            if tid > 0:
                tname = user.get("llm_type_name", "未知")
                type_counts[tname] = type_counts.get(tname, 0) + 1

        _save_report(bvid, report)

        summary = ", ".join(f"{t}:{c}" for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:5]) if type_counts else "正常用户"
        _log("success", f"LLM初筛完成: {updated} 用户更新, 类型: {summary}")

        LlmScreenTracker.update(bvid, status="done", progress="初筛完成",
                                 success_count=updated, identified_types=type_counts)
    except Exception as e:
        logger.error(f"批量 LLM 初筛失败 (bvid={bvid}): {e}", exc_info=True)
        _log("error", f"LLM初筛失败: {e}")
        LlmScreenTracker.update(bvid, status="error", progress="初筛出错", error=str(e))


@app.route("/api/llm-screen-status/<bvid>")
def api_llm_screen_status(bvid: str):
    """轮询 LLM 初筛进度。支持 ?since=N 增量获取日志, ?key= 指定任务 key。"""
    since = request.args.get("since", 0, type=int)
    key = request.args.get("key", bvid)
    return jsonify({"success": True, "data": LlmScreenTracker.get_status(key, since_log=since)})


@app.route("/api/video/<bvid>/user/<int:mid>/deep-analyze", methods=["POST"])
def api_user_deep_analyze(bvid: str, mid: int):
    """对单个用户执行 AICU 深度分析（异步，前端轮询 status 获取日志）。"""
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在，请先运行分析"}), 404

    top_suspects = report.get("top_suspects", [])
    user = next((u for u in top_suspects if str(u.get("mid")) == str(mid)), None)
    if not user:
        return jsonify({"success": False, "error": f"未找到 MID={mid} 的用户"}), 404

    comments = _load_comments(bvid)
    if not comments:
        return jsonify({"success": False, "error": "评论数据不存在"}), 404

    video_info = report.get("video_info", {}) or _load_video_info(bvid) or {}

    from analyzer.llm_analyzer import create_llm_analyzer
    analyzer = create_llm_analyzer()
    if not analyzer:
        return jsonify({"success": False, "error": "LLM 分析器不可用"}), 503

    # 使用复合 key 区分单用户任务
    task_key = f"{bvid}_user_{mid}"
    AicuBatchTracker.start(task_key, total=1)
    AicuBatchTracker.update(task_key, done=0, current_user=f"0/1 {user.get('uname', str(mid))}")

    # 后台线程执行
    threading.Thread(
        target=_run_single_aicu_bg,
        args=(bvid, mid, report, user, comments, video_info, analyzer, task_key),
        daemon=True,
    ).start()

    return jsonify({
        "success": True,
        "message": "单用户深度分析已启动",
        "task_key": task_key,
    })


@app.route("/api/video/<bvid>/user/<int:mid>/detail")
def api_user_detail(bvid: str, mid: int):
    """返回单个用户的详细分析数据（供前端原地刷新弹窗）"""
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "error": "报告不存在"}), 404

    top_suspects = report.get("top_suspects", [])
    user = next((u for u in top_suspects if str(u.get("mid")) == str(mid)), None)
    if not user:
        return jsonify({"success": False, "error": f"未找到 MID={mid} 的用户"}), 404

    # 附加样本评论
    comments = _load_comments(bvid)
    sample = [c for c in comments if str(c.get("mid")) == str(mid)][:5]
    user["sample_comments"] = sample

    # ★ 检查用户空间数据是否已采集
    user_file = Path(DATA_DIR) / "users" / f"{mid}.json"
    user["_user_data_available"] = user_file.exists()

    return jsonify({"success": True, "data": user})


@app.route("/api/comments/<bvid>")
def api_comments(bvid: str):
    """返回树形评论结构：主评论 (root=0) + 嵌套子评论 (replies)。

    分页按主评论数计算，子评论随主评论一起返回不单独分页。
    """
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    comments = _load_comments(bvid)

    # ---- 构建评论树 ----
    # Step 1: 建立 rpid → comment 索引
    comment_map = {}
    root_comments = []
    for c in comments:
        rpid = c.get("rpid")
        if rpid:
            comment_map[rpid] = c
        if c.get("root", 0) == 0:
            root_comments.append(c)

    # Step 2: 将子评论归入对应主评论的 replies 数组
    replies_by_root = {}
    for c in comments:
        root = c.get("root", 0)
        if root > 0:
            replies_by_root.setdefault(root, []).append(c)

    # Step 2.5: 为子回复富化 parent_uname (v2.16 修复楼中楼"回复: "空白名)
    for root_rpid, reply_list in replies_by_root.items():
        for reply in reply_list:
            parent_rpid = reply.get("parent", 0)
            if parent_rpid and parent_rpid in comment_map:
                reply["parent_uname"] = comment_map[parent_rpid].get("uname", "")

    # Step 3: 为主评论附加 replies (按时间排序)
    for rc in root_comments:
        rpid = rc.get("rpid")
        replies = replies_by_root.get(rpid, [])
        replies.sort(key=lambda x: x.get("ctime", 0))
        rc["replies"] = replies

    # Step 4: 按时间倒序排列主评论
    root_comments.sort(key=lambda x: x.get("ctime", 0), reverse=True)

    # Step 5: 分页（按主评论数）
    top_level_total = len(root_comments)
    start = (page - 1) * per_page
    end = start + per_page
    paged = root_comments[start:end]

    return jsonify({
        "success": True,
        "data": paged,
        "total": len(comments),
        "top_level_total": top_level_total,
        "page": page,
        "per_page": per_page,
    })


@app.route("/api/danmaku/<bvid>")
def api_danmaku(bvid: str):
    """返回指定视频的弹幕数据 (分页)。"""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    danmaku_path = Path(DATA_DIR) / "danmaku" / f"{bvid}_danmaku.json"
    if not danmaku_path.exists():
        return jsonify({"success": True, "data": [], "total": 0})
    with open(danmaku_path, "r", encoding="utf-8") as f:
        danmaku = json.load(f)
    if not isinstance(danmaku, list):
        danmaku = []
    start = (page - 1) * per_page
    end = start + per_page
    return jsonify({
        "success": True,
        "data": danmaku[start:end],
        "total": len(danmaku),
        "page": page,
        "per_page": per_page,
    })


# ============================================================
#  数据管理 API — 删除已爬取数据
# ============================================================

@app.route("/api/data/video/<bvid>", methods=["DELETE"])
def api_delete_video_data(bvid: str):
    """删除指定 BV 号的所有数据：视频 JSON + 评论 JSON + 报告 JSON"""
    bvid = bvid.strip()
    if not bvid or not bvid.startswith("BV"):
        return jsonify({"success": False, "message": f"无效的 BV 号: {bvid}"}), 400

    deleted = []
    errors = []

    targets = [
        ("video",   Path(DATA_DIR) / "videos"   / f"{bvid}.json"),
        ("comment", Path(DATA_DIR) / "comments"  / f"{bvid}_comments.json"),
        ("report",  Path(DATA_DIR) / "reports"   / f"{bvid}_report.json"),
    ]

    for label, path in targets:
        try:
            if path.exists():
                path.unlink()
                deleted.append(label)
        except Exception as e:
            errors.append(f"{label}: {e}")

    if not deleted:
        return jsonify({"success": False, "message": f"未找到 {bvid} 的任何数据文件"}), 404

    return jsonify({
        "success": True,
        "message": f"已删除 {bvid} 的 {', '.join(deleted)} 数据",
        "deleted": deleted,
        "errors": errors if errors else None,
    })


@app.route("/api/data/all", methods=["DELETE"])
def api_delete_all_data():
    """清空所有已爬取数据（视频 + 评论 + 报告）"""
    dirs = {
        "videos":   Path(DATA_DIR) / "videos",
        "comments": Path(DATA_DIR) / "comments",
        "reports":  Path(DATA_DIR) / "reports",
    }

    deleted_counts = {}
    for name, dir_path in dirs.items():
        count = 0
        if dir_path.exists():
            for f in list(dir_path.glob("*.json")):
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        deleted_counts[name] = count

    total = sum(deleted_counts.values())
    return jsonify({
        "success": True,
        "message": f"已清空 {total} 个文件",
        "deleted": deleted_counts,
    })


@app.route("/api/data/category", methods=["DELETE"])
def api_delete_category_data():
    """异步删除指定分类的所有数据（热门视频 / UP主视频 / 搜索关键词）。

    请求体: {"source": "hot"}  或  {"owner_mid": 315812497}

    立即返回 task_id，前端轮询 /api/data/category-status/<task_id> 获取进度。
    """
    data = request.get_json(silent=True) or {}
    source_filter = data.get("source", "").strip()
    owner_mid = data.get("owner_mid")

    if not source_filter and not owner_mid:
        return jsonify({"success": False, "message": "缺少分类参数 (source 或 owner_mid)"}), 400

    # 列出匹配视频（source_filter 必须非空才过滤，避免空字符串误匹配）
    all_videos = _list_video_dirs()
    matched = []
    for v in all_videos:
        if source_filter and v.get("source") == source_filter:
            matched.append(v.get("bvid"))
        elif owner_mid is not None and not source_filter and v.get("owner_mid") == owner_mid:
            matched.append(v.get("bvid"))

    if not matched:
        return jsonify({"success": False, "message": "未找到匹配的视频"}), 404

    # 生成 task_id 并启动后台线程
    import uuid
    task_id = uuid.uuid4().hex[:8]
    DeleteTaskTracker.start(task_id, total=len(matched))
    category_name = source_filter or f"UP主(mid={owner_mid})"

    threading.Thread(
        target=_run_delete_bg,
        args=(task_id, matched, category_name),
        daemon=True,
    ).start()

    return jsonify({"success": True, "task_id": task_id, "total": len(matched),
                     "message": f"开始删除「{category_name}」({len(matched)} 个视频)..."})


def _run_delete_bg(task_id: str, bvids: list, category_name: str):
    """后台删除指定 BV 列表的所有文件（视频 + 评论 + 报告）。"""
    deleted_v = deleted_c = deleted_r = 0
    total = len(bvids)

    for i, bvid in enumerate(bvids):
        for label, path in [
            ("v", Path(DATA_DIR) / "videos" / f"{bvid}.json"),
            ("c", Path(DATA_DIR) / "comments" / f"{bvid}_comments.json"),
            ("r", Path(DATA_DIR) / "reports" / f"{bvid}_report.json"),
        ]:
            try:
                if path.exists():
                    path.unlink()
                    if label == "v": deleted_v += 1
                    elif label == "c": deleted_c += 1
                    else: deleted_r += 1
            except Exception:
                pass
        # 每 10 个更新一次进度
        if (i + 1) % 10 == 0 or i + 1 == total:
            DeleteTaskTracker.update(task_id,
                done=i + 1,
                progress=f"已删除 {i+1}/{total}...",
            )

    result = {
        "video_count": total,
        "deleted": {"videos": deleted_v, "comments": deleted_c, "reports": deleted_r},
        "message": f"已删除「{category_name}」: {total} 个视频, {deleted_v} 视频文件, {deleted_c} 评论文件, {deleted_r} 报告文件",
    }
    DeleteTaskTracker.update(task_id, status="done", progress="删除完成", done=total, result=result)


@app.route("/api/data/category-status/<task_id>")
def api_delete_category_status(task_id: str):
    """轮询删除进度。"""
    status = DeleteTaskTracker.get_status(task_id)
    return jsonify({"success": True, "data": status})


@app.route("/api/video/<bvid>/refresh", methods=["POST"])
def api_video_refresh(bvid: str):
    """刷新单个视频数据：清除去重 + 注入种子 + 启动爬虫 + 重新生成报告。

    用于视频详情页的「刷新数据」按钮。
    重新抓取当前视频的最新信息和评论。
    """
    result = spider_mgr.refresh_video(bvid)
    # 后台线程重新生成报告（基于当前磁盘上的评论数据）
    threading.Thread(target=_regenerate_report, args=(bvid,), daemon=True).start()
    return jsonify(result)


def _regenerate_report(bvid: str):
    """基于当前评论数据重新生成水军分析报告（后台线程）。"""
    try:
        from analyzer.feature_extractor import FeatureExtractor
        from analyzer.scorer import WaterArmyScorer
        from analyzer.report_generator import ReportGenerator
        from analyzer.similarity_detector import SimilarityDetector
        from analyzer.time_analyzer import TimeAnalyzer

        # 1. 加载评论
        comment_file = Path(DATA_DIR) / "comments" / f"{bvid}_comments.json"
        if not comment_file.exists():
            logger.warning(f"[report] No comment file for {bvid}, skip regenerate")
            return
        with open(comment_file, "r", encoding="utf-8") as f:
            comments = json.load(f)
        if not comments:
            return

        # 2. 加载视频信息
        video_info = _load_video_info(bvid) or {}
        if not video_info:
            video_info = {"bvid": bvid, "title": f"[{bvid}]"}

        # 3. 加载用户数据 (F12-F14 需要)
        users = {}
        user_posts = {}
        users_dir = Path(DATA_DIR) / "users"
        if users_dir.exists():
            for fname in os.listdir(str(users_dir)):
                fpath = users_dir / fname
                if fname.endswith(".json") and fname != "unique_mids.json" and not fname.endswith("_posts.json"):
                    with open(fpath, "r", encoding="utf-8") as uf:
                        user_data = json.load(uf)
                        users[user_data.get("mid")] = user_data
                elif fname.endswith("_posts.json"):
                    try:
                        mid = int(fname.replace("_posts.json", ""))
                    except ValueError:
                        continue
                    with open(fpath, "r", encoding="utf-8") as uf:
                        posts_data = json.load(uf)
                        user_posts[mid] = posts_data if isinstance(posts_data, list) else []

        # 4. 相似度检测
        sim_detector = SimilarityDetector(comments, threshold=0.75)
        sim_detector.build_matrix()
        clusters = sim_detector.find_clusters()

        # 5. 时间分析
        time_analyzer = TimeAnalyzer(comments, users)
        burst_scores = time_analyzer.detect_time_burst()
        batch_scores = time_analyzer.detect_registration_batch()
        timeline = time_analyzer.get_comment_timeline()

        # 6. 特征提取
        extractor = FeatureExtractor(
            comments, users,
            sim_detector.get_user_similarity_score,
            burst_scores,
            batch_scores,
            user_posts=user_posts,
        )
        features_list = extractor.extract_all()

        # 7. 评分
        scorer = WaterArmyScorer()
        scored_users = scorer.score_users(features_list)
        stats = scorer.get_statistics(scored_users)

        # 8. 生成并保存报告
        generator = ReportGenerator(
            video_bvid=bvid,
            video_info=video_info,
            scored_users=scored_users,
            stats=stats,
            similarity_clusters=clusters,
            timeline=timeline,
            llm_summary=None,
            llm_stats=None,
            deep_summary=None,
            deep_stats=None,
            comments=comments,
        )
        report = generator.generate()
        generator.save_report()
        logger.info(f"[report] Regenerated report for {bvid}: {stats['total_users']} users")

        # 9. 自动收录高风险水军账号 (v2.15)
        try:
            from dashboard.water_army_store import WaterArmyStore
            n = WaterArmyStore.batch_add_from_report(scored_users, bvid=bvid)
            if n > 0:
                logger.info(f"[water_army] Auto-collected {n} high-risk accounts from {bvid}")
        except Exception as we:
            logger.warning(f"[water_army] Auto-collect failed for {bvid}: {we}")

    except Exception as e:
        logger.error(f"[report] Failed to regenerate report for {bvid}: {e}")


# ============================================================
#  水军账号管理 API + 页面 (v2.15)
# ============================================================

@app.route("/water-army")
def water_army_page():
    """水军账号管理页面"""
    from dashboard.water_army_store import WaterArmyStore
    stats = WaterArmyStore.stats()
    return render_template("water_army.html", stats=stats)


@app.route("/api/water-army/stats")
def api_water_army_stats():
    from dashboard.water_army_store import WaterArmyStore
    return jsonify({"success": True, "data": WaterArmyStore.stats()})


@app.route("/api/water-army/list")
def api_water_army_list():
    sort_by = request.args.get("sort_by", "score")
    order = request.args.get("order", "desc")
    risk_filter = request.args.get("risk", "")
    search = request.args.get("search", "")
    added_by = request.args.get("added_by", "")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))

    from dashboard.water_army_store import WaterArmyStore
    result = WaterArmyStore.list_all(
        sort_by=sort_by, order=order, risk_filter=risk_filter,
        search=search, added_by=added_by, page=page, per_page=per_page,
    )

    # 从评论数据中补充缺失的头像
    missing = [a for a in result.get("data", []) if not a.get("avatar")]
    if missing:
        mid_set = {str(a["mid"]) for a in missing}
        avatar_map = {}
        comment_dir = Path(DATA_DIR) / "comments"
        for cf in comment_dir.glob("*_comments.json"):
            if not mid_set:
                break
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                comments = data if isinstance(data, list) else data.get("comments", data.get("replies", []))
                for c in comments:
                    if isinstance(c, dict):
                        c_mid = str(c.get("mid", ""))
                        if c_mid in mid_set and c_mid not in avatar_map:
                            avatar_map[c_mid] = c.get("avatar", "")
                            mid_set.discard(c_mid)
                            if not mid_set:
                                break
            except Exception:
                continue
        for a in missing:
            if str(a["mid"]) in avatar_map:
                a["avatar"] = avatar_map[str(a["mid"])]

    return jsonify({"success": True, **result})


@app.route("/api/water-army/<int:mid>")
def api_water_army_detail(mid: int):
    from dashboard.water_army_store import WaterArmyStore
    account = WaterArmyStore.get(mid)
    if not account:
        return jsonify({"success": False, "message": f"账号 MID={mid} 未收录"}), 404
    return jsonify({"success": True, "data": account})


@app.route("/api/water-army/add", methods=["POST"])
def api_water_army_add():
    data = request.get_json(silent=True) or {}
    mid = data.get("mid", 0)
    if not mid:
        return jsonify({"success": False, "message": "缺少 MID 参数"}), 400

    from dashboard.water_army_store import WaterArmyStore
    reason = data.get("reason")
    account = WaterArmyStore.add(
        mid=mid,
        uname=data.get("uname", ""),
        avatar=data.get("avatar", ""),
        suspicion_score=data.get("score", 0),
        risk_level=data.get("risk_level", "low"),
        added_by="manual",
        reason=reason,
        bvid=data.get("bvid", ""),
        notes=data.get("notes", ""),
    )
    return jsonify({"success": True, "data": account, "message": "已收录水军账号"})


@app.route("/api/water-army/<int:mid>", methods=["DELETE"])
def api_water_army_remove(mid: int):
    from dashboard.water_army_store import WaterArmyStore
    ok = WaterArmyStore.remove(mid)
    return jsonify({"success": ok, "message": "已移除" if ok else "未找到该账号"})


@app.route("/api/water-army/batch-remove", methods=["POST"])
def api_water_army_batch_remove():
    data = request.get_json(silent=True) or {}
    mids = data.get("mids", [])
    if not mids:
        return jsonify({"success": False, "message": "缺少 mids 参数"}), 400
    from dashboard.water_army_store import WaterArmyStore
    n = WaterArmyStore.batch_remove(mids)
    return jsonify({"success": True, "removed": n, "message": f"已移除 {n} 个账号"})


@app.route("/api/water-army/<int:mid>/notes", methods=["PUT"])
def api_water_army_update_notes(mid: int):
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "")
    from dashboard.water_army_store import WaterArmyStore
    WaterArmyStore.update_notes(mid, notes)
    return jsonify({"success": True, "message": "备注已更新"})


@app.route("/api/water-army/export")
def api_water_army_export():
    from dashboard.water_army_store import WaterArmyStore
    accounts = WaterArmyStore.export_json()
    fmt = request.args.get("format", "json")
    if fmt == "csv":
        import csv, io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["mid", "uname", "suspicion_score", "risk_level", "added_by", "added_at", "reasons_count", "sources_count"])
        for a in accounts:
            writer.writerow([
                a.get("mid"), a.get("uname"), a.get("suspicion_score"),
                a.get("risk_level"), a.get("added_by"), a.get("added_at"),
                len(a.get("reasons", [])), len(a.get("sources", [])),
            ])
        csv_str = output.getvalue()
        resp = make_response(csv_str)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
        resp.headers["Content-Disposition"] = "attachment; filename=water_army_accounts.csv"
        return resp
    return jsonify({"success": True, "data": accounts, "total": len(accounts)})


# ============================================================
#  系统状态 API (v2.0 新增)
# ============================================================

@app.route("/api/system/status")
def api_system_status():
    """聚合所有子系统健康状态"""
    return jsonify({
        "success": True,
        "data": system_monitor.get_full_status(),
    })


@app.route("/api/system/health")
def api_system_health():
    """轻量级健康检查（run_all.bat 使用）"""
    return jsonify({"status": "ok", "service": "bilibili-sentinel-dashboard"})


@app.route("/api/proxy/status")
def api_proxy_status():
    """代理池统计"""
    return jsonify({"success": True, "data": system_monitor.get_proxy_status()})


@app.route("/api/proxy/refresh", methods=["POST"])
def api_proxy_refresh():
    """手动刷新代理池"""
    result = system_monitor.refresh_proxy()
    return jsonify(result)


@app.route("/api/proxy/clash-test", methods=["POST"])
def api_proxy_clash_test():
    """测试 Clash Verge 代理连通性（通过 proxy 访问 B站 API）"""
    import time
    import requests as _requests
    
    data = request.get_json(silent=True) or {}
    proxy_url = data.get("proxy_url", "").strip()
    
    if not proxy_url:
        return jsonify({"success": False, "message": "未提供代理地址"})
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com",
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    
    try:
        t0 = time.time()
        r = _requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers, proxies=proxies, timeout=10
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        
        api_code = None
        try:
            api_code = r.json().get("code")
        except Exception:
            pass
        
        if r.status_code == 200:
            return jsonify({
                "success": True,
                "message": f"代理连通，B站 API 正常响应",
                "api_code": api_code,
                "elapsed_ms": elapsed_ms,
            })
        else:
            return jsonify({
                "success": False,
                "message": f"代理连接成功但 B站 返回 HTTP {r.status_code}",
                "api_code": api_code,
                "elapsed_ms": elapsed_ms,
            })
    except _requests.exceptions.ProxyError as e:
        return jsonify({
            "success": False,
            "message": f"代理连接失败: {e}"
        })
    except _requests.exceptions.ConnectTimeout:
        return jsonify({
            "success": False,
            "message": "连接代理超时，请检查 Clash Verge 是否运行"
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"测试失败: {e}"
        })


@app.route("/api/cache/status")
def api_cache_status():
    """缓存统计"""
    return jsonify({"success": True, "data": system_monitor.get_cache_status()})


@app.route("/api/llm/status")
def api_llm_status():
    """LLM / 深度分析实时状态（前端设置页面 + 深度分析按钮使用）"""
    try:
        data = system_monitor.get_llm_status()
        # 附加字段: 前端深度分析按钮判断是否可用
        llm_ok = data.get("available", False)
        return jsonify({
            "success": True,
            "data": data,
            "llm_available": llm_ok,
            "llm_provider": data.get("provider"),
            "llm_model": data.get("model"),
            "deep_available": llm_ok,  # 深度分析可用 = LLM 已就绪
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """清空缓存"""
    result = system_monitor.clear_cache()
    return jsonify(result)


@app.route("/api/store/status")
def api_store_status():
    """存储层统计"""
    return jsonify({"success": True, "data": system_monitor.get_store_status()})


@app.route("/api/config/view")
def api_config_view():
    """查看当前配置"""
    return jsonify({"success": True, "data": system_monitor.get_config_summary()})


@app.route("/api/config/update", methods=["POST"])
def api_config_update():
    """更新运行时配置（内存 + 持久化到 runtime_config.json，爬虫新进程也可读取）"""
    body = request.get_json(silent=True) or {}
    key = body.get("key", "")
    value = body.get("value")
    if not key:
        return jsonify({"success": False, "message": "缺少配置键"}), 400
    try:
        import config.base_config
        # 1. 更新内存
        if hasattr(config.base_config, key):
            old = getattr(config.base_config, key)
            setattr(config.base_config, key, value)
        else:
            return jsonify({"success": False, "message": f"未知配置键: {key}"}), 400

        # 2. 持久化到运行时配置文件
        runtime_path = os.path.join(PROJECT_ROOT, "config", "runtime_config.json")
        runtime = {}
        if os.path.exists(runtime_path):
            try:
                with open(runtime_path, "r", encoding="utf-8") as f:
                    runtime = json.load(f)
            except Exception:
                pass
        runtime[key] = value
        with open(runtime_path, "w", encoding="utf-8") as f:
            json.dump(runtime, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        return jsonify({
            "success": True,
            "message": f"{key}: {old} → {value} (已持久化)",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ============================================================
#  LLM 配置 API — 持久化到 config/llm_config.json
#  支持 DeepSeek V4 / OpenAI / 自定义 OpenAI 兼容端点
# ============================================================

LLM_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "llm_config.json")

LLM_DEFAULTS = {
    "enabled": False,
    "provider": "deepseek",
    "api_key": "",
    "base_url": "",
    "model": "deepseek-v4-pro",
    "score_threshold": 30,
    "engine_weight": 0.75,
    "llm_weight": 0.25,
    "max_batches": 10,
    "users_per_batch": 5,
    # AICU 深度分析
    "deep_analysis_enabled": False,
    "aicu_cookie": "",
    "deep_score_threshold": 70,
}

LLM_PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-v4-pro", "label": "V4 Pro (推理增强)"},
            {"id": "deepseek-v4-flash", "label": "V4 Flash (快速文本)"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4o-mini", "label": "GPT-4o Mini"},
            {"id": "gpt-4-turbo", "label": "GPT-4 Turbo"},
            {"id": "o3-mini", "label": "o3-mini"},
        ],
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "models": [],
    },
}


def _load_llm_config() -> dict:
    """加载 LLM 持久化配置；不存在则返回默认值"""
    try:
        if os.path.exists(LLM_CONFIG_PATH):
            with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 合并默认值防止缺失字段
            return {**LLM_DEFAULTS, **cfg}
    except Exception:
        pass
    return dict(LLM_DEFAULTS)


def _save_llm_config(cfg: dict):
    """保存 LLM 配置到磁盘"""
    os.makedirs(os.path.dirname(LLM_CONFIG_PATH), exist_ok=True)
    with open(LLM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@app.route("/api/llm/config/view")
def api_llm_config_view():
    """查看 LLM 配置（含可用 provider 列表）"""
    cfg = _load_llm_config()
    # 脱敏：不返回完整 API Key
    masked = dict(cfg)
    if masked.get("api_key") and len(masked["api_key"]) > 8:
        masked["api_key"] = masked["api_key"][:4] + "****" + masked["api_key"][-4:]
    # 添加实时状态
    try:
        from analyzer.llm_analyzer import create_llm_analyzer
        llm = create_llm_analyzer()
        masked["live_available"] = llm is not None
        masked["deep_available"] = llm is not None  # 深度分析可用 = LLM 正常 (已改为手动触发)
    except Exception:
        masked["live_available"] = False
        masked["deep_available"] = False
    # 附加 provider 预设信息供前端使用
    masked["providers"] = {
        k: {"label": v["label"], "base_url": v["base_url"], "models": v["models"]}
        for k, v in LLM_PROVIDER_PRESETS.items()
    }
    return jsonify({"success": True, "data": masked})


@app.route("/api/llm/config/update", methods=["POST"])
def api_llm_config_update():
    """更新 LLM 配置（持久化到磁盘）"""
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"success": False, "message": "请求体为空"}), 400

    cfg = _load_llm_config()
    changed = []
    for key in LLM_DEFAULTS:
        if key in body:
            old = cfg.get(key)
            new_val = body[key]
            # 防止脱敏字符串被写回：如果 api_key 包含星号，跳过（保留磁盘上的原值）
            if key == "api_key" and new_val and "*" in str(new_val):
                continue
            cfg[key] = new_val
            if key == "api_key" and new_val:
                changed.append(f"{key}: **** → ****")
            else:
                changed.append(f"{key}: {old} → {new_val}")

    if not changed:
        return jsonify({"success": False, "message": "未提供有效配置项"}), 400

    _save_llm_config(cfg)

    # 立即同步到环境变量（兼容多种 provider）
    if cfg.get("api_key"):
        os.environ["DEEPSEEK_API_KEY"] = cfg["api_key"]
        os.environ["OPENAI_API_KEY"] = cfg["api_key"]

    # 同步到 base_config
    try:
        import config.base_config as bc
        bc.ENABLE_LLM_ANALYSIS = cfg["enabled"]
        bc.ENABLE_DEEP_ANALYSIS = cfg.get("deep_analysis_enabled", False)
        if cfg.get("aicu_cookie"):
            bc.AICU_COOKIE = cfg["aicu_cookie"]
    except Exception:
        pass

    return jsonify({
        "success": True,
        "message": f"已更新 {len(changed)} 项: {', '.join(changed)}",
    })


@app.route("/api/llm/config/test", methods=["POST"])
def api_llm_config_test():
    """测试 LLM 连接（不扣费：发送极短 prompt）"""
    body = request.get_json(silent=True) or {}
    api_key = body.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({"success": False, "message": "未提供 API Key"}), 400

    provider = body.get("provider", "deepseek")
    base_url = body.get("base_url", "")
    if not base_url and provider in LLM_PROVIDER_PRESETS:
        base_url = LLM_PROVIDER_PRESETS[provider]["base_url"]

    try:
        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=15,
        )
        resp = client.chat.completions.create(
            model=body.get("model", "deepseek-v4-pro"),
            messages=[
                {"role": "system", "content": "你是一个测试助手。"},
                {"role": "user", "content": "回复：OK"},
            ],
            max_tokens=5,
            temperature=0,
        )
        reply = resp.choices[0].message.content if resp.choices else "无回复"
        return jsonify({
            "success": True,
            "message": f"连接成功! 回复: {reply}",
            "model": resp.model,
            "provider": provider,
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            },
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"连接失败: {str(e)}"}), 500


@app.route("/api/llm/aicu/test", methods=["POST"])
def api_aicu_test():
    """测试 AICU API 连接（使用测试 MID=2 查询历史评论）"""
    import time as _time
    try:
        from analyzer.aicu_fetcher import AicuFetcher
        cfg = _load_llm_config()
        cookie = cfg.get("aicu_cookie", "")
        fetcher = AicuFetcher(cookie=cookie, timeout=15)

        t0 = _time.monotonic()
        data = fetcher.fetch_user_comments(2, page_size=20)
        latency = round((_time.monotonic() - t0) * 1000)

        return jsonify({
            "success": True,
            "message": "AICU API 可用",
            "test_mid": 2,
            "comment_count": data.get("count", 0),
            "latency_ms": latency,
            "stats": data.get("stats", {}),
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"AICU 连接失败: {str(e)}"}), 500


# ============================================================
#  爬虫控制 API 路由
# ============================================================

@app.route("/api/crawler/status")
def api_crawler_status():
    global _user_spider_auto_started
    data = spider_mgr.get_status()
    data["_user_auto_queued"] = _comment_finished_trigger_user  # 等待触发
    data["_user_auto_started"] = _user_spider_auto_started       # 已自动启动（前端消费后清零）
    if _user_spider_auto_started:
        _user_spider_auto_started = False  # ★ 消费后清零，避免重复提示
    return jsonify({"success": True, "data": data})


VALID_SPIDERS = ("bilibili_video", "bilibili_comment", "bilibili_user")


@app.route("/api/crawler/start/<spider_name>", methods=["POST"])
def api_crawler_start(spider_name: str):
    if spider_name not in VALID_SPIDERS:
        return jsonify({"success": False, "message": f"未知爬虫: {spider_name}"}), 400
    result = spider_mgr.start_spider(spider_name)

    # ★ 自动联动：启动视频/评论爬虫时顺便启动用户爬虫（确保用户空间数据被采集）
    if spider_name in ("bilibili_video", "bilibili_comment"):
        _auto_start_user_spider()

    return jsonify(result)


@app.route("/api/crawler/stop/<spider_name>", methods=["POST"])
def api_crawler_stop(spider_name: str):
    if spider_name not in VALID_SPIDERS:
        return jsonify({"success": False, "message": f"未知爬虫: {spider_name}"}), 400
    return jsonify(spider_mgr.stop_spider(spider_name))


@app.route("/api/crawler/force-stop/<spider_name>", methods=["POST"])
def api_crawler_force_stop(spider_name: str):
    """强制停止爬虫（核武器级）：杀进程 + 清队列 + 重置状态。

    适用于爬虫卡在 spider_idle/DontCloseSpider 循环中、常规 stop 无效的场景。
    """
    if spider_name not in VALID_SPIDERS:
        return jsonify({"success": False, "message": f"未知爬虫: {spider_name}"}), 400
    return jsonify(spider_mgr.force_stop(spider_name))


@app.route("/api/crawler/rescan-comment-seeds", methods=["POST"])
def api_crawler_rescan_comment_seeds():
    """扫描已有视频数据，为有评论但未爬取的视频重新注入评论种子。

    适用场景：评论爬虫意外中断后，Redis 种子丢失，但视频 JSON 和评论数据仍在。
    此接口扫描 data/videos/*.json，找到 reply_count > 0 但无对应评论文件的视频，
    将其 BVID 注入 Redis comment_seeds 队列。
    """
    import redis as redis_mod
    try:
        r = redis_mod.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        r.ping()
    except Exception:
        return jsonify({"success": False, "message": "Redis 连接失败"}), 503

    # 扫描视频文件
    video_dir = Path(DATA_DIR) / "videos"
    comment_dir = Path(DATA_DIR) / "comments"
    injected = 0
    skipped_no_reply = 0
    skipped_has_comment = 0

    for vf in sorted(video_dir.glob("*.json")):
        bvid = vf.stem
        # 检查是否已有评论文件
        if (comment_dir / f"{bvid}_comments.json").exists():
            skipped_has_comment += 1
            continue
        try:
            with open(vf, "r", encoding="utf-8") as f:
                data = json.load(f)
            reply = data.get("reply_count", 0)
            aid = data.get("aid", 0)
            if not reply or not aid:
                skipped_no_reply += 1
                continue
            # 注入种子
            task = json.dumps({"bvid": bvid, "aid": int(aid), "reply_count": int(reply)})
            r.lpush("bilibili_crawler:comment_seeds", task)
            injected += 1
        except Exception:
            continue

    return jsonify({
        "success": True,
        "injected": injected,
        "skipped_has_comment": skipped_has_comment,
        "skipped_no_reply": skipped_no_reply,
        "total_videos": skipped_has_comment + skipped_no_reply + injected,
        "message": f"已从 {injected + skipped_has_comment + skipped_no_reply} 个视频中注入 {injected} 条评论种子"
    })


@app.route("/api/crawler/rescan-user-seeds", methods=["POST"])
def api_crawler_rescan_user_seeds():
    """扫描已有评论数据，提取所有评论者 MID，注入用户爬虫种子队列。

    扫描 data/comments/*_comments.json，提取每个评论的 mid，
    去重后注入 Redis user_seeds 队列。
    """
    import redis as redis_mod
    try:
        r = redis_mod.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        r.ping()
    except Exception:
        return jsonify({"success": False, "message": "Redis 连接失败"}), 503

    comment_dir = Path(DATA_DIR) / "comments"
    all_mids = set()
    scanned = 0

    for cf in comment_dir.glob("*_comments.json"):
        try:
            with open(cf, "r", encoding="utf-8") as f:
                data = json.load(f)
            comments = data if isinstance(data, list) else data.get("comments", [])
            for c in comments:
                if isinstance(c, dict) and c.get("mid"):
                    all_mids.add(int(c["mid"]))
            scanned += 1
        except Exception:
            continue

    injected = 0
    for mid in all_mids:
        r.rpush("bilibili_crawler:user_seeds", json.dumps({"mid": mid}))
        injected += 1

    return jsonify({
        "success": True,
        "scanned_comments": scanned,
        "unique_mids": len(all_mids),
        "injected": injected,
        "message": f"从 {scanned} 个评论文件中提取 {injected} 个唯一 MID，已注入用户爬虫种子队列"
    })


@app.route("/api/crawler/start-both", methods=["POST"])
def api_crawler_start_both():
    results = {}
    for name in ("bilibili_video", "bilibili_comment"):
        results[name] = spider_mgr.start_spider(name)
        time.sleep(0.3)
    # ★ 自动联动用户爬虫
    results["bilibili_user"] = _auto_start_user_spider()
    return jsonify({"success": True, "results": results})


# ============================================================
#  ★ 统一的用户数据加载与特征刷新 (LLM / AICU 共用)
# ============================================================

def _load_fresh_users(mids: set = None) -> dict:
    """从 data/users/ 加载用户数据，返回 {str(mid): user_data}。"""
    users = {}
    users_dir = Path(DATA_DIR) / "users"
    if not users_dir.exists():
        return users
    for fname in os.listdir(str(users_dir)):
        if not fname.endswith(".json") or fname.endswith("_posts.json") or fname == "unique_mids.json":
            continue
        try:
            mid_name = fname.replace(".json", "")
            with open(users_dir / fname, "r", encoding="utf-8") as uf:
                ud = json.load(uf)
                mid_key = str(ud.get("mid", mid_name))
                if mids is not None and mid_key not in mids:
                    continue
                users[mid_key] = ud
        except Exception:
            pass
    return users


def _refresh_features(raw_features: dict, user_data: dict, uname: str = "") -> dict:
    """根据最新用户数据刷新 F4/F12，返回新的 features dict。
    如果没有用户数据（user_data 为空），不修改特征分数，保留原值。"""
    f = dict(raw_features or {})
    ud = user_data or {}
    if not ud:
        return f  # ★ 无数据 → 不修改，保留原始评分

    face = ud.get("face", "").lower()
    # F4: 头像/认证
    f4 = 0.0
    if not face or "noface" in face: f4 += 0.50
    official = ud.get("official_verify", {})
    if isinstance(official, str):
        try: official = json.loads(official)
        except Exception: official = {}
    if not official or official.get("type", -1) == -1: f4 += 0.50
    f["f4_avatar_verify"] = round(min(1.0, f4) * 100, 1)

    # F12: 账号骨架
    f12 = 0.0
    if not face or "noface" in face: f12 += 0.20
    if not uname or (uname.startswith("bili_") and len(uname) > 8): f12 += 0.20
    if ud.get("post_count") == 0: f12 += 0.20
    if ud.get("upload_count", -1) == 0: f12 += 0.20
    if ud.get("sign", "") == "这个人没有填简介啊~~~": f12 += 0.20
    f["f12_account_skeleton"] = round(f12 * 100, 1)

    return f


def _build_raw_profile_line(user_data: dict) -> str:
    """构建用户原始数据摘要行，供 LLM prompt 使用。"""
    if not user_data:
        return "- ⚠️ 用户数据: 未采集 (用户爬虫未运行, 头像/动态/投稿未知)"
    parts = []
    face = user_data.get("face", "")
    parts.append("头像:" + ("无" if not face or "noface" in face else "有"))
    parts.append(f"动态:{user_data.get('post_count', '?')}条")
    uploads = user_data.get("upload_count", -1)
    parts.append(f"投稿:{uploads}个" if uploads >= 0 else "投稿:未知")
    sign = user_data.get("sign", "")
    if sign == "这个人没有填简介啊~~~":
        parts.append("签名:默认(未修改)")
    elif sign:
        parts.append(f"签名:{sign[:30]}")
    else:
        parts.append("签名:空")
    return "- 原始数据: " + " | ".join(parts)


def _auto_start_user_spider() -> dict:
    """注入用户种子，标记等待评论爬虫完成后自动启动。"""
    try:
        result = spider_mgr.inject_seeds("rescan_users")
        injected = result.get("injected", 0)
        # ★ 标记：评论爬虫完成后应触发用户爬虫
        global _comment_finished_trigger_user
        _comment_finished_trigger_user = True
        return {"success": True, "injected": injected,
                "message": f"已注入 {injected} 个用户种子，等待评论爬虫完成后自动启动用户爬虫"}
    except Exception as e:
        return {"success": False, "message": f"注入失败: {e}"}


# ★ 后台监控：评论爬虫完成后自动启动用户爬虫
_comment_finished_trigger_user = False
_last_comment_running = False
_user_spider_auto_started = False  # ★ 前端通知标记

def _check_comment_to_user() -> None:
    """每秒检查一次：如果评论爬虫从运行→停止，自动启动用户爬虫。"""
    global _comment_finished_trigger_user, _last_comment_running, _user_spider_auto_started
    import time as _time
    while True:
        try:
            state = spider_mgr._read_state()
            comment_running = state.get("bilibili_comment", {}).get("status") == "running"
            user_running = state.get("bilibili_user", {}).get("status") == "running"

            if _last_comment_running and not comment_running and _comment_finished_trigger_user and not user_running:
                logger.info("评论爬虫已完成，自动启动用户爬虫...")
                spider_mgr.inject_seeds("rescan_users")
                result = spider_mgr.start_spider("bilibili_user")
                if result.get("success"):
                    logger.info(f"用户爬虫已自动启动 (PID={result.get('pid')})")
                    _user_spider_auto_started = True  # ★ 通知前端
                _comment_finished_trigger_user = False

            _last_comment_running = comment_running
        except Exception:
            pass
        _time.sleep(2)


@app.route("/api/crawler/stop-all", methods=["POST"])
def api_crawler_stop_all():
    return jsonify(spider_mgr.stop_all())


@app.route("/api/crawler/inject", methods=["POST"])
def api_crawler_inject():
    body = request.get_json(silent=True) or {}
    seed_type = body.get("type", "")
    kwargs = {k: v for k, v in body.items() if k != "type"}
    return jsonify(spider_mgr.inject_seeds(seed_type, **kwargs))


@app.route("/api/crawler/clear", methods=["POST"])
def api_crawler_clear():
    return jsonify(spider_mgr.clear_queues())


@app.route("/api/crawler/log/<spider_name>")
def api_crawler_log(spider_name: str):
    lines = request.args.get("lines", 50, type=int)
    log_info = _read_spider_log(spider_name, tail_lines=lines)
    if log_info["total_lines"] == 0:
        return jsonify({"success": False, "message": "暂无日志（爬虫未启动或日志文件不存在）"}), 404
    return jsonify({"success": True, "data": log_info})


@app.route("/api/crawler/stream/<spider_name>")
def api_crawler_stream(spider_name: str):
    """SSE 实时流式推送爬虫日志。

    连接到该端点后，先发送最近 100 行历史日志（让用户看到上下文），
    然后每秒从共享日志文件尾部读取新行，通过 SSE 推送到前端。
    支持 EventSource 自动重连。
    """
    marker = f"[{spider_name}]"
    valid_spiders = {"bilibili_video", "bilibili_comment", "bilibili_user"}
    if spider_name not in valid_spiders:
        marker = f"[{spider_name}]"  # 允许任意 spider_name（宽松匹配）

    def generate():
        # 第一阶段：发送最近 100 行历史日志
        if os.path.exists(CRAWLER_LOG_PATH):
            try:
                with open(CRAWLER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                matched = [ln.rstrip("\n") for ln in all_lines if marker in ln]
                history = matched[-100:]
                for line in history:
                    yield f"data: {json.dumps({'line': line, 'type': 'history'})}\n\n"
            except Exception:
                pass

        # 发送一条连接就绪信号
        yield f"data: {json.dumps({'line': '', 'type': 'ready', 'message': f'已连接，正在监听 {spider_name} 日志...'})}\n\n"

        # 第二阶段：尾部实时跟踪
        last_pos = os.path.getsize(CRAWLER_LOG_PATH) if os.path.exists(CRAWLER_LOG_PATH) else 0
        empty_polls = 0
        max_empty = 600  # 10 分钟无新日志后断开（前端自动重连）

        while empty_polls < max_empty:
            if not os.path.exists(CRAWLER_LOG_PATH):
                time.sleep(1)
                empty_polls += 1
                continue

            try:
                current_size = os.path.getsize(CRAWLER_LOG_PATH)
                if current_size < last_pos:
                    # 日志文件被截断/轮转了，重置位置
                    last_pos = 0
                    yield f"data: {json.dumps({'line': '', 'type': 'truncated', 'message': '⚠ 日志文件已轮转'})}\n\n"

                if current_size > last_pos:
                    with open(CRAWLER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        new_data = f.read(current_size - last_pos)
                    last_pos = current_size

                    new_lines = new_data.split("\n")
                    for line in new_lines:
                        if not line.strip():
                            continue
                        if marker in line:
                            yield f"data: {json.dumps({'line': line.rstrip('\r'), 'type': 'live'})}\n\n"
                    empty_polls = 0
                else:
                    empty_polls += 1
            except Exception:
                empty_polls += 1

            time.sleep(1)

        # 超时断开，发送关闭信号（前端 EventSource 会自动重连）
        yield f"data: {json.dumps({'line': '', 'type': 'timeout', 'message': '长时间无新日志，连接即将重新建立...'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ============================================================
#  系统控制 API
# ============================================================

@app.route("/api/system/shutdown", methods=["POST"])
def api_system_shutdown():
    """优雅关闭 Dashboard 服务。

    先停止所有爬虫，再关闭自身。
    run_all.bat 的关闭流程会先调用此接口，而非暴力 taskkill。
    """
    # 1. 停止所有爬虫
    spider_mgr.stop_all()

    # 2. 计划关闭 Flask（给 HTTP 响应一点时间发送）
    def _shutdown():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()

    return jsonify({"success": True, "message": "Dashboard 正在关闭...", "spiders_stopped": True})


# ============================================================
#  Login API
# ============================================================

@app.route("/api/login/status")
def api_login_status():
    try:
        from bilibili_crawler.login.login_manager import LoginManager
        mgr = LoginManager()
        return jsonify({
            "success": True,
            "data": {
                "is_logged_in": mgr.is_logged_in(),
                "has_sessdata": mgr.get_sessdata() is not None,
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/login/qrcode", methods=["POST"])
def api_login_qrcode():
    try:
        from bilibili_crawler.login.bilibili_login import BilibiliLogin
        import requests as req
        resp = req.get(
            BilibiliLogin.QRCODE_API,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "https://www.bilibili.com",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            return jsonify({"success": False, "message": data.get("message", "生成失败")}), 400
        qr = data["data"]
        return jsonify({"success": True, "data": {"qrcode_key": qr["qrcode_key"], "qrcode_url": qr["url"]}})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/login/poll/<qrcode_key>")
def api_login_poll(qrcode_key):
    try:
        from bilibili_crawler.login.bilibili_login import BilibiliLogin
        import requests as req
        login = BilibiliLogin()

        # ★ 使用代理（如果需要）
        proxies = None
        try:
            from config.base_config import CLASH_PROXY_ENABLED, CLASH_PROXY_URL
            if CLASH_PROXY_ENABLED and CLASH_PROXY_URL:
                proxies = {"http": CLASH_PROXY_URL, "https": CLASH_PROXY_URL}
        except Exception:
            pass

        resp = req.get(
            BilibiliLogin.QRCODE_POLL_API,
            params={"qrcode_key": qrcode_key},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "https://passport.bilibili.com/login",
            },
            proxies=proxies,
            timeout=10,
        )
        poll_data = resp.json()
        # B站 QR poll: 外层 code=0 表示API成功, 内层 data.code: 0=已确认, 86090=已扫码未确认, 86101=未扫码
        data = poll_data.get("data", {})
        inner_code = data.get("code", -1)
        if inner_code == 0:
            # ★ 从 response headers 直接提取所有 cookie
            cookies = {}
            raw_cookies = resp.headers.get("set-cookie", "")
            logger.info(f"[Login] Raw Set-Cookie (first 200 chars): {raw_cookies[:200]}")

            # URL解码 cookie 值（B站 Set-Cookie 值是 URL 编码的，需解码后才能用）
            from urllib.parse import unquote
            for k, v in resp.cookies.items():
                cookies[k] = unquote(v)
            if isinstance(data, dict):
                refresh_token = data.get("refresh_token", "")
                if refresh_token:
                    cookies["bili_refresh_token"] = refresh_token

            logger.info(f"[Login] Cookie keys: {list(cookies.keys())}, SESSDATA前20: {cookies.get('SESSDATA','')[:20]}")
            if not cookies or "SESSDATA" not in cookies:
                logger.error(f"[Login] Cookie 提取失败! resp.cookies 为空或缺少 SESSDATA")
                return jsonify({"success": False, "message": "Cookie 提取失败，请重试"}), 500

            login._cookies = cookies
            login.save_login_state_sync()

            # ★ 方案二：登录成功后自动同步 Cookie 到 llm_config.json
            try:
                cookie_str = login.get_cookies_string()
                if cookie_str:
                    cfg = _load_llm_config()
                    cfg["aicu_cookie"] = cookie_str
                    _save_llm_config(cfg)
                    # 同步到内存中的配置，使正在运行的进程立即生效
                    try:
                        import config.base_config as _bc
                        _bc.AICU_COOKIE = cookie_str
                    except Exception:
                        pass
                    logger.info(f"Cookie 已自动同步到 llm_config.json (len={len(cookie_str)})")
            except Exception as _e:
                logger.warning(f"同步 Cookie 到 llm_config.json 失败: {_e}")

            # 同步到 LoginManager 单例，使 Dashboard 状态面板能检测到登录
            try:
                from bilibili_crawler.login.login_manager import LoginManager
                LoginManager._login = login
            except Exception:
                pass
            return jsonify({"success": True, "data": {"status": "success", "message": "登录成功"}})
        elif poll_data.get("code") == 86101:
            return jsonify({"success": True, "data": {"status": "waiting", "message": "等待扫码"}})
        elif inner_code == 86090:
            return jsonify({"success": True, "data": {"status": "scanned", "message": "已扫码，请确认"}})
        elif inner_code == 86038:
            return jsonify({"success": True, "data": {"status": "expired", "message": "二维码已过期"}})
        else:
            return jsonify({
                "success": True,
                "data": {"status": "unknown", "code": inner_code, "message": data.get("message", "")}
            })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ============================================================
#  启动
# ============================================================

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("=" * 60)
    print("  B站哨兵 Dashboard v2.0")
    print(f"  数据目录: {DATA_DIR}")
    print(f"  总览面板: http://127.0.0.1:5001")
    print(f"  爬虫控制: http://127.0.0.1:5001/crawler")
    print(f"  系统设置: http://127.0.0.1:5001/settings")
    print(f"  调试模式: {'开启' if debug_mode else '关闭（生产模式，无 reloader）'}")
    print("=" * 60)
    # ★ 启动后台监控线程（评论爬虫→用户爬虫自动联动）
    import os as _oss
    if not _oss.environ.get("WERKZEUG_RUN_MAIN"):  # 避免 Flask reloader 重复启动
        _check_thread = threading.Thread(target=_check_comment_to_user, daemon=True)
        _check_thread.start()
        logger.info("后台监控已启动: 评论爬虫完成后自动启动用户爬虫")
    app.run(host="0.0.0.0", port=5001, debug=debug_mode, use_reloader=debug_mode)
