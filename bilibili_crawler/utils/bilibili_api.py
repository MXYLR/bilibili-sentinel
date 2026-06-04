"""
B站 API 工具函数

包含:
- WBI 签名算法 (search API 必需)
- API URL 构造
- 响应解析与错误处理
- WBI 密钥动态获取与缓存
"""

import hashlib
import json
import logging
import random
import time
import urllib.parse
from functools import reduce

from config import BILIBILI_API_BASE, MIXIN_KEY_ENC_TAB

logger = logging.getLogger(__name__)

# ---- WBI 密钥缓存 ----
# 用回退密钥初始化，确保 _wbi_keys_cache 永不为 None。
# 这样 Scrapy spider 回调中的任何 enc_wbi() / get_popular_url() 调用
# 都不会触发同步 HTTP 请求来阻塞 Twisted 事件循环。
_FALLBACK_IMG_KEY = "653657f524a547ac981ded72ea172057"
_FALLBACK_SUB_KEY = "6e4909c971f14ec8889c72f7e4c3a0f2"
_wbi_keys_cache = (_FALLBACK_IMG_KEY, _FALLBACK_SUB_KEY)
_wbi_keys_fetched_at = time.time()
_WBI_CACHE_TTL = 3600  # 1小时缓存


def _fetch_wbi_keys() -> tuple:
    """
    获取 WBI 签名密钥。

    如果缓存中已是回退密钥且已过期，尝试通过 HTTP 获取最新密钥。
    在 Twisted 事件循环外（如独立脚本）可正常执行；
    在 Scrapy 回调中调用时直接返回缓存值，不会阻塞。

    如需在 Scrapy 运行时异步刷新密钥，请使用
    BilibiliWbiRefreshMiddleware._async_refresh_keys() 方法。

    Returns:
        (img_key, sub_key) 元组
    """
    global _wbi_keys_cache, _wbi_keys_fetched_at

    # 缓存命中 (无论是回退密钥还是从 API 获取的)
    if (time.time() - _wbi_keys_fetched_at) < _WBI_CACHE_TTL:
        return _wbi_keys_cache

    # 缓存过期 → 尝试在线刷新
    url = f"{BILIBILI_API_BASE}/x/web-interface/nav"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com",
    }
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
        sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""

        if img_key and sub_key:
            _wbi_keys_cache = (img_key, sub_key)
            _wbi_keys_fetched_at = time.time()
            logger.info(f"WBI keys refreshed: img={img_key[:8]}..., sub={sub_key[:8]}...")
            return (img_key, sub_key)
        else:
            logger.warning(f"WBI keys not found in nav response: img_url={img_url}, sub_url={sub_url}")
    except Exception as e:
        logger.warning(f"Failed to refresh WBI keys: {e}")

    # 刷新失败 → 延长回退密钥 TTL 以避免重复尝试
    _wbi_keys_fetched_at = time.time()
    logger.debug("WBI key refresh failed, extending fallback cache TTL")
    return _wbi_keys_cache


def get_mixin_key(orig_key: str) -> str:
    """对 img_key 或 sub_key 进行混肴，生成 32 位签名密钥"""
    return reduce(lambda s, i: s + orig_key[i], MIXIN_KEY_ENC_TAB, "")[:32]


def enc_wbi(params: dict, img_key: str = None, sub_key: str = None) -> dict:
    """
    WBI 签名算法。

    步骤:
      1. 获取 img_key + sub_key → 混肴 → mixin_key (32位)
      2. 添加 wts (unix 时间戳)
      3. 按 key 字母序排列所有参数
      4. w_rid = md5(排序后参数字符串 + mixin_key)
      5. 返回含 w_rid + wts 的参数 dict

    Args:
        params: 原始请求参数
        img_key, sub_key: 可手动传入, 不传则自动获取

    Returns:
        添加了 w_rid, wts 的参数 dict
    """
    if img_key is None or sub_key is None:
        img_key, sub_key = _fetch_wbi_keys()

    mixin_key = get_mixin_key(img_key + sub_key)

    # 添加时间戳
    params["wts"] = int(time.time())

    # 按 key 排序
    sorted_params = sorted(params.items(), key=lambda x: x[0])

    # 拼接 URL 参数串
    query_string = urllib.parse.urlencode(sorted_params)

    # MD5 签名
    w_rid = hashlib.md5((query_string + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid

    return params


def build_api_url(endpoint: str, params: dict = None, use_wbi: bool = False) -> str:
    """
    构造完整的 B站 API URL。

    Args:
        endpoint: 如 '/x/web-interface/view'
        params: 查询参数字典
        use_wbi: 是否添加 WBI 签名

    Returns:
        完整 URL 字符串
    """
    if params is None:
        params = {}

    if use_wbi:
        params = enc_wbi(params)

    query = urllib.parse.urlencode(params)
    return f"{BILIBILI_API_BASE}{endpoint}?{query}"


def parse_bilibili_response(response_data: dict) -> dict:
    """
    解析 B站 API 响应，统一错误处理。

    成功: code == 0 → 返回 data
    常见错误码:
      -101: 未登录
      -404: 不存在
      -412: 被风控拦截
      400: 请求错误

    Args:
        response_data: API 返回的 JSON dict (含 code, message, data)

    Returns:
        data dict 或 None (失败时)
    """
    code = response_data.get("code", -1)
    message = response_data.get("message", "")

    if code == 0:
        return response_data.get("data", {})

    # 特殊错误处理
    if code == -412:
        logger.warning(f"[B站API] 被风控拦截 (412): {message}")
    elif code == -101:
        logger.warning(f"[B站API] 需要登录 (-101): {message}")
    elif code == -404:
        logger.warning(f"[B站API] 资源不存在 (-404): {message}")
    elif code == -352:
        logger.warning(f"[B站API] 请求校验失败 (-352): {message} — Cookie缺失或WBI签名无效")
    else:
        logger.warning(f"[B站API] 错误 code={code}: {message}")

    return None


def get_video_info(bvid: str = None, aid: int = None) -> dict:
    """
    获取B站视频详情 (需要 WBI 签名)。
    GET /x/web-interface/view?bvid={bvid} 或 ?aid={aid}
    """
    params = {}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["aid"] = aid
    else:
        raise ValueError("必须提供 bvid 或 aid")

    url = build_api_url("/x/web-interface/view", params, use_wbi=True)
    return url


def get_comments_url(oid: int, page: int = 1, sort: int = 0,
                     next_cursor: int = None, page_size: int = None) -> str:
    """
    构造评论 API URL。

    Args:
        oid: 视频 aid (数字ID)
        page: 页码 (从1开始)
        sort: 0=按时间, 1=默认, 2=按热度
        next_cursor: cursor 翻页值 (从 API 响应 cursor.next 获取)。
                     当提供时，同时携带 pn + next 双参数以确保兼容性。
        page_size: 每页条数 (默认随机 15-25，避免固定值被 B站 WAF 检测)

    注意: sort=2(热度)在 mode=3 下每页仅返回少量高赞评论，
    不利于采集全量数据。默认使用 sort=0(时间排序)可获取全部评论。
    """
    # v2.4: 随机 page_size (15-25)，避免固定 20 被 WAF 模式识别
    if page_size is None:
        page_size = random.randint(15, 25)
    # mode: 3=按时间排序（全量评论）; 2=按热度
    # sort 参数映射: 0=时间, 2=热度
    mode = 3 if sort == 0 else 2
    params = {
        "type": 1,
        "oid": oid,
        "mode": mode,
        "ps": page_size,
        "sort": sort,
    }
    # 翻页策略: 始终携带 pn 作为兜底，如果提供了 cursor 则额外加入 next
    # 实测验证: pn + next 同时存在时 B站 API 正常返回 (code=0)
    params["pn"] = page
    if next_cursor is not None:
        params["next"] = next_cursor
    # v2.12: 主评论 API WBI 签名 + 不带 Cookie（避免账号维度风控）
    # 代价: 拿不到 IP 属地信息
    # Cookie 策略由 BilibiliCookieMiddleware 根据 URL 路径自动判断
    return build_api_url("/x/v2/reply/main", params, use_wbi=True)


def get_sub_replies_url(oid: int, root_rpid: int, page: int = 1,
                        page_size: int = None) -> str:
    """
    构造子评论 (楼中楼) API URL。

    v2.12 更新:
    - 楼中楼 API (/x/v2/reply/reply) 不需要 WBI 签名（主评论独立的风控维度）
    - 建议带登录 Cookie 以获取 IP 属地信息（Cookie 由中间件按路径注入）
    - 主评论和楼中楼的 IP 风控不共享，可以同 IP 并行请求

    Args:
        oid: 视频 aid
        root_rpid: 根评论 rpid
        page: 页码
        page_size: 每页条数 (默认随机 15-25)
    """
    if page_size is None:
        page_size = random.randint(15, 25)
    params = {
        "type": 1,
        "oid": oid,
        "root": root_rpid,
        "pn": page,
        "ps": page_size,
    }
    return build_api_url("/x/v2/reply/reply", params, use_wbi=False)


def get_popular_url(page: int = 1, page_size: int = 50) -> str:
    """
    获取热门排行榜 URL (需要 WBI 签名)。
    GET /x/web-interface/popular?ps=50&pn={page}
    """
    params = {"ps": page_size, "pn": page}
    return build_api_url("/x/web-interface/popular", params, use_wbi=True)


def get_search_url(keyword: str, page: int = 1) -> str:
    """
    获取搜索 API URL (需要 WBI 签名)。
    GET /x/web-interface/wbi/search/type?keyword={kw}&search_type=video&page={page}
    """
    params = {
        "keyword": keyword,
        "search_type": "video",
        "page": page,
        "page_size": 42,
    }
    return build_api_url("/x/web-interface/wbi/search/type", params, use_wbi=True)


def get_user_info_url(mid: int) -> str:
    """
    获取 B站用户空间信息 URL。
    GET /x/space/wbi/acc/info?mid={mid}
    """
    params = {"mid": mid}
    return build_api_url("/x/space/wbi/acc/info", params, use_wbi=True)


def get_user_videos_url(mid: int, page: int = 1, ps: int = 50) -> str:
    """
    获取 B站用户投稿视频列表 URL。
    GET /x/space/wbi/arc/search?mid={mid}&ps={ps}&pn={pn}

    用于提取 video_count (投稿数)，辅助 F12/F13 判断。
    """
    params = {"mid": mid, "ps": ps, "pn": page, "order": "pubdate"}
    return build_api_url("/x/space/wbi/arc/search", params, use_wbi=True)


def get_user_posts_url(mid: int, offset: str = "") -> str:
    """
    获取 B站用户空间动态列表 URL (新版 polymer API)。
    GET /x/polymer/web-dynamic/v1/feed/space?host_mid={mid}&offset={offset}

    返回用户最近发布的动态（文本/转发/图片/视频），
    用于 F13(转发抽奖) 和 F14(敏感内容) 检测。

    注意: 此 API 不需要 WBI 签名，但需要有效的登录 Cookie。
    """
    params = {
        "host_mid": mid,
        "timezone_offset": -480,
        "platform": "web",
    }
    if offset:
        params["offset"] = offset
    return build_api_url("/x/polymer/web-dynamic/v1/feed/space", params, use_wbi=True)


def get_danmaku_url(cid: int, segment_index: int = 1) -> str:
    """
    获取 B站弹幕 API URL (新分段 API, protobuf 格式)。

    GET /x/v2/dm/web/seg.so?oid={cid}&segment_index={n}&type=1

    每段覆盖约 6 分钟的视频时长。需要 protobuf 解析。

    降级方案: 使用 /x/v1/dm/list.so?oid={cid} (XML 格式, 全量但上限~8000条)
    """
    params = {"oid": cid, "segment_index": segment_index, "type": 1}
    return build_api_url("/x/v2/dm/web/seg.so", params, use_wbi=False)


def get_danmaku_xml_url(cid: int) -> str:
    """
    获取 B站弹幕 XML API URL (旧版全量接口)。

    GET /x/v1/dm/list.so?oid={cid}

    返回 XML 格式弹幕，上限约 8000 条。不需要 WBI 签名。
    适用场景: 弹幕量较少的视频，或作为 protobuf API 的降级方案。
    """
    return build_api_url("/x/v1/dm/list.so", {"oid": cid}, use_wbi=False)


def prewarm_wbi_cache():
    """
    预加载 WBI 密钥缓存。

    注意: 从 v2.1 起，_wbi_keys_cache 已在模块加载时用回退密钥初始化，
    此函数仅尝试在线刷新为最新密钥（如果网络允许）。
    它不再是必需的启动步骤，但保留以支持独立脚本和测试。
    """
    global _wbi_keys_cache, _wbi_keys_fetched_at

    # 强制刷新（忽略当前缓存 TTL）
    _wbi_keys_fetched_at = 0
    img_key, sub_key = _fetch_wbi_keys()
    logger.info(f"WBI cache pre-warmed: img_key={img_key[:8]}...")


if __name__ == "__main__":
    # 测试 WBI 签名
    print("=== WBI签名测试 ===")
    print(f"缓存状态: _wbi_keys_cache={'SET' if _wbi_keys_cache else 'NONE'}")

    test_params = enc_wbi({"keyword": "测试", "search_type": "video", "page": 1})
    print(f"签名后参数: {json.dumps(test_params, indent=2)}")
    assert "w_rid" in test_params
    assert "wts" in test_params
    print("WBI签名 OK")

    # 测试 URL构造
    print("\n=== URL构造测试 ===")
    print(f"热门榜: {get_popular_url(1)}")
    print(f"视频详情: {get_video_info(bvid='BV1xx411c7mD')}")
    print(f"评论: {get_comments_url(170001, 1)}")
    print(f"搜索: {get_search_url('华为', 1)}")
    print("URL构造 OK")
