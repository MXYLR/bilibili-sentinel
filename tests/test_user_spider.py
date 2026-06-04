#!/usr/bin/env python
"""用户爬虫端到端测试 — 快速验证数据采集全链路。

环境要求: Redis (localhost:6379, db=1) 运行中

用法:
  python tests/test_user_spider.py

测试内容:
  1. 种子注入 → Redis 队列可读
  2. 蜘蛛启动 → 消费种子 → 卡住存活
  3. 产出验证 → data/users/{mid}.json 字段完整性
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / "venv" / "Scripts" / "python.exe"

PASS = 0
FAIL = 1
SKIP = 2


def log(level, msg):
    prefix = {"ok": "✅", "fail": "❌", "skip": "⚠️"}.get(level, "•")
    print(f"  {prefix} {msg}")


def check_redis() -> int:
    """检查 Redis 连接。"""
    import redis
    try:
        r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        r.ping()
        return PASS
    except Exception as e:
        log("skip", f"Redis not available: {e}")
        return SKIP


def test_seed_injection() -> int:
    """注入测试种子并验证。"""
    import redis
    r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
    r.flushdb()
    r.rpush("bilibili_crawler:user_seeds", '{"mid": 2}')
    assert r.llen("bilibili_crawler:user_seeds") == 1, "Seed injection failed"
    log("ok", "Seed injected into Redis")
    return PASS


def test_spider_run() -> int:
    """运行蜘蛛并检查产出。"""
    user_file = ROOT / "data" / "users" / "2.json"
    if user_file.exists():
        user_file.unlink()

    # 启动蜘蛛 25 秒超时
    cmd = [
        str(VENV_PYTHON), "-m", "scrapy", "crawl", "bilibili_user",
        "-s", "LOG_LEVEL=WARNING",
        "-s", "CLOSESPIDER_TIMEOUT=25",
    ]
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=30
    )

    if not user_file.exists():
        log("fail", f"No output file: {user_file}")
        log("fail", f"stderr: {proc.stderr[-300:]}")
        return FAIL

    with open(user_file, encoding="utf-8") as f:
        data = json.load(f)

    errors = []
    checks = [
        ("name", data.get("name"), lambda v: bool(v), "name is missing"),
        ("level", data.get("level"), lambda v: v >= 1, "level={v}"),
        ("follower", data.get("follower"), lambda v: isinstance(v, (int, float)) and v > 0, "follower={v}"),
        ("following", data.get("following"), lambda v: isinstance(v, (int, float)), "following={v}"),
    ]

    for field, value, check_fn, err_msg in checks:
        if not check_fn(value):
            errors.append(f"{field}: {err_msg.format(v=value) if '{v}' in err_msg else err_msg}")

    if errors:
        for e in errors:
            log("fail", e)
        return FAIL

    log("ok", f"name={data['name']} Lv{data['level']} follower={data['follower']} following={data['following']}")
    log("ok", f"File size: {user_file.stat().st_size} bytes")
    return PASS


def main():
    print("=" * 60)
    print("  bilibili_user Spider E2E Test")
    print("=" * 60)

    if check_redis() == SKIP:
        log("skip", "Tests skipped (no Redis)")
        sys.exit(0)

    results = []
    results.append(("Seed Injection", test_seed_injection()))
    results.append(("Spider Run + Output", test_spider_run()))

    print(f"\n{'=' * 60}")
    failed = sum(1 for _, r in results if r == FAIL)
    skipped = sum(1 for _, r in results if r == SKIP)
    passed = sum(1 for _, r in results if r == PASS)

    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    for name, r in results:
        status = {PASS: "✅", FAIL: "❌", SKIP: "⚠️"}.get(r, "?")
        print(f"  {status} {name}")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
