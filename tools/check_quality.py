#!/usr/bin/env python
"""代码质量检查工具 — 提交前必跑。

检查项:
  1. 重复方法定义 (两个同名 def)
  2. 硬编码零值 (follower=0 / following=0 等数据透传路径)
  3. 无防循环保护的递归模式 (_fetch_next -> _request -> _fetch_next)
  4. 缩进错误 (模块级 def 打断 class 定义)

用法:
  python tools/check_quality.py
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

issues = []


def check_duplicate_methods(spider_path: Path):
    """检查重复的 def 方法名。"""
    with open(spider_path, encoding="utf-8") as f:
        lines = f.readlines()

    defs = {}
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\s*def\s+([a-zA-Z_]\w*)\s*\(.*", line)
        if m:
            name = m.group(1)
            if name in defs:
                issues.append(
                    f"{spider_path.name}:{i} DUPLICATE def {name}() — "
                    f"first at line {defs[name]} (第二个覆盖第一个！)"
                )
            defs[name] = i


def check_hardcoded_zeros(spider_path: Path):
    """检查数据构造器中的硬编码零值。"""
    with open(spider_path, encoding="utf-8") as f:
        content = f.read()

    suspects = ["follower=0", "following=0", "video_count=0,", "upload_count=0,"]
    for s in suspects:
        if s in content:
            # 排除已在注释中的
            in_code = False
            for lineno, line in enumerate(content.split("\n"), 1):
                if s in line and not line.strip().startswith("#") and "logger." not in line and "=0,  # 安全" not in line:
                    issues.append(
                        f"{spider_path.name}:{lineno} HARDCODED {s} — "
                        f"数据透传路径不应硬编码零值"
                    )


def check_recursion(spider_path: Path):
    """检查无保护的递归调用。"""
    with open(spider_path, encoding="utf-8") as f:
        content = f.read()

    if "_fetch_next_user()" in content and "loop_guard" not in content:
        lineno = next(
            (i for i, l in enumerate(content.split("\n"), 1)
             if "_fetch_next_user()" in l and "self" in l), 0
        )
        if lineno:
            issues.append(
                f"{spider_path.name}:{lineno} RECURSION RISK — "
                f"_fetch_next_user 可能递归调用自身（需加 while 循环或 loop_guard）"
            )


def check_indentation(spider_path: Path):
    """检查模块级 def 是否打断了 class 定义。"""
    with open(spider_path, encoding="utf-8") as f:
        lines = f.readlines()

    in_class = False
    for i, line in enumerate(lines, 1):
        # Detect class start
        if re.match(r"^class\s+", line):
            in_class = True
            continue
        # A def at column 0 after a class started = module-level break
        if in_class and re.match(r"^def\s+", line):
            # Check if there's a blank line + no class body continuation
            issues.append(
                f"{spider_path.name}:{i} INDENTATION — "
                f"def at column 0 after class starts, breaks class definition!"
            )
            in_class = False


def main():
    spiders_dir = ROOT / "bilibili_crawler" / "spiders"
    for py_file in sorted(spiders_dir.glob("*.py")):
        print(f"Checking {py_file.name:40s} ... ", end="")
        before = len(issues)
        check_duplicate_methods(py_file)
        check_hardcoded_zeros(py_file)
        check_recursion(py_file)
        check_indentation(py_file)
        after = len(issues)
        if after > before:
            print(f"❌ {after - before} issue(s)")
        else:
            print("✅")

    print(f"\n{'=' * 60}")
    if issues:
        print(f"❌ {len(issues)} issue(s) found:\n")
        for issue in issues:
            print(f"  • {issue}")
        print(f"\n请修复以上问题后再提交。")
        sys.exit(1)
    else:
        print("✅ All checks passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
