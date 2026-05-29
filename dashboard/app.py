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
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, render_template, jsonify, request, Response, stream_with_context

from config import DATA_DIR, VIDEO_DIR, COMMENT_DIR, REPORT_DIR

app = Flask(__name__)
app.secret_key = "bilibili-sentinel-dashboard-v2"

SCRAPY_EXE = os.path.join(PROJECT_ROOT, "venv", "Scripts", "scrapy.exe")
SCRAPY_CWD = PROJECT_ROOT
CRAWLER_LOG_PATH = os.path.join(DATA_DIR, "logs", "bilibili_crawler.log")


def _read_spider_log(spider_name: str, tail_lines: int = 50) -> dict:
    """从共享的 Scrapy LOG_FILE 中按爬虫名过滤日志行。

    Scrapy LOG_FORMAT: "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    爬虫的 logger name 即爬虫名，所以过滤 [bilibili_video] / [bilibili_comment]
    """
    if not os.path.exists(CRAWLER_LOG_PATH):
        return {"total_lines": 0, "recent": "", "log_file": CRAWLER_LOG_PATH}
    try:
        with open(CRAWLER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        marker = f"[{spider_name}]"
        matched = [ln for ln in all_lines if marker in ln]
        recent = "".join(matched[-tail_lines:])
        return {
            "log_file": CRAWLER_LOG_PATH,
            "total_lines": len(matched),
            "recent": recent,
        }
    except Exception:
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
        def _fetch():
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

        return self._cached("login", _fetch)

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
                # 截断共享日志文件，避免旧运行的 [spider_idle] 污染 is_idle_stuck 检测
                shared_log = os.path.join(DATA_DIR, "logs", "bilibili_crawler.log")
                if os.path.exists(shared_log):
                    os.remove(shared_log)
                log_file = os.path.join(
                    DATA_DIR, "logs", f"{spider_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                )
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                with open(log_file, "w", encoding="utf-8") as log_f:
                    proc = subprocess.Popen(
                        [SCRAPY_EXE, "crawl", spider_name],
                        cwd=SCRAPY_CWD,
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
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
                    state[spider_name] = entry
                    self._write_state(state)
                    return {"success": True, "message": f"爬虫 {spider_name} 已停止（状态记录曾标记为 {entry.get('status', '?')}，但进程已被杀死）"}
                return {"success": False, "message": f"爬虫 {spider_name} 未在运行"}

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

            # 清空工作队列（dupefilter），但保留种子队列。
            # taskkill /T /F 已强杀进程树，不需要靠空队列来逼退。
            # 保留 start_urls / comment_seeds 避免用户刚注入的种子被误删。
            self._nuke_redis_queues(spider_name, keep_seeds=True)

            entry["status"] = "stopped"
            entry["stopped_at"] = datetime.now().isoformat()
            state[spider_name] = entry
            self._write_state(state)

            if killed:
                return {"success": True, "message": f"爬虫 {spider_name} 已停止（队列已清空）"}
            else:
                return {
                    "success": True,
                    "message": f"爬虫 {spider_name} 状态已停止，队列已清空。"
                               f"若进程未退出，下次 idle 检查（最长 30 秒）后将自动释放。",
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
        然后用 taskkill 杀进程（避免杀软误报）。
        """
        spider_to_spider = {
            "bilibili_video": "bilibili_video",
            "bilibili_comment": "bilibili_comment",
        }
        target = spider_to_spider.get(spider_name)
        if not target:
            return False
        try:
            ps_cmd = (
                f'Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
                f'Where-Object {{ $_.CommandLine -like \'*scrapy*crawl*{target}*\' }} | '
                f'Select-Object -ExpandProperty ProcessId'
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10,
            )
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            if not pids:
                return False
            killed_any = False
            for pid in pids:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/T", "/F"],
                        capture_output=True, timeout=10,
                    )
                    killed_any = True
                except Exception:
                    continue
            return killed_any
        except Exception:
            return False

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
        """最终兜底：杀光所有 bilibili scrapy 进程（窗口标题匹配，不涉及 WMI）。"""
        titles = [
            "Bilibili Video Spider",
            "Bilibili Comment Spider",
        ]
        for t in titles:
            try:
                subprocess.run(
                    ["taskkill", "/FI", f"WINDOWTITLE eq {t}*", "/F"],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    def get_status(self) -> dict:
        with self._lock:
            state = self._read_state()
            state_changed = False
            spiders = {}
            for name in ["bilibili_video", "bilibili_comment", "bilibili_user", "bilibili_danmaku"]:
                entry = state.get(name, {})
                pid = entry.get("pid")
                alive = self._is_process_alive(pid) if pid else False
                # 自愈：如果状态是 running 但进程已死，自动修正
                if entry.get("status") == "running" and not alive:
                    entry["status"] = "stopped"
                    entry["stopped_at"] = datetime.now().isoformat()
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
            return {
                "success": True,
                "message": f"已注入 {len(valid_mids)} 个用户种子 (UID: {valid_mids[:5]}{'...' if len(valid_mids) > 5 else ''})",
            }
        else:
            return {"success": False, "message": f"未知种子类型: {seed_type}"}

    def clear_queues(self) -> dict:
        try:
            import redis
            r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
            keys = [
                "bilibili_crawler:start_urls", "bilibili_crawler:comment_seeds",
                "bilibili_crawler:user_seeds",
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

        # 4. 启动视频和评论爬虫
        start_results = {}
        for spider_name in ("bilibili_video", "bilibili_comment"):
            result = self.start_spider(spider_name)
            start_results[spider_name] = {
                "started": result.get("success", False),
                "message": result.get("message", ""),
            }

        return {
            "success": True,
            "message": f"已刷新 {bvid}：去重记录清除了 {removed_count} 条，种子已注入{comment_msg}",
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
                videos.append({
                    "bvid": bvid,
                    "title": info.get("title", "N/A"),
                    "owner_name": info.get("owner_name", info.get("owner", {}).get("name", "N/A")),
                    "view_count": info.get("view_count", info.get("stat", {}).get("view", 0)),
                    "reply_count": info.get("reply_count", info.get("stat", {}).get("reply", 0)),
                    "danmaku_count": info.get("danmaku_count", info.get("stat", {}).get("danmaku", 0)),
                    "pubdate": info.get("pubdate", info.get("pub_date", 0)),
                    "pic": info.get("pic", ""),
                    "has_report": has_report,
                    "comment_count": comment_count,
                })
            except Exception as e:
                print(f"Error parsing {json_file}: {e}")
                continue

    # ---- 第二轮: 补充"有评论但无视频元数据"的视频 ----
    comment_path = Path(DATA_DIR) / "comments"
    if comment_path.exists():
        for cf in sorted(comment_path.glob("*_comments.json"), reverse=True):
            # 从文件名提取 bvid: "BVxxx_comments.json" -> "BVxxx"
            bvid = cf.stem.replace("_comments", "")
            if not bvid or bvid in seen_bvids:
                continue  # 已有视频元数据，跳过
            seen_bvids.add(bvid)
            comment_count = _load_comment_count(cf, bvid)
            report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
            videos.append({
                "bvid": bvid,
                "title": f"[仅有评论数据] {bvid}",
                "owner_name": "N/A",
                "view_count": 0,
                "reply_count": 0,
                "danmaku_count": 0,
                "pubdate": 0,
                "pic": "",
                "has_report": report_path.exists(),
                "comment_count": comment_count,
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
            "owner_mid": first_comment.get("mid", 0) if isinstance(first_comment, dict) else 0,
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

    # 构建 mid → risk_level 快速查找表
    risk_map = {}
    if report and "top_suspects" in report:
        for u in report["top_suspects"]:
            mid = u.get("mid", 0)
            if mid:
                risk_map[mid] = {
                    "score": u.get("score", 0),
                    "level": u.get("risk_level", "low"),
                }

    return render_template(
        "video_detail.html",
        bvid=bvid,
        video=video_info,
        report=report,
        comments=comments,
        comment_count=comment_count,
        risk_map=risk_map,
    )


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
    videos = _list_video_dirs()
    return jsonify({"success": True, "data": videos, "total": len(videos)})


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


@app.route("/api/score-distribution/<bvid>")
def api_score_distribution(bvid: str):
    report = _load_report(bvid)
    if not report:
        return jsonify({"success": False, "message": "报告不存在"}), 404
    stats = report.get("statistics", {})
    score_buckets = {"0-10": 0, "10-20": 0, "20-30": 0, "30-40": 0,
                     "40-50": 0, "50-60": 0, "60-70": 0, "70-80": 0,
                     "80-90": 0, "90-100": 0}
    scored = stats.get("score_distribution", [])
    if scored:
        for item in scored:
            score = item.get("score", 0)
            for bucket in score_buckets:
                low, high = map(int, bucket.split("-"))
                if low <= score < high + 1:
                    score_buckets[bucket] += 1
                    break
    return jsonify({
        "success": True,
        "data": {
            "buckets": score_buckets,
            "high_risk": stats.get("high_risk_count", 0),
            "medium_risk": stats.get("medium_risk_count", 0),
            "low_risk": stats.get("low_risk_count", 0),
            "total": stats.get("total_users", 0),
            "avg_score": stats.get("avg_score", 0),
        }
    })


@app.route("/api/run-analysis/<bvid>", methods=["POST"])
def api_run_analysis(bvid: str):
    result = analysis_mgr.start_analysis(bvid)
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 409


@app.route("/api/analysis-status/<bvid>")
def api_analysis_status(bvid: str):
    status = analysis_mgr.get_status(bvid)
    return jsonify({"success": True, "data": status})


@app.route("/api/video/<bvid>/deep-analyze", methods=["POST"])
def api_video_deep_analyze(bvid: str):
    """手动触发深度分析 (AICU)，可自定义分数阈值。

    请求体: {"threshold": 30}  (可选, 默认30, 范围20-70)
    返回: {"success": true, "deep_stats": {...}, "threshold": 30}
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
        threshold = max(10, min(70, threshold))  # 限制 10-70
    except (TypeError, ValueError):
        threshold = 30

    # ---- 3. 构建 deep_analyze 所需的 scored_users ----
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

    # ---- 4. 创建 LLM 分析器 ----
    from analyzer.llm_analyzer import create_llm_analyzer
    analyzer = create_llm_analyzer()
    if not analyzer:
        return jsonify({"success": False, "error": "LLM 分析器不可用，请检查 API Key 配置"}), 503

    # ---- 5. 记录分析前状态 ----
    before_deep_count = sum(1 for u in top_suspects if u.get("deep_analyzed"))
    before_candidate_count = sum(
        1 for u in scored_users if u["suspicious_score"] >= threshold
    )
    if before_candidate_count == 0:
        return jsonify({
            "success": False,
            "error": f"阈值 {threshold} 下无候选用户（最高评分 {max((u['suspicious_score'] for u in scored_users), default=0)}）",
        }), 400

    # ---- 6. 执行深度分析 ----
    try:
        result = analyzer.deep_analyze(
            scored_users, comments, video_info,
            threshold_override=threshold,
        )
    except Exception as e:
        logger.exception(f"[Deep] 深度分析异常: {e}")
        return jsonify({"success": False, "error": f"深度分析异常: {str(e)}"}), 500

    # ---- 7. 合并结果回报告 ----
    enhanced_users = result.get("enhanced_users", [])
    enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}

    for u in report["top_suspects"]:
        mid = u.get("mid", 0)
        enhanced = enhanced_by_mid.get(mid)
        if enhanced:
            # 优先用深度分析后的融合分；fallback 用原有的 score/uspicious_score
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

    deep_stats = result.get("stats", {})
    report["deep_stats"] = deep_stats

    # ---- 8. 保存报告 ----
    report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    after_deep_count = sum(1 for u in report["top_suspects"] if u.get("deep_analyzed"))
    newly_analyzed = after_deep_count - before_deep_count

    return jsonify({
        "success": True,
        "deep_stats": deep_stats,
        "threshold": threshold,
        "newly_analyzed": newly_analyzed,
        "total_candidates": before_candidate_count,
        "deep_confirmed": deep_stats.get("deep_confirmed", 0),
        "aicu_success": deep_stats.get("aicu_success", 0),
        "aicu_failed": deep_stats.get("aicu_failed", 0),
        "deep_summary": result.get("deep_summary", ""),
    })


@app.route("/api/video/<bvid>/user/<int:mid>/llm-analyze", methods=["POST"])
def api_user_llm_analyze(bvid: str, mid: int):
    """对单个用户执行 LLM 语义分析，结果写回报告。"""
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

    # 获取该用户在此视频下的评论（供 LLM 上下文）
    user_comments = [c for c in comments if str(c.get("mid")) == str(mid)]
    if not user_comments:
        return jsonify({"success": False, "error": "该用户在此视频下无评论"}), 400

    # 构造单用户 scored_users 列表
    single_user = {
        "mid": mid,
        "uname": user.get("uname", ""),
        "suspicious_score": user.get("score", 0),
        "engine_score_raw": user.get("engine_score_raw", user.get("score", 0)),
        "llm_confidence": user.get("llm_confidence", 0),
        "llm_type_id": user.get("llm_type_id", 0),
        "llm_type_name": user.get("llm_type_name", ""),
        "comment_count": user.get("comment_count", len(user_comments)),
        "sample_comments": user_comments[:5],
    }

    # ---- 3. 构建 deep_analyze 所需的 scored_users ----
    # 注意：deep_analyze 期望完整的 scored_users 列表用于排序/统计，
    # 但只会对 candidates（>= threshold）做深度分析。
    # 单用户模式：构造一个元素的列表，并确保其 suspicious_score 足够高以通过阈值
    scored_users = []
    single_user_for_deep = dict(single_user)
    # 确保能通过 deep_analyze 内部的阈值筛选（threshold_override=0 时 all pass）
    scored_users.append(single_user_for_deep)

    try:
        from analyzer.llm_analyzer import create_llm_analyzer
        analyzer = create_llm_analyzer()
        if not analyzer or not analyzer.is_available:
            return jsonify({"success": False, "error": "LLM 不可用，请检查 API Key 配置"}), 503

        # 详细日志
        logger.info(f"[AICU单用户] 开始: mid={mid}, uname={user.get('uname','?')}")
        logger.info(f"[AICU单用户] 传入 scored_users[0]: score={single_user_for_deep.get('suspicious_score',0)}, engine_raw={single_user_for_deep.get('engine_score_raw',0)}")

        result = analyzer.deep_analyze(scored_users, comments_data=comments, video_info=video_info, threshold_override=0)
        
        # 详细日志：打印 deep_analyze 返回内容
        logger.info(f"[AICU单用户] deep_analyze 返回: keys={list(result.keys())}")
        logger.info(f"[AICU单用户] enhanced_users 数量: {len(result.get('enhanced_users', []))}")
        logger.info(f"[AICU单用户] stats: {result.get('stats', {})}")

        enhanced_users = result.get("enhanced_users", [])
        if not enhanced_users:
            logger.warning(f"[AICU单用户] enhanced_users 为空！mid={mid}")
            return jsonify({"success": False, "error": "LLM 深度分析返回空结果，请检查 LLM API 是否正常"}), 500

        enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}
        logger.info(f"[AICU单用户] enhanced_by_mid keys: {list(enhanced_by_mid.keys())}")
        
        enhanced = enhanced_by_mid.get(mid)
        if not enhanced:
            logger.warning(f"[AICU单用户] mid={mid} 不在 enhanced_by_mid 中！available keys: {list(enhanced_by_mid.keys())}")
            # 退回使用第一个结果
            if enhanced_users:
                enhanced = enhanced_users[0]
            else:
                return jsonify({"success": False, "error": "深度分析结果中未找到该用户"}), 500

        # 写回报告 — 注意：单用户路径直接走 deep_analyze，llm_* 字段需从 deep_* 同步
        deep_conf = enhanced.get("deep_confidence", 0)
        deep_type_id = enhanced.get("deep_type_id", 0)
        deep_type_name = enhanced.get("deep_type_name", "") or ("正常用户" if deep_type_id == 0 else "")

        user["llm_analyzed"] = True  # 显式标记分析已完成
        user["llm_confidence"] = deep_conf
        user["llm_type_id"] = deep_type_id
        user["llm_type_name"] = deep_type_name
        user["score"] = enhanced.get("suspicious_score", user.get("score", 0))
        # 同时写 deep_* 字段
        user["deep_analyzed"] = enhanced.get("deep_analyzed", True)
        user["deep_type_id"] = deep_type_id
        user["deep_type_name"] = deep_type_name
        user["deep_confidence"] = deep_conf
        user["deep_reasoning"] = enhanced.get("deep_reasoning", "")
        user["aicu_device"] = enhanced.get("aicu_device", "")
        user["aicu_names"] = enhanced.get("aicu_names", [])
        user["aicu_comment_count"] = enhanced.get("aicu_comment_count", 0)

        # 保存报告
        report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return jsonify({
            "success": True,
            "mid": mid,
            "llm_analyzed": True,
            "llm_type_name": deep_type_name,
            "llm_confidence": deep_conf,
            "deep_type_name": deep_type_name,
            "deep_confidence": deep_conf,
            "score": enhanced.get("suspicious_score", 0),
            "aicu_device": enhanced.get("aicu_device", ""),
            "aicu_comment_count": enhanced.get("aicu_comment_count", 0),
        })
    except Exception as e:
        logger.error(f"单用户 LLM 分析失败 (mid={mid}): {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/video/<bvid>/user/<int:mid>/deep-analyze", methods=["POST"])
def api_user_deep_analyze(bvid: str, mid: int):
    """对单个用户执行 AICU 深度分析（爬取历史评论/空间/标记），结果写回报告。"""
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

    # 构造单用户 scored_users 列表
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

    try:
        from analyzer.llm_analyzer import create_llm_analyzer
        analyzer = create_llm_analyzer()
        if not analyzer:
            return jsonify({"success": False, "error": "LLM 分析器不可用，请检查 API Key 配置"}), 503

        # 注意：aicu.cc API 是公开的，不需要 Cookie
        result = analyzer.deep_analyze([single_user], comments_data=comments, video_info=video_info, threshold_override=0)
        # Bug 修复：deep_analyze 返回 {"enhanced_users": [...]}, 不是 "enhanced_by_mid"
        enhanced_users = result.get("enhanced_users", [])
        enhanced_by_mid = {u["mid"]: u for u in enhanced_users if u.get("mid")}
        enhanced = enhanced_by_mid.get(mid, single_user)

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
            # 同步 llm_* 字段（前台主展示用这些字段）
            user["llm_confidence"] = deep_conf
            user["llm_type_id"] = deep_type_id
            user["llm_type_name"] = deep_type_name
            fused_score = enhanced.get("suspicious_score", user.get("score", 0))
            user["score"] = fused_score

        # 保存报告
        report_path = Path(DATA_DIR) / "reports" / f"{bvid}_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return jsonify({
            "success": True,
            "mid": mid,
            "deep_analyzed": enhanced.get("deep_analyzed", False),
            "deep_type_name": enhanced.get("deep_type_name", "") or ("正常用户" if enhanced.get("deep_type_id", 0) == 0 else ""),
            "deep_confidence": enhanced.get("deep_confidence", 0),
            "llm_type_name": enhanced.get("deep_type_name", "") or ("正常用户" if enhanced.get("deep_type_id", 0) == 0 else ""),
            "llm_confidence": enhanced.get("deep_confidence", 0),
            "score": enhanced.get("suspicious_score", user.get("score", 0)),
            "aicu_comment_count": enhanced.get("aicu_comment_count", 0),
            "aicu_device": enhanced.get("aicu_device", ""),
            "aicu_waf_blocked": enhanced.get("aicu_waf_blocked", False),
        })
    except Exception as e:
        logger.error(f"单用户 AICU 深度分析失败 (mid={mid}): {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


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
    sample = [c for c in comments if c.get("mid") == mid][:5]
    user["sample_comments"] = sample

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


@app.route("/api/video/<bvid>/refresh", methods=["POST"])
def api_video_refresh(bvid: str):
    """刷新单个视频数据：清除去重 + 注入种子 + 启动爬虫。

    用于视频详情页的「刷新数据」按钮。
    重新抓取当前视频的最新信息和评论。
    """
    return jsonify(spider_mgr.refresh_video(bvid))


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
    """更新运行时配置（内存级，重启后失效）"""
    body = request.get_json(silent=True) or {}
    key = body.get("key", "")
    value = body.get("value")
    if not key:
        return jsonify({"success": False, "message": "缺少配置键"}), 400
    try:
        import config.base_config
        if hasattr(config.base_config, key):
            old = getattr(config.base_config, key)
            setattr(config.base_config, key, value)
            return jsonify({
                "success": True,
                "message": f"{key}: {old} → {value} (运行时，重启后恢复默认)",
            })
        return jsonify({"success": False, "message": f"未知配置键: {key}"}), 400
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
    return jsonify({"success": True, "data": spider_mgr.get_status()})


VALID_SPIDERS = ("bilibili_video", "bilibili_comment", "bilibili_user", "bilibili_danmaku")


@app.route("/api/crawler/start/<spider_name>", methods=["POST"])
def api_crawler_start(spider_name: str):
    if spider_name not in VALID_SPIDERS:
        return jsonify({"success": False, "message": f"未知爬虫: {spider_name}"}), 400
    return jsonify(spider_mgr.start_spider(spider_name))


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


@app.route("/api/crawler/start-both", methods=["POST"])
def api_crawler_start_both():
    r1 = spider_mgr.start_spider("bilibili_video")
    time.sleep(0.5)
    r2 = spider_mgr.start_spider("bilibili_comment")
    return jsonify({"success": r1["success"] or r2["success"], "video": r1, "comment": r2})


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
    if log_info["total_lines"] == 0 and not os.path.exists(CRAWLER_LOG_PATH):
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
    valid_spiders = {"bilibili_video", "bilibili_comment", "bilibili_user", "bilibili_danmaku"}
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
        resp = req.get(
            BilibiliLogin.QRCODE_POLL_API,
            params={"qrcode_key": qrcode_key},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"},
            timeout=10,
        )
        poll_data = resp.json()
        if poll_data.get("code") == 0:
            cookies = login._extract_cookies_from_response(resp)
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
        elif poll_data.get("code") == 86090:
            return jsonify({"success": True, "data": {"status": "scanned", "message": "已扫码，请确认"}})
        elif poll_data.get("code") == 86038:
            return jsonify({"success": True, "data": {"status": "expired", "message": "二维码已过期"}})
        else:
            return jsonify({
                "success": True,
                "data": {"status": "unknown", "code": poll_data.get("code"), "message": poll_data.get("message", "")}
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
    app.run(host="0.0.0.0", port=5001, debug=debug_mode, use_reloader=debug_mode)
