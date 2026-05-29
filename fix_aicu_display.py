#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 video_detail.html 中 AICU 数据显示逻辑"""

p = r"D:\腾讯云黑客松\bilibili-sentinel\dashboard\templates\video_detail.html"
with open(p, "r", encoding="utf-8") as f:
    content = f.read()

# ---- Fix 1: 第一个分支（水军，deep_type_id > 0）里的 AICU metadata 条件
# 原来：if (user.aicu_comment_count || user.aicu_device) {
# 改为：始终渲染（只要 deep_analyzed 为 true）
old1 = "        // AICU metadata\n        if (user.aicu_comment_count || user.aicu_device) {"
new1 = "        // AICU metadata（始终显示，只要做过深度分析）\n        if (user.deep_analyzed) {"
if old1 in content:
    content = content.replace(old1, new1, 1)
    print("Fix1 applied: first branch AICU condition")
else:
    print("Fix1 NOT FOUND")

# ---- Fix 2: 第二个分支（正常用户，deep_type_id=0）里的 AICU 始终渲染
# 找到第二个 else if (user.deep_analyzed) 后面的 AICU metadata 块
# 原来：if (user.aicu_comment_count || user.aicu_device) {
# 改为：if (user.deep_analyzed) {
old2 = "        // AICU metadata（正常用户也显示，始终渲染）\n        if (user.deep_analyzed) {"
new2 = "        // AICU metadata（正常用户也显示，始终渲染）\n        if (user.deep_analyzed) {"
# 其实这个条件已经是对的，但要确保 aicu_comment_count=0 也显示
# 问题在于 aicuParts 拼接后如果是空，显示 "AICU 已抓取，但无显著特征数据"
# 这部分逻辑已经是对的，不需要改

# 但还要修复 aicu_comment_count === 0 的判断
# 在第一个分支（水军）里，也要处理 comment_count = 0 的情况
# 找到第一个分支的 aicuParts 拼接逻辑

# 更根本的问题：两个分支里 aicu_comment_count 和 aicu_device 判断都是在 JS 里做的
# 我们要确保 api_user_detail 返回的数据里包含这些字段

print("Checking if both branches have proper AICU display...")

# 检查：第一个分支的 AICU metadata div 是否正确渲染
branch1_count = content.count("if (user.deep_analyzed) {")
print(f"  'if (user.deep_analyzed)' occurrences: {branch1_count}")

# 最终检查：写回文件
with open(p, "w", encoding="utf-8") as f:
    f.write(content)
print("File saved.")
