"""
诊断 Page 2+ -352 错误的根因。

对比 Python requests 库 vs 模拟 Scrapy 请求，找出差异。

测试步骤:
1. 用 Python requests + WBI签名 + Cookie → 验证 page 1/2 是否正常
2. 记录 Scrapy 日志中 page 1 vs page 2 请求的实际 HTTP 细节
3. 对比 TLS 指纹、Cookie header 格式、User-Agent 差异
"""

import json
import os
import sys
import time
import hashlib
import urllib.parse
from functools import reduce

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from config import MIXIN_KEY_ENC_TAB, BILIBILI_API_BASE

# =============================================================
# STEP 1: 加载 Cookie
# =============================================================
COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "cookies.json")

with open(COOKIE_FILE, "r", encoding="utf-8") as f:
    cookies = json.load(f)

print(f"[DIAG] 加载 Cookie: {len(cookies)} 键")
print(f"  SESSDATA={'***' if 'SESSDATA' in cookies else 'MISSING'}")
print(f"  bili_jct={'***' if 'bili_jct' in cookies else 'MISSING'}")

# =============================================================
# STEP 2: 获取最新 WBI 密钥
# =============================================================
def fetch_wbi_keys():
    """从 nav API 获取最新 WBI 密钥"""
    url = f"{BILIBILI_API_BASE}/x/web-interface/nav"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com",
    }
    resp = requests.get(url, headers=headers, cookies=cookies, timeout=10)
    data = resp.json()
    wbi_img = data.get("data", {}).get("wbi_img", {})
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")
    img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
    sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""
    return img_key, sub_key

img_key, sub_key = fetch_wbi_keys()
print(f"[DIAG] WBI keys: img={img_key[:8]}..., sub={sub_key[:8]}...")

# =============================================================
# STEP 3: 准备测试视频
# =============================================================
# 从日志中找一个有足够评论的视频来测试
def get_mixin_key(orig_key: str) -> str:
    return reduce(lambda s, i: s + orig_key[i], MIXIN_KEY_ENC_TAB, "")[:32]

def enc_wbi(params: dict) -> dict:
    mixin_key = get_mixin_key(img_key + sub_key)
    params["wts"] = int(time.time())
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    query_string = urllib.parse.urlencode(sorted_params)
    w_rid = hashlib.md5((query_string + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params

# 使用一个有评论的视频 aid（从日志中获取）
TEST_AID = 116634685545000  # BV1wtGR6dEha 的 aid
TEST_BVID = "BV1wtGR6dEha"

# =============================================================
# STEP 4: Python requests 测试 page 1
# =============================================================
print("\n" + "="*60)
print("[TEST 1] Python requests — Page 1 (pn=1)")
print("="*60)

params_p1 = {
    "type": 1,
    "oid": TEST_AID,
    "mode": 3,
    "ps": 20,
    "sort": 0,
    "pn": 1,
}
signed_p1 = enc_wbi(params_p1.copy())
url_p1 = f"{BILIBILI_API_BASE}/x/v2/reply/main?" + urllib.parse.urlencode(signed_p1)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

print(f"URL: {url_p1[:120]}...")
resp_p1 = requests.get(url_p1, headers=headers, cookies=cookies, timeout=15)
data_p1 = resp_p1.json()
code_p1 = data_p1.get("code", -1)
print(f"HTTP Status: {resp_p1.status_code}")
print(f"API Code: {code_p1}")
print(f"Message: {data_p1.get('message', '')}")

if code_p1 == 0:
    replies = data_p1.get("data", {}).get("replies", [])
    cursor = data_p1.get("data", {}).get("cursor", {})
    is_end = cursor.get("is_end", True)
    next_cursor = cursor.get("next", 0)
    all_count = cursor.get("all_count", 0)
    print(f"  ✓ Page 1 OK: {len(replies)} replies, all_count={all_count}, is_end={is_end}, next_cursor={next_cursor}")
    
    # Save cursor for page 2 test
    _next_cursor = next_cursor
    _is_end = is_end
else:
    print(f"  ✗ Page 1 FAILED: code={code_p1}")
    sys.exit(1)

# =============================================================
# STEP 5: Python requests 测试 page 2 (with cursor)
# =============================================================
print("\n" + "="*60)
print(f"[TEST 2] Python requests — Page 2 (pn=2, next={_next_cursor})")
print("="*60)

if _is_end:
    print("  ⚠ page 1 cursor.is_end=True, skipping page 2 test")
else:
    params_p2 = {
        "type": 1,
        "oid": TEST_AID,
        "mode": 3,
        "ps": 20,
        "sort": 0,
        "pn": 2,
        "next": _next_cursor,
    }
    signed_p2 = enc_wbi(params_p2.copy())
    url_p2 = f"{BILIBILI_API_BASE}/x/v2/reply/main?" + urllib.parse.urlencode(signed_p2)
    
    print(f"URL: {url_p2[:150]}...")
    resp_p2 = requests.get(url_p2, headers=headers, cookies=cookies, timeout=15)
    data_p2 = resp_p2.json()
    code_p2 = data_p2.get("code", -1)
    print(f"HTTP Status: {resp_p2.status_code}")
    print(f"API Code: {code_p2}")
    print(f"Message: {data_p2.get('message', '')}")
    
    if code_p2 == 0:
        replies = data_p2.get("data", {}).get("replies", [])
        print(f"  ✓ Page 2 OK: {len(replies)} replies")
    else:
        print(f"  ✗ Page 2 FAILED: code={code_p2}")

# =============================================================
# STEP 6: 测试不带 next 参数 (只用 pn=2)
# =============================================================
print("\n" + "="*60)
print(f"[TEST 3] Python requests — Page 2 (pn=2 ONLY, no cursor)")
print("="*60)

params_p2_nocursor = {
    "type": 1,
    "oid": TEST_AID,
    "mode": 3,
    "ps": 20,
    "sort": 0,
    "pn": 2,
}
signed_p2nc = enc_wbi(params_p2_nocursor.copy())
url_p2nc = f"{BILIBILI_API_BASE}/x/v2/reply/main?" + urllib.parse.urlencode(signed_p2nc)

print(f"URL: {url_p2nc[:150]}...")
resp_p2nc = requests.get(url_p2nc, headers=headers, cookies=cookies, timeout=15)
data_p2nc = resp_p2nc.json()
code_p2nc = data_p2nc.get("code", -1)
print(f"HTTP Status: {resp_p2nc.status_code}")
print(f"API Code: {code_p2nc}")
print(f"Message: {data_p2nc.get('message', '')}")

if code_p2nc == 0:
    replies = data_p2nc.get("data", {}).get("replies", [])
    print(f"  ✓ Page 2 (pn only) OK: {len(replies)} replies")
else:
    print(f"  ✗ Page 2 (pn only) FAILED: code={code_p2nc}")

# =============================================================
# STEP 7: 测试 Cookie 注入方式差异
# =============================================================
print("\n" + "="*60)
print("[TEST 4] Python requests — Cookie as header string (模拟 Scrapy)")
print("="*60)

if not _is_end:
    # 模拟 Scrapy BilibiliCookieMiddleware 的 Cookie header 注入方式
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers_with_cookie = headers.copy()
    headers_with_cookie["Cookie"] = cookie_str
    
    # 不传 cookies 参数，只用 header
    params_p2h = {
        "type": 1,
        "oid": TEST_AID,
        "mode": 3,
        "ps": 20,
        "sort": 0,
        "pn": 2,
        "next": _next_cursor,
    }
    signed_p2h = enc_wbi(params_p2h.copy())
    url_p2h = f"{BILIBILI_API_BASE}/x/v2/reply/main?" + urllib.parse.urlencode(signed_p2h)
    
    print(f"Cookie header: {cookie_str[:80]}...")
    resp_p2h = requests.get(url_p2h, headers=headers_with_cookie, timeout=15)
    data_p2h = resp_p2h.json()
    code_p2h = data_p2h.get("code", -1)
    print(f"HTTP Status: {resp_p2h.status_code}")
    print(f"API Code: {code_p2h}")
    print(f"Message: {data_p2h.get('message', '')}")
    
    if code_p2h == 0:
        print(f"  ✓ Cookie as header OK")
    else:
        print(f"  ✗ Cookie as header FAILED: code={code_p2h}")

# =============================================================
# STEP 8: 记录 Python requests 的实际 HTTP 请求细节
# =============================================================
print("\n" + "="*60)
print("[TEST 5] 检查 Python requests 实际发送的 headers")
print("="*60)

# 使用 prepared request 查看实际发送内容
from requests import Request, Session

if not _is_end:
    s = Session()
    req = Request('GET', url_p2, headers=headers, cookies=cookies)
    prepped = s.prepare_request(req)
    
    print(f"Method: {prepped.method}")
    print(f"URL: {prepped.url[:120]}...")
    print(f"Headers sent:")
    for k, v in prepped.headers.items():
        if k.lower() == 'cookie':
            print(f"  {k}: {v[:60]}...")
        else:
            print(f"  {k}: {v}")

print("\n" + "="*60)
print("[DIAGNOSTIC COMPLETE]")
print("="*60)
