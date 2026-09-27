# -*- coding: utf-8 -*-
"""
比特浏览器（BitBrowser）指纹浏览器自动化注册配置。

官方文档：
- 本地 API 默认地址: http://127.0.0.1:54345（比特浏览器系统设置中可查看/修改）
- 所有接口均为 POST + JSON body；响应 {success: true/false, data: ..., msg: ...}
- 本地接口默认无需 token
- 可配合 Selenium / Puppeteer / Playwright 自动化（/browser/open 返回 ws / http 调试地址和 chromedriver 路径）
"""
from config.env_loader import env_str, apply_env_overrides


# 注册驱动开关在 config/roxybrowser.py 的 REGISTRATION_DRIVER；值 "bit" 表示使用比特浏览器。

# 比特浏览器本地 API。无需 token。
BIT_API_BASE: str = "http://127.0.0.1:54345"

# 比特浏览器窗口 ID（浏览器窗口列表里的 id）；留空时每次运行创建新窗口。
BIT_PROFILE_ID: str = ""

# 分组 ID（可选）。比特浏览器没有 Roxy 的工作区概念，用 groupId 归类窗口；
# 可在比特浏览器窗口管理或 /group/list 里查看。
BIT_GROUP_ID: str = ""

# 打开窗口时是否无头启动：
#   False = 显示比特浏览器窗口（便于观察/调试）
#   True  = 通过启动参数 --headless=new 无头运行
BIT_OPEN_HEADLESS: bool = False

# 打开窗口时附加启动参数；会合并到 /browser/open 的 args 数组。
BIT_OPEN_EXTRA_PARAMS: list = []

# Selenium 行为
BIT_SELENIUM_TIMEOUT: int = 90
BIT_KEEP_BROWSER_OPEN: bool = False

# 比特 API transient 错误重试。create（/browser/update 无 id）超时后服务端可能已创建窗口，
# 直接重试可能产生孤儿窗口；默认不重试 create，open/close/delete 会重试。
BIT_API_RETRIES: int = 3
BIT_API_RETRY_DELAY: int = 2

# 窗口生命周期：
#   True  = 一号一窗口：每个账号强制创建新窗口，用完关闭并删除，不允许复用 BIT_PROFILE_ID
#   False = 可复用 BIT_PROFILE_ID 或只关闭不删除
BIT_ONE_PROFILE_PER_ACCOUNT: bool = True

# 一号一窗口结束后是否删除窗口。建议保持 True。
BIT_DELETE_PROFILE_AFTER_RUN: bool = True

# 创建窗口时是否使用随机指纹；比特 /browser/update 的 browserFingerPrint 传空对象 {} 即随机指纹。
BIT_RANDOM_FINGERPRINT: bool = True

# 内核版本（browserFingerPrint.coreVersion）；留空使用比特默认/随机内核。
BIT_CORE_VERSION: str = ""

# 创建窗口时随机名称；开启后每次创建生成不同名称。
BIT_RANDOM_PROFILE_NAME_ON_CREATE: bool = True
BIT_PROFILE_NAME_PREFIX: str = "bit"

# 创建比特窗口时是否使用 config/proxy.py 的 PROXY_POOL：
#   False = 不主动给比特窗口设置代理
#   True  = 每次创建窗口时从 PROXY_POOL 随机取一个代理写入 proxyMethod=2 的自定义代理
BIT_CREATE_USE_PROXY_POOL: bool = False

# 比特 Codex 授权等待 callback 的最长秒数
BIT_CODEX_CALLBACK_TIMEOUT: int = 180


# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'BIT_API_BASE': 'str', 'BIT_PROFILE_ID': 'str', 'BIT_GROUP_ID': 'str', 'BIT_OPEN_HEADLESS': 'bool', 'BIT_KEEP_BROWSER_OPEN': 'bool', 'BIT_ONE_PROFILE_PER_ACCOUNT': 'bool', 'BIT_DELETE_PROFILE_AFTER_RUN': 'bool', 'BIT_RANDOM_FINGERPRINT': 'bool', 'BIT_CORE_VERSION': 'str', 'BIT_RANDOM_PROFILE_NAME_ON_CREATE': 'bool', 'BIT_PROFILE_NAME_PREFIX': 'str', 'BIT_CREATE_USE_PROXY_POOL': 'bool', 'BIT_CODEX_CALLBACK_TIMEOUT': 'int'})
