"""
B站数据模型定义

Four item types:
  - VideoItem: B站视频基本信息
  - CommentItem: B站评论 (含子评论, 含内嵌用户信息)
  - UserInfoItem: B站用户详细信息 (通过 space API)
  - UserPostItem: B站用户空间动态 (通过 polymer dynamic API)
"""

import scrapy


class VideoItem(scrapy.Item):
    """B站视频基本信息 — 来自 /x/web-interface/view API"""

    # ---- 核心标识 ----
    bvid = scrapy.Field()              # BV号 (如 BV1xx411c7mD)
    aid = scrapy.Field()               # AV号 (数字ID)

    # ---- 视频信息 ----
    title = scrapy.Field()             # 标题
    desc = scrapy.Field()              # 简介
    duration = scrapy.Field()          # 时长 (秒)
    pubdate = scrapy.Field()           # 发布时间戳 (Unix timestamp)
    cid = scrapy.Field()               # 视频 cid (用于弹幕API)

    # ---- 作者信息 ----
    owner_name = scrapy.Field()        # UP主昵称
    owner_mid = scrapy.Field()         # UP主 UID

    # ---- 统计 (data.stat) ----
    view_count = scrapy.Field()        # 播放量
    danmaku_count = scrapy.Field()     # 弹幕数
    reply_count = scrapy.Field()       # 评论数
    favorite_count = scrapy.Field()    # 收藏数
    coin_count = scrapy.Field()        # 硬币数
    share_count = scrapy.Field()       # 分享数
    like_count = scrapy.Field()        # 点赞数

    # ---- 元数据 ----
    tname = scrapy.Field()             # 分区名称 (如 "科技")
    pic = scrapy.Field()               # 封面图 URL
    tags = scrapy.Field()              # 标签列表 (list of dicts with tag_name)
    crawl_time = scrapy.Field()        # 采集时间 (ISO 8601)
    source = scrapy.Field()            # 来源: "hot"=热门, "bvid"=指定BV, "search:{kw}"=搜索, 留空=unknown


class CommentItem(scrapy.Item):
    """B站评论 (含主评论和子评论, 含内嵌用户信息)"""

    # ---- 核心标识 ----
    rpid = scrapy.Field()              # 评论ID (全局唯一)
    oid = scrapy.Field()               # 视频 aid
    type_id = scrapy.Field()           # 评论类型 (1=视频, 12=专栏等)
    bvid = scrapy.Field()              # 所属视频 bvid
    root = scrapy.Field()              # 根评论ID (0=主评论, >0=子评论)
    parent = scrapy.Field()            # 父评论ID

    # ---- 评论内容 ----
    content = scrapy.Field()           # 评论文本
    ctime = scrapy.Field()             # 发布时间戳 (Unix timestamp)
    like_count = scrapy.Field()        # 获赞数
    rcount = scrapy.Field()            # 子评论数

    # ---- 评论者信息 (API 内嵌, 无需额外请求) ----
    mid = scrapy.Field()               # 评论者 UID
    uname = scrapy.Field()             # 评论者昵称
    avatar = scrapy.Field()            # 头像 URL
    level = scrapy.Field()             # 用户等级 (0-6)
    sex = scrapy.Field()               # 性别 ("男"/"女"/"保密")

    # ---- 会员信息 (API 内嵌) ----
    vip_status = scrapy.Field()        # 大会员状态 (0=否, 1=是)
    vip_type = scrapy.Field()          # 大会员类型 (0=非会员, 1=月度, 2=年度)
    is_senior_member = scrapy.Field()  # 是否为年度大会员

    # ---- 采集时间 ----
    crawl_time = scrapy.Field()        # 采集时间 (ISO 8601)


class UserInfoItem(scrapy.Item):
    """B站用户详细信息 — 来自 /x/space/wbi/acc/info API"""

    mid = scrapy.Field()               # UID
    name = scrapy.Field()              # 昵称
    sex = scrapy.Field()               # 性别
    face = scrapy.Field()              # 头像 URL
    sign = scrapy.Field()              # 个人签名
    level = scrapy.Field()             # 等级 (0-6)
    birthday = scrapy.Field()          # 生日 (实际存储的是注册日期, B站API字段名就叫birthday)
    vip_status = scrapy.Field()        # 大会员状态
    official_verify = scrapy.Field()   # 认证信息 (dict: type/desc)
    follower = scrapy.Field()          # 粉丝数
    following = scrapy.Field()         # 关注数
    video_count = scrapy.Field()       # 投稿视频数
    post_count = scrapy.Field()        # 动态总数 (v2.1: F12账号骨架)
    upload_count = scrapy.Field()      # 投稿数别名 (v2.1: 与video_count同源)
    crawl_time = scrapy.Field()        # 采集时间


class UserPostItem(scrapy.Item):
    """B站用户空间动态 — 来自 /x/polymer/web-dynamic/v1/feed/space API

    用于 F13(转发抽奖) 和 F14(敏感内容) 检测。
    """

    mid = scrapy.Field()               # 用户 UID
    dynamic_id = scrapy.Field()        # 动态 ID (全局唯一)
    content = scrapy.Field()           # 动态文本内容 (纯文本)
    timestamp = scrapy.Field()         # 发布时间戳 (Unix timestamp)
    is_repost = scrapy.Field()         # 是否为转发动态 (bool)
    post_type = scrapy.Field()         # 动态类型 (DYNAMIC_TYPE_WORD/DRAW/AV/ARTICLE)
    crawl_time = scrapy.Field()        # 采集时间 (ISO 8601)


class DanmakuItem(scrapy.Item):
    """B站视频弹幕 — 来自 XML API (/x/v1/dm/list.so) 或 seg.so 分段 API

    用于展示弹幕内容和发送者分析。
    注意: mid_hash 是发送者 UID 的 CRC32 哈希（非真实 MID），
    但同一用户的 hash 值一致，可用于跨弹幕关联分析。
    """

    bvid = scrapy.Field()              # 所属视频 bvid
    cid = scrapy.Field()               # 视频分P cid
    danmaku_id = scrapy.Field()        # 弹幕 dbid (全局或段内唯一)
    content = scrapy.Field()           # 弹幕文本
    progress = scrapy.Field()          # 视频进度毫秒 (弹幕出现时间点)
    mode = scrapy.Field()              # 弹幕模式 (1-3=滚动, 4=底部, 5=顶部)
    fontsize = scrapy.Field()          # 字号 (12/16/18/25/28/36)
    color = scrapy.Field()             # 颜色 (10进制, 例如 16777215=白色)
    send_time = scrapy.Field()         # 发送时间戳 (Unix timestamp)
    mid_hash = scrapy.Field()          # 发送者 UID 哈希 (CRC32, 字符串)
    pool = scrapy.Field()              # 弹幕池 (0=普通, 1=字幕, 2=特殊)
    crawl_time = scrapy.Field()        # 采集时间 (ISO 8601)


class UpVideoItem(scrapy.Item):
    """B站 UP主投稿视频 — 来自 /x/space/wbi/arc/search API

    用于爬取指定 UP主 (mid) 的所有投稿视频列表。
    相比 VideoItem (单视频详情 API)，此 Item 的字段来自空间列表 API，
    缺少 cid、coin_count、like_count 等聚合统计字段。
    """

    # ---- 所属UP主 ----
    up_mid = scrapy.Field()            # UP主 UID (种子MID)
    up_name = scrapy.Field()           # UP主昵称 (从首个视频提取)

    # ---- 核心标识 ----
    bvid = scrapy.Field()              # BV号
    aid = scrapy.Field()               # AV号

    # ---- 视频信息 ----
    title = scrapy.Field()             # 标题
    description = scrapy.Field()       # 简介
    length = scrapy.Field()            # 时长 (mm:ss 格式字符串)
    created = scrapy.Field()           # 发布时间戳 (Unix timestamp)
    pic = scrapy.Field()               # 封面图 URL
    is_union_video = scrapy.Field()    # 是否联合投稿
    is_steins_gate = scrapy.Field()    # 是否互动视频
    is_pay = scrapy.Field()            # 是否付费视频

    # ---- 统计 ----
    play = scrapy.Field()              # 播放量
    video_review = scrapy.Field()      # 弹幕数
    comment = scrapy.Field()           # 评论数

    # ---- 分区 ----
    typeid = scrapy.Field()            # 一级分区 ID
    tname = scrapy.Field()             # 分区名称
    subtitle = scrapy.Field()          # 副标题/推荐语

    # ---- 元数据 ----
    crawl_time = scrapy.Field()        # 采集时间 (ISO 8601)
    page = scrapy.Field()              # 来源页码 (调试用)
    source = scrapy.Field()            # "up:{mid}" — UP主空间爬取
