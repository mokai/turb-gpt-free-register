# -*- coding: utf-8 -*-
"""本地指纹浏览器客户端（比特浏览器 / RoxyBrowser）可用性检查与友好提示。

注册、Codex 授权、WebUI 辅助接口都会调用这两个客户端的本地 HTTP API。
**客户端没启动时** requests 只会抛 `ConnectionError: ... Connection refused`，
用户看到的是英文堆栈，看不出"要先打开浏览器"。这里统一处理：

- `classify_client_error()` 识别"客户端没起来"这一类连接错误
  （端口无人监听 / 被环境代理劫持 / 连接超时）；
- `unavailable_message()` 把底层异常翻译成中文可操作提示；
- `ensure_client_ready()` 供流程开跑前主动探测，失败抛 `BrowserClientUnavailable`；
- `check_registration_driver()` 供 WebUI / CLI 在提交任务前拦截，返回提示文案。

两个客户端（`bitbrowser_client` / `roxybrowser_client`）的 request() 也会在
连接类错误上抛 `BrowserClientUnavailable`，所以任何调用方拿到的都是同一句提示。
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 本地探测超时：连接被拒绝是立即返回的，这里主要兜住"端口被占了但没人应答"。
PROBE_TIMEOUT = 3.0

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"}

# 回环地址必须直连：这些条目会被补进 no_proxy/NO_PROXY（幂等）。
_LOOPBACK_NO_PROXY_ENTRIES = ("127.0.0.1", "localhost", "::1")
_LOOPBACK_NO_PROXY_WARNED = False
_NO_PROXY_STAR_WARNED = False

# 客户端进程没起来的典型标记（requests / urllib3 的报错文本）
_DOWN_MARKERS = (
    "connection refused",
    "newconnectionerror",
    "failed to establish a new connection",
    "max retries exceeded",
    "cannot connect to proxy",
    "proxyerror",
    "connection aborted",
    "connection reset by peer",
    "nodename nor servname",
    "name or service not known",
    "no route to host",
    "network is unreachable",
)

_TIMEOUT_MARKERS = (
    "connecttimeout",
    "connect timeout",
    "read timed out",
    "readtimeout",
    "timed out",
    "timeout",
)


class BrowserClientUnavailable(RuntimeError):
    """本地浏览器客户端未启动 / 本地接口连不上。"""


def is_loopback_url(url: str) -> bool:
    """判断地址是否指向本机回环（本地客户端 API 都在回环上）。"""
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").strip().lower()
    except Exception:
        return False
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def disable_env_proxy_for_loopback(session, url: str) -> bool:
    """回环地址禁用 requests 的环境代理（trust_env）。

    设置了 HTTP_PROXY / HTTPS_PROXY 的机器上，requests 会把 127.0.0.1 的请求
    也丢给代理，结果是 502 ProxyError —— 看着像"客户端没启动"。这里对回环地址
    显式绕过环境代理；非回环（用户自建远程 API）保持原样。
    """
    if not is_loopback_url(url):
        return False
    try:
        session.trust_env = False
        return True
    except Exception:
        return False


def ensure_loopback_no_proxy() -> str:
    """把回环地址补进 no_proxy/NO_PROXY，返回生效后的值。

    为什么必须做：Selenium 的 RemoteConnection 在**构造时**读取环境里的
    HTTP_PROXY/HTTPS_PROXY（`selenium/webdriver/remote/client_config.py`
    的 `ClientConfig.get_proxy_url`），只有 no_proxy 命中回环时才直连。
    于是「从带代理的 shell 启动的进程」（如 CodeBuddy/终端里
    `export HTTP_PROXY=...` 后再 `./webui.sh start`）会让 Selenium 把发往
    chromedriver 的每个命令都丢给代理：代理对 `/session/<id>/...` 回
    `400 unhandled request`，业务侧只看到
    `WebDriverException: Message: unhandled request`——`POST /session`
    （Selenium 的第一个请求）反而是通的，所以现象像是"连上了却全挂"。

    `requests` 侧已经用 `disable_env_proxy_for_loopback()` 逐会话规避；
    这里补一条全局兜底，供创建 Selenium 驱动前调用（幂等）。
    """
    current = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    entries = [item.strip() for item in current.split(",") if item.strip()]
    if "*" in entries:
        # no_proxy='*' 在 libcurl 语义里是"禁用所有代理"：不只是跳过环境代理，
        # 还会把代码里**显式设置**的代理一起绕过（session.proxies 形同虚设）。
        # 后果是查套餐/查活/注册的流量全从本机出口出去，出口地区画像变成本机
        # 地区，按出口地区下发的 Plus 活动自然查不到。这里换成只覆盖回环的形式。
        global _NO_PROXY_STAR_WARNED
        entries = [item for item in entries if item != "*"]
        if not _NO_PROXY_STAR_WARNED:
            _NO_PROXY_STAR_WARNED = True
            logger.warning(
                "no_proxy 里含有 '*'：libcurl 会把它当成「禁用所有代理」，"
                "连代码里显式设置的代理（session.proxies）也会被绕过。"
                "已改成只覆盖回环地址 %s；启动脚本请勿再设 no_proxy='*'。",
                ",".join(_LOOPBACK_NO_PROXY_ENTRIES),
            )
    present = {item.lower().lstrip(".") for item in entries}
    missing = [host for host in _LOOPBACK_NO_PROXY_ENTRIES if host not in present]
    if not missing:
        if len(entries) == len([item.strip() for item in current.split(",") if item.strip()]):
            return current
        value = ",".join(entries)
        os.environ["no_proxy"] = value
        os.environ["NO_PROXY"] = value
        return value
    entries.extend(missing)
    value = ",".join(entries)
    os.environ["no_proxy"] = value
    os.environ["NO_PROXY"] = value
    global _LOOPBACK_NO_PROXY_WARNED
    proxy_env = sorted(
        key for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy") if os.environ.get(key)
    )
    if proxy_env and not _LOOPBACK_NO_PROXY_WARNED:
        _LOOPBACK_NO_PROXY_WARNED = True
        logger.warning(
            "检测到环境代理 %s 且 no_proxy 未覆盖回环地址，已自动补 no_proxy=%s。"
            "Selenium 会读环境代理，不补会让发往 chromedriver 的命令全部撞代理"
            "（表现为 WebDriverException: Message: unhandled request）。",
            ",".join(proxy_env),
            value,
        )
    return value


def classify_client_error(exc: BaseException) -> str | None:
    """把连接类异常分成 'down'（客户端没起来）/ 'timeout'（无响应），其他返回 None。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _DOWN_MARKERS):
        return "down"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    return None


def _short_error(exc: BaseException, limit: int = 220) -> str:
    text = " ".join(str(exc or "").split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def unavailable_message(
    browser_label: str,
    api_base: str,
    exc: BaseException | None = None,
    *,
    config_key: str = "",
    kind: str | None = None,
) -> str:
    """生成"请先启动 XXX 浏览器"的中文提示（含本地接口地址与原始错误）。"""
    base = str(api_base or "").strip() or "未配置"
    kind = kind or (classify_client_error(exc) if exc is not None else None) or "down"
    if kind == "timeout":
        head = f"无法连接{browser_label}本地接口 {base}（连接超时，客户端可能尚未启动完成或已卡死）。"
    elif "proxy" in f"{exc}".lower() or "proxyerror" in f"{type(exc).__name__}".lower():
        head = (
            f"无法连接{browser_label}本地接口 {base}（请求被代理拦截）。"
            "请把 127.0.0.1 加入 no_proxy 环境变量，或关闭系统代理后重试。"
        )
    else:
        head = f"无法连接{browser_label}本地接口 {base}（连接被拒绝，本地端口没有进程监听）。"

    lines = [
        head,
        f"请先启动「{browser_label}」客户端并保持已登录，确认本地接口地址与配置项 {config_key or '本地接口地址'} 一致后再重试。",
    ]
    if exc is not None:
        lines.append(f"原始错误：{_short_error(exc)}")
    return "\n".join(lines)


def probe_local_api(api_base: str, *, timeout: float = PROBE_TIMEOUT) -> None:
    """探测本地客户端接口是否在监听；不通则抛原始连接异常。

    只要能拿到任意 HTTP 响应（200 / 401 / 404 / 405…）就说明客户端进程在跑，
    因此这里不判断状态码。
    """
    import requests

    base = str(api_base or "").strip()
    if not base:
        raise RuntimeError("本地接口地址为空")
    if "://" not in base:
        base = f"http://{base}"
    session = requests.Session()
    disable_env_proxy_for_loopback(session, base)
    try:
        return session.get(base, timeout=max(0.5, float(timeout)))
    finally:
        try:
            session.close()
        except Exception:
            pass


def ensure_client_ready(
    browser_label: str,
    api_base: str,
    *,
    config_key: str = "",
    timeout: float = PROBE_TIMEOUT,
) -> bool:
    """探测本地客户端是否可用；不可用抛 BrowserClientUnavailable（含中文提示）。"""
    base = str(api_base or "").strip()
    if not base:
        raise BrowserClientUnavailable(
            f"未配置{browser_label}本地接口地址（配置项 {config_key or '本地接口地址'}）。"
            "请在配置页填写本地 API 地址后再重试。"
        )
    try:
        probe_local_api(base, timeout=timeout)
    except Exception as exc:
        kind = classify_client_error(exc) or "down"
        raise BrowserClientUnavailable(
            unavailable_message(browser_label, base, exc, config_key=config_key, kind=kind)
        ) from exc
    logger.debug("[%s] 本地接口可用：%s", browser_label, base)
    return True


def registration_driver_client() -> tuple[str, str, str] | None:
    """按 REGISTRATION_DRIVER 返回 (显示名, 本地接口地址, 配置键)；非本地客户端驱动返回 None。"""
    try:
        from config import roxybrowser as _roxy_cfg
    except Exception:
        return None
    mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "") or "").strip().lower()
    if mode in ("bit", "bitbrowser"):
        try:
            from config import bitbrowser as _bit_cfg
        except Exception:
            return None
        return "比特浏览器", str(getattr(_bit_cfg, "BIT_API_BASE", "") or "").strip(), "BIT_API_BASE"
    if mode in ("roxy", "roxybrowser", "fingerprint", "browser"):
        return "RoxyBrowser", str(getattr(_roxy_cfg, "ROXY_API_BASE", "") or "").strip(), "ROXY_API_BASE"
    return None


def check_registration_driver(*, timeout: float = PROBE_TIMEOUT) -> str | None:
    """提交注册任务前的预检：返回 None 表示可用，否则返回给用户看的中文提示。

    只对 bit / roxy 这类"必须先启动本地客户端"的驱动做检查；探测本身出错时不阻断。
    """
    target = registration_driver_client()
    if not target:
        return None
    label, api_base, config_key = target
    try:
        ensure_client_ready(label, api_base, config_key=config_key, timeout=timeout)
    except BrowserClientUnavailable as exc:
        return str(exc)
    except Exception as exc:  # 探测逻辑自身异常不阻断任务提交
        logger.warning("[%s] 预检本地接口失败（忽略）：%s", label, exc)
        return None
    return None
