# -*- coding: utf-8 -*-
"""dasaobi 公益提链（UPI 公益站）服务配置。

与官方提链（config/extract_link.py，走 CDK + SSE 事件流）互相独立，
本模块只承载 dasaobi 公益站（https://dasaobi.online/upi/）的异步提链参数。
该渠道无需 CDK，客户端提交自己的 Access Token（AT）+ 印度随机出口代理，
公益站异步处理，客户端轮询查询结果。

全部配置通过 .env 覆盖，改动后 reload_all() 热加载即可生效。
"""
from config.env_loader import apply_env_overrides

# dasaobi 公益站基础地址。默认取线上实现地址。
UPI_PUBLIC_API_BASE: str = "https://dasaobi.online/upi/"

# 提交任务 / 查询结果时的单次请求超时（秒）。
UPI_PUBLIC_REQUEST_TIMEOUT: int = 30

# 轮询任务结果的间隔（秒）。
UPI_PUBLIC_POLL_INTERVAL: float = 2.0

# 单个任务最长轮询时长（秒）；超时视为失败。
UPI_PUBLIC_POLL_TIMEOUT: int = 600

# Cookie 会话持久化目录。公益站首次访问会下发 upi_paid_session Cookie（24h），
# 后续查询/取消必须带回同一个 Cookie。保存到该目录下，进程重启后可复用。
UPI_PUBLIC_COOKIE_DIR: str = ""

# 后台公益提链并发与队列上限。
UPI_PUBLIC_WORKERS: int = 2
UPI_PUBLIC_QUEUE_LIMIT: int = 500

# 创建 / 更新 Checkout 时使用的印度（IN）随机出口代理池（每行一个）。
# 留空则从通用代理分组（DEFAULT_PROXY_GROUP）中随机选 IN 出口。
# 代理可能包含账号密码，因此保存到 .env。
UPI_PUBLIC_PROXY_POOL: str = ""

apply_env_overrides(globals(), {
    'UPI_PUBLIC_API_BASE': 'str',
    'UPI_PUBLIC_REQUEST_TIMEOUT': 'int',
    'UPI_PUBLIC_POLL_INTERVAL': 'float',
    'UPI_PUBLIC_POLL_TIMEOUT': 'int',
    'UPI_PUBLIC_COOKIE_DIR': 'str',
    'UPI_PUBLIC_WORKERS': 'int',
    'UPI_PUBLIC_QUEUE_LIMIT': 'int',
    'UPI_PUBLIC_PROXY_POOL': 'str',
})
