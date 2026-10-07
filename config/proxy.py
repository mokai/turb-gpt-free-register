# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
import random
import re
import threading
import time
from urllib.parse import quote, urlparse

from config.env_loader import apply_env_overrides
from config.task_proxy import (
    clear_task_pool,
    get_task_group_name,
    get_task_pool,
    set_task_pool,
    task_pool,
)


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
#
# 该列表就是「默认分组」的代理；未特别指定分组时，所有任务都走这里。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# Optional first hop for the default proxy pool.
PROXY_POOL_UPSTREAM_PROXY = ""

# 默认分组名。WebUI 中显示的"默认分组"就是这个池子；只有一个分组时它就是默认分组。
# 可在配置页「代理池」把任意分组「设为默认」，保存时写入 .env 的 DEFAULT_PROXY_GROUP。
DEFAULT_PROXY_GROUP = "默认"

# 「未指定分组」的别名（大小写不敏感）。
# 这里**只放符号化写法**：不能再放「默认」「默认分组」这类词——用户完全可以把某个
# 分组真的命名为「默认」，一旦当成别名就会被强行改写成默认分组名，导致该分组消失。
_DEFAULT_GROUP_ALIASES = {"", "-", "*", "default"}
# 命名代理分组：{分组名: [代理 URL, ...]}。默认分组单独用 PROXY_POOL 承载，
# 因此这里只保存额外命名的分组（例如 KN / IN）。
PROXY_GROUPS: dict[str, list[str]] = {}
# 代理出口测试（IP2Location）地址：每行一个，按顺序探活，第一个可用的即作为结论。
# IP2Location 免密钥接口限 1000 次/天，因此默认放在最前面。
PROXY_TEST_GEO_URLS = [
    "https://api.ip2location.io/?format=json",
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
]
PROXY_TEST_TIMEOUT = 12.0
# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；没有代理时才按 direct 运行
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 支持配置多个（每行一个），每次请求从池中随机选一个健康出口；
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY: list[str] = []

# Optional first hop for plan-check and Codex-token traffic.
PLAN_CHECK_UPSTREAM_PROXY = ""

# 邮箱换绑专用代理池（每行一个）。留空时换绑沿用通用代理池（PROXY_POOL）选路。
# 配置后，换绑本身的网络请求（change_email begin/verify、新邮箱会话）优先从这组
# 代理中选取；换绑过程中涉及的查活 / 原邮箱重认证步骤仍然使用通用代理池。
EMAIL_CHANGE_PROXY_POOL: list[str] = []
# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8

# 自适应代理池：记录失败出口并在冷却期内跳过。
PROXY_ADAPTIVE_ROUTING = True
PROXY_COOLDOWN_403_SECONDS = 900
PROXY_COOLDOWN_429_SECONDS = 300
PROXY_MAX_ROUTE_ATTEMPTS = 4

_PROXY_HEALTH: dict[str, dict[str, float | int | str]] = {}
_PROXY_HEALTH_LOCK = threading.Lock()


def _proxy_key(value: str | None) -> str:
    """统一代理标识，供健康状态与换路排除集合使用。"""
    return str(value or "").strip()


def report_proxy_result(
    proxy: str | None,
    *,
    status: int | None = None,
    ok: bool | None = None,
    error: str = "",
) -> None:
    """记录代理请求结果，自适应路由据此暂时跳过失败出口。"""
    key = _proxy_key(proxy)
    if not key or not PROXY_ADAPTIVE_ROUTING:
        return
    now = time.time()
    status = int(status or 0)
    with _PROXY_HEALTH_LOCK:
        item = _PROXY_HEALTH.setdefault(key, {"failures": 0, "cooldown_until": 0.0})
        if ok is True or status in range(200, 400):
            item["failures"] = 0
            item["cooldown_until"] = 0.0
            item["last_ok"] = now
            return
        if status == 403:
            cooldown = max(0, int(PROXY_COOLDOWN_403_SECONDS))
        elif status == 429:
            cooldown = max(0, int(PROXY_COOLDOWN_429_SECONDS))
        else:
            cooldown = 60
        item["failures"] = int(item.get("failures", 0) or 0) + 1
        item["cooldown_until"] = now + cooldown
        item["last_error"] = str(error or f"HTTP {status}" if status else error)[:300]


def proxy_is_cooling_down(proxy: str | None) -> bool:
    """Return whether adaptive routing currently skips this proxy."""
    key = _proxy_key(proxy)
    if not key or not PROXY_ADAPTIVE_ROUTING:
        return False
    with _PROXY_HEALTH_LOCK:
        item = _PROXY_HEALTH.get(key) or {}
        return float(item.get("cooldown_until", 0.0) or 0.0) > time.time()


def _valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    """Normalize common proxy notations without altering invalid input.

    Accepted shorthand formats are ``host:port``, ``host:port:user:password``,
    and ``user:password:host:port``.
    """
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[0] and _valid_port(parts[1]) and parts[2] and parts[3]:
        host, port, username, password = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 4 and parts[0] and parts[1] and parts[2] and _valid_port(parts[3]):
        username, password, host, port = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    parsed = urlparse(f"//{text}")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.hostname and port:
        return f"{default_scheme}://{text}"
    return text


def normalize_proxy_list(values, default_scheme: str = "http") -> list[str]:
    """Normalize a multiline proxy list and omit empty entries."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value, default_scheme=default_scheme))
    ]


# ------------------------------------------------------------
# 代理分组
# ------------------------------------------------------------

_GROUP_BRACKET_RE = re.compile(r"^\[(?P<name>[^\[\]]{1,40})\]$")
# host:port / host:port:user:pass / user:pass@host:port 这类「看起来就是代理」的行
_PROXY_SHORTHAND_RE = re.compile(r"^[^:\s/]+:\d{1,5}(?:[:@].*)?$")


def _looks_like_proxy(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if "://" in value:
        return True
    return bool(_PROXY_SHORTHAND_RE.match(value))


def is_default_group(name: str | None) -> bool:
    return str(name or "").strip().lower() in _DEFAULT_GROUP_ALIASES


def normalize_group_name(name: str | None) -> str:
    """规范化分组名；默认分组的各种别名统一成 DEFAULT_PROXY_GROUP。"""
    value = str(name or "").strip()
    if is_default_group(value):
        return DEFAULT_PROXY_GROUP
    return re.sub(r"\s+", " ", value)[:40]


def parse_proxy_groups_text(text) -> dict[str, list[str]]:
    """解析多分组代理文本。

    支持三种写法（可混用），默认分组用 `默认:` 表示：

        [KN]
        socks5://1.2.3.4:1080

        IN:
        http://user:pass@5.6.7.8:8080

        KN: http://9.9.9.9:3128        # 行内带一个代理

    解析不出分组头的代理行归入默认分组。
    """
    groups: dict[str, list[str]] = {}
    if text is None:
        return groups
    if isinstance(text, dict):
        for key, value in text.items():
            name = normalize_group_name(key)
            pool = normalize_proxy_list(value)
            if pool:
                groups.setdefault(name, []).extend(pool)
        return _dedupe_groups(groups)

    lines = text.splitlines() if isinstance(text, str) else list(text)
    current = DEFAULT_PROXY_GROUP
    for raw in lines:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue

        bracket = _GROUP_BRACKET_RE.match(line)
        if bracket:
            current = normalize_group_name(bracket.group("name"))
            groups.setdefault(current, [])
            continue

        # `名称:` 单独一行，或 `名称: 协议://完整代理`（行内带代理）。
        # 注意：`user:pass@host:port` 这种简写同样含冒号，因此行内形式只认
        # 带协议头（://）的写法，避免把简写代理误判成分组名。
        head, sep, tail = line.partition(":")
        if not sep:
            head, sep, tail = line.partition("：")
        tail = tail.strip()
        if sep and head.strip() and not _looks_like_proxy(head.strip()) and (not tail or "://" in tail):
            current = normalize_group_name(head)
            groups.setdefault(current, [])
            if tail:
                normalized = normalize_proxy_url(tail)
                if normalized:
                    groups[current].append(normalized)
            continue

        normalized = normalize_proxy_url(line)
        if normalized:
            groups.setdefault(current, []).append(normalized)
    return _dedupe_groups(groups)


def _dedupe_groups(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, pool in groups.items():
        seen: set[str] = set()
        items: list[str] = []
        for item in pool:
            if item in seen:
                continue
            seen.add(item)
            items.append(item)
        if items:
            out[name] = items
    return out


def format_proxy_groups_text(groups: dict[str, list[str]]) -> str:
    """把多分组结构序列化成 .env 里保存的文本（默认分组写在最前面）。"""
    lines: list[str] = []
    items = _dedupe_groups({normalize_group_name(k): list(v or []) for k, v in (groups or {}).items()})
    ordered = [DEFAULT_PROXY_GROUP] if DEFAULT_PROXY_GROUP in items else []
    ordered += [name for name in items if name != DEFAULT_PROXY_GROUP]
    for name in ordered:
        lines.append(f"{name}:")
        lines.extend(items[name])
        lines.append("")
    return "\n".join(lines).strip()


def proxy_groups() -> dict[str, list[str]]:
    """返回全部代理分组（含默认分组），默认分组排在最前面。"""
    merged: dict[str, list[str]] = {DEFAULT_PROXY_GROUP: list(PROXY_POOL or [])}
    for name, pool in (PROXY_GROUPS or {}).items():
        key = normalize_group_name(name)
        if key == DEFAULT_PROXY_GROUP:
            continue
        merged[key] = list(pool or [])
    if not merged[DEFAULT_PROXY_GROUP]:
        # 默认分组为空时不显示占位，保持和"未配置代理"一致的语义
        merged[DEFAULT_PROXY_GROUP] = []
    return merged


def proxy_group_names(*, only_with_proxies: bool = False) -> list[str]:
    names = list(proxy_groups().keys())
    if only_with_proxies:
        groups = proxy_groups()
        names = [n for n in names if groups.get(n)]
    return names


def proxy_group_pool(name: str | None) -> list[str]:
    """取分组代理列表；分组不存在或为空返回 []。"""
    key = normalize_group_name(name)
    groups = proxy_groups()
    if key in groups:
        return list(groups[key] or [])
    return []


def resolve_group_pool(name: str | None) -> list[str] | None:
    """把分组名解析成代理列表；None 表示"未指定分组"（继续走全局配置）。

    分组名有效但分组内没有代理时返回空列表（明确的空池）。
    """
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    key = normalize_group_name(raw)
    if key == DEFAULT_PROXY_GROUP:
        return list(PROXY_POOL or [])
    groups = proxy_groups()
    if key in groups:
        return list(groups[key] or [])
    return None


def resolve_group_pools(names) -> list[str]:
    """把多个分组名合并成一个代理池（查套餐多选分组用）。"""
    if names is None:
        return []
    if isinstance(names, str):
        names = [part for part in re.split(r"[,\n]", names) if part.strip()]
    merged: list[str] = []
    seen: set[str] = set()
    for name in names:
        for item in proxy_group_pool(name):
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def task_pool_active() -> list[str] | None:
    """当前线程是否被指定了任务级代理池。"""
    return get_task_pool()


def pick_proxy(
    exclude: set[str] | list[str] | tuple[str, ...] | None = None,
    *,
    allow_excluded_fallback: bool = False,
    pool: list[str] | tuple[str, ...] | None = None,
) -> str:
    """从代理池随机抽取健康出口。

    选路优先级：
        1. 显式传入的 pool（如换绑专用代理池、套餐查询代理池）；
        2. 当前线程的任务级代理池（WebUI 上选中的代理分组）；
        3. 全局默认分组 PROXY_POOL。

    动态住宅代理只有一个入口 URL 时，即使该入口被排除，也允许复用，
    因为每次新建连接可能轮换真实出口 IP。
    """
    if pool is not None:
        source_pool = list(pool)
    else:
        task = get_task_pool()
        source_pool = list(task) if task else list(PROXY_POOL)
    excluded = {_proxy_key(item) for item in (exclude or ()) if _proxy_key(item)}
    candidates = [_proxy_key(item) for item in source_pool if _proxy_key(item) not in excluded]
    if not candidates and allow_excluded_fallback:
        all_candidates = [_proxy_key(item) for item in source_pool if _proxy_key(item)]
        if len(set(all_candidates)) == 1:
            candidates = all_candidates
    if not candidates:
        return ""
    if PROXY_ADAPTIVE_ROUTING:
        healthy = [item for item in candidates if not proxy_is_cooling_down(item)]
        if healthy:
            candidates = healthy
    return random.choice(candidates)

# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'DEFAULT_PROXY_GROUP': 'str',
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_GROUPS': 'proxy_groups',
    'PROXY_TEST_GEO_URLS': 'list_str_multiline',
    'PROXY_TEST_TIMEOUT': 'float',
    'PROXY_POOL_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'list_str_multiline',
    'PLAN_CHECK_UPSTREAM_PROXY': 'str',
    'EMAIL_CHANGE_PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PROXY_ADAPTIVE_ROUTING': 'bool',
    'PROXY_COOLDOWN_403_SECONDS': 'int',
    'PROXY_COOLDOWN_429_SECONDS': 'int',
    'PROXY_MAX_ROUTE_ATTEMPTS': 'int',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY_POOL = normalize_proxy_list(PROXY_POOL)
PROXY_GROUPS = parse_proxy_groups_text(PROXY_GROUPS or {})
PROXY_GROUPS = {k: v for k, v in PROXY_GROUPS.items() if k != DEFAULT_PROXY_GROUP}
PROXY_TEST_GEO_URLS = [str(x).strip() for x in (PROXY_TEST_GEO_URLS or []) if str(x).strip()]
PLAN_CHECK_PROXY = normalize_proxy_list(PLAN_CHECK_PROXY)
EMAIL_CHANGE_PROXY_POOL = normalize_proxy_list(EMAIL_CHANGE_PROXY_POOL)

# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()
