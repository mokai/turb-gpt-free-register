# -*- coding: utf-8 -*-
"""比特浏览器（BitBrowser）本地 API 客户端。

比特浏览器本地接口约定（https://doc.bitbrowser.cn）：
- 默认地址 http://127.0.0.1:54345，本地接口默认无需 token
- 所有接口均为 POST + JSON body
- 响应统一为 {success: true/false, data: ..., msg: ...}
- /browser/update 不传 id 即创建窗口，传 id 为修改
- /browser/open 返回 data.ws（CDP WebSocket）、data.http（debugger 地址）、data.driver（chromedriver 路径）
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import bitbrowser as _cfg
from core.browser_client_ready import (
    BrowserClientUnavailable,
    classify_client_error,
    disable_env_proxy_for_loopback,
    unavailable_message,
)

logger = logging.getLogger(__name__)


@dataclass
class BitOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    created_by_run: bool = False
    browser_kind: str = "Bit"


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return str(proxy_url or "").strip()


def _proxy_url_to_bit_info(proxy_url: str) -> dict:
    """
    将 config/proxy.py 里的代理 URL 转成比特 /browser/update 的自定义代理字段。

    比特官方字段：proxyMethod(2=自定义) / proxyType(noproxy/http/https/socks5/ssh) /
    host / port / proxyUserName / proxyPassword。
    支持：
      http://user:pass@host:port
      https://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port  -> 比特侧按 SOCKS5 处理
    """
    text = str(proxy_url or "").strip()
    if not text:
        raise ValueError("代理为空")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"比特浏览器暂不支持该代理协议: {scheme or '-'}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式缺少 host/port: {_mask_proxy(text)}")

    protocol = {
        "http": "http",
        "https": "https",
        "socks5": "socks5",
        "socks5h": "socks5",
    }[scheme]
    info = {
        "proxyMethod": 2,
        "proxyType": protocol,
        "host": parsed.hostname,
        "port": str(parsed.port),
    }
    if parsed.username:
        info["proxyUserName"] = unquote(parsed.username)
    if parsed.password:
        info["proxyPassword"] = unquote(parsed.password)
    return info


def _dig(payload, *keys):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(payload, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value = _dig(payload, *path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _random_profile_name() -> str:
    prefix = str(getattr(_cfg, "BIT_PROFILE_NAME_PREFIX", "bit") or "bit").strip() or "bit"
    # 比特窗口名每次创建都不同：前缀 + 毫秒时间戳 + 随机 4 位十六进制。
    return f"{prefix}-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


def _build_fingerprint() -> dict:
    """比特 /browser/update 的 browserFingerPrint 必传；传空对象 {} 即随机指纹。"""
    fp: dict = {}
    core_version = str(getattr(_cfg, "BIT_CORE_VERSION", "") or "").strip()
    if core_version:
        fp["coreVersion"] = core_version
    return fp


def _apply_data_saver_open_args(args: list) -> list:
    """在比特启动参数中尽早关闭图片加载（复用 Roxy 的省流量开关逻辑）。"""
    try:
        from core.roxybrowser_client import _apply_data_saver_open_args as _roxy_apply

        params = {"args": list(args or [])}
        params = _roxy_apply(params)
        return list(params.get("args") or [])
    except Exception as exc:
        logger.debug("[Bit] 添加省流量启动参数失败，继续使用原参数：%s", exc)
        return list(args or [])


class BitBrowserClient:
    # 用于"客户端没启动"时的中文提示文案。
    browser_label = "比特浏览器"
    config_key = "BIT_API_BASE"

    def __init__(self, api_base: str | None = None):
        self.api_base = (api_base or _cfg.BIT_API_BASE).strip()
        self.http = requests.Session()
        # 本地接口在回环上：绕过 HTTP_PROXY/HTTPS_PROXY，否则会被系统代理拦成 502。
        disable_env_proxy_for_loopback(self.http, self.api_base)
        self.http.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def unavailable_error(self, exc: BaseException | None = None) -> BrowserClientUnavailable:
        """构造"请先启动比特浏览器"的可读异常。"""
        return BrowserClientUnavailable(
            unavailable_message(
                self.browser_label,
                self.api_base,
                exc,
                config_key=self.config_key,
                kind=classify_client_error(exc) if exc is not None else "down",
            )
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, path: str, json_body: dict | None = None) -> dict:
        url = _join_url(self.api_base, path)
        # create（/browser/update 无 id）超时后服务端可能已创建窗口，直接重试
        # 可能产生孤儿窗口；默认不重试 create。
        is_create = (
            str(path or "").rstrip("/") == "/browser/update"
            and not (json_body or {}).get("id")
        )
        max_attempts = 1 if is_create else max(1, int(getattr(_cfg, "BIT_API_RETRIES", 3) or 3))
        base_delay = max(0.5, float(getattr(_cfg, "BIT_API_RETRY_DELAY", 2) or 2))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug("[Bit] POST %s body=%s attempt=%s/%s", url, json_body, attempt, max_attempts)
                resp = self.http.post(
                    url,
                    json=json_body if json_body is not None else {},
                    timeout=max(5, int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90)),
                )
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(f"比特 API 请求失败 POST {path} HTTP {resp.status_code}: {text[:500]}")
                if isinstance(payload, dict):
                    success = payload.get("success")
                    if success is not True:
                        msg = payload.get("msg") or payload.get("message") or json.dumps(payload, ensure_ascii=False)[:500]
                        raise RuntimeError(f"比特 API 返回失败 POST {path}: {msg}")
                if attempt > 1:
                    logger.info("[Bit] API 重试成功：POST %s attempt=%s/%s", path, attempt, max_attempts)
                return payload if isinstance(payload, dict) else {"data": payload, "success": True}
            except Exception as exc:
                last_exc = exc
                kind = classify_client_error(exc)
                if kind == "down":
                    # 客户端没启动（端口无人监听 / 被代理拦截）：重试没有意义，
                    # 直接给出"请先启动比特浏览器"的提示，别让用户看英文堆栈。
                    logger.warning("[Bit] 本地接口不可用（%s）：%s", self.api_base, exc)
                    raise self.unavailable_error(exc) from exc
                retryable = self._is_retryable_error(exc)
                if attempt >= max_attempts or not retryable:
                    if kind == "timeout":
                        # 客户端可能没启动、或启动中卡住：统一转成可读提示。
                        raise self.unavailable_error(exc) from exc
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Bit] API 请求失败，将在 %.1fs 后重试：POST %s attempt=%s/%s error=%s",
                    delay, path, attempt, max_attempts, exc,
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"比特 API 请求失败 POST {path}")

    def try_request(self, path: str, json_body: dict | None = None) -> tuple[bool, dict | str]:
        """宽松请求：用于探测接口，失败不抛出。

        唯一例外是"客户端没启动"（BrowserClientUnavailable）：这不是接口风格不匹配，
        必须上抛给调用方去提示用户启动比特浏览器，否则会被降级成"探测失败"。
        """
        try:
            return True, self.request(path, json_body=json_body)
        except BrowserClientUnavailable:
            raise
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _extract_items(payload, list_keys: tuple[str, ...], id_keys: tuple[str, ...], name_keys: tuple[str, ...]) -> list[dict]:
        """从比特分页响应（data.dataList 等）里递归抽取 id/name 条目。"""
        out: list[dict] = []
        seen: set[str] = set()

        def pick(item: dict) -> tuple[str, str]:
            pid = ""
            for key in id_keys:
                value = item.get(key)
                if value not in (None, ""):
                    pid = str(value).strip()
                    break
            name = ""
            for key in name_keys:
                value = item.get(key)
                if value not in (None, ""):
                    name = str(value).strip()
                    break
            return pid, name

        def walk(node):
            if isinstance(node, dict):
                # 分页容器：data: {dataList: [...]} / data: {list: [...]}
                for key in list_keys:
                    value = node.get(key)
                    if isinstance(value, list):
                        for entry in value:
                            walk(entry)
                    if key in node:
                        return
                pid, name = pick(node)
                if pid and pid not in seen:
                    seen.add(pid)
                    out.append({"id": pid, "name": name or pid, "raw": node})
                return
            if isinstance(node, list):
                for entry in node:
                    walk(entry)

        walk(payload)
        return out

    def list_groups(self) -> dict:
        """获取比特浏览器分组列表（POST /group/list）。"""
        candidates = [
            ("/group/list", {"page": 0, "pageSize": 100}),
            ("/group/list", {}),
            ("/browser/group/list", {"page": 0, "pageSize": 100}),
        ]
        errors = []
        for path, body in candidates:
            ok, payload = self.try_request(path, body)
            if not ok:
                errors.append({"path": path, "error": payload})
                continue
            items = self._extract_items(
                payload,
                list_keys=("dataList", "list", "rows", "records", "groups"),
                id_keys=("groupId", "id", "group_id"),
                name_keys=("groupName", "name", "group_name"),
            )
            if items:
                return {"ok": True, "path": path, "items": items, "raw": payload}
            # 接口成功但确实是空列表（比如还没创建过分组）：直接返回空结果，
            # 不继续换路径探测，避免把空列表误报成失败。
            data = payload.get("data") if isinstance(payload, dict) else None
            has_list_shape = isinstance(data, dict) and any(
                isinstance(data.get(k), list) for k in ("dataList", "list", "rows", "records")
            )
            if has_list_shape:
                return {"ok": True, "path": path, "items": [], "raw": payload}
            errors.append({"path": path, "error": "响应中未解析到分组列表", "payload": payload})
        return {"ok": False, "items": [], "errors": errors}

    def create_window(self, payload: dict | None = None) -> str:
        """POST /browser/update（不传 id 即创建），返回窗口 id。"""
        body: dict = {}
        random_name_enabled = bool(getattr(_cfg, "BIT_RANDOM_PROFILE_NAME_ON_CREATE", True))
        if random_name_enabled:
            body["name"] = _random_profile_name()
        else:
            body["name"] = "gpt-free-register"
        group_id = str(getattr(_cfg, "BIT_GROUP_ID", "") or "").strip()
        if group_id:
            body["groupId"] = group_id
        if bool(getattr(_cfg, "BIT_CREATE_USE_PROXY_POOL", False)):
            from config import proxy as _proxy_cfg

            proxy_url = _proxy_cfg.pick_proxy()
            if proxy_url:
                info = _proxy_url_to_bit_info(proxy_url)
                body.update(info)
                logger.info(
                    "[Bit] 创建窗口启用代理池：proxy=%s type=%s host=%s port=%s",
                    _mask_proxy(proxy_url),
                    info.get("proxyType"),
                    info.get("host"),
                    info.get("port"),
                )
            else:
                logger.warning("[Bit] 已启用 BIT_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建窗口不设置代理")
        body["browserFingerPrint"] = _build_fingerprint()
        if payload:
            body.update(payload)
        logger.info(
            "[Bit] 创建窗口参数：groupId=%s name=%s random_name=%s fingerprint=%s",
            body.get("groupId") or "-",
            body.get("name") or "-",
            random_name_enabled,
            body.get("browserFingerPrint") or {},
        )
        try:
            result = self.request("/browser/update", body)
        except BrowserClientUnavailable:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"比特创建窗口失败：{exc}\n"
                f"请确认「{self.browser_label}」客户端已启动并已登录（未登录时无法创建窗口）。"
            ) from exc
        window_id = _first(result, [
            ("data", "id"), ("data", "groupId"), ("id",), ("browserId",),
        ])
        if not window_id:
            raise RuntimeError(f"比特创建窗口成功但未返回 id: {result}")
        return window_id

    @staticmethod
    def _normalize_profile_id(value: str | None) -> str:
        text = str(value or "").strip()
        # WebUI/人工配置里常用 - 表示“未配置”，这里统一按空处理。
        if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
            return ""
        return text

    def open_window(self, profile_id: str | None = None) -> BitOpenResult:
        one_profile = bool(getattr(_cfg, "BIT_ONE_PROFILE_PER_ACCOUNT", True))
        configured_pid = self._normalize_profile_id(profile_id if profile_id is not None else getattr(_cfg, "BIT_PROFILE_ID", ""))
        if one_profile and configured_pid:
            raise RuntimeError(
                "已启用 BIT_ONE_PROFILE_PER_ACCOUNT=True（一号一窗口），"
                "不能配置/传入固定 BIT_PROFILE_ID；请留空以便每个账号创建新窗口。"
            )

        pid = configured_pid
        created_by_run = False
        if not pid:
            pid = self.create_window()
            created_by_run = True
            logger.info("[Bit] 已创建临时窗口：%s", pid)

        args = list(getattr(_cfg, "BIT_OPEN_EXTRA_PARAMS", []) or [])
        if bool(getattr(_cfg, "BIT_OPEN_HEADLESS", False)) and not any(
            str(a or "").strip().startswith("--headless") for a in args
        ):
            args.append("--headless=new")
        args = _apply_data_saver_open_args(args)
        body = {"id": pid}
        if args:
            body["args"] = args
        logger.info(
            "[Bit] open 参数：window=%s headless=%s keep_open=%s",
            pid, bool(getattr(_cfg, "BIT_OPEN_HEADLESS", False)), getattr(_cfg, "BIT_KEEP_BROWSER_OPEN", False),
        )
        try:
            result = self.request("/browser/open", body)
        except BrowserClientUnavailable:
            raise
        except Exception as exc:
            # 客户端在跑但窗口没打开：常见于客户端未登录 / 窗口被删 / 内核异常。
            raise RuntimeError(
                f"比特窗口打开失败（窗口 {pid}）：{exc}\n"
                f"请确认「{self.browser_label}」客户端已启动并已登录，且该窗口可以正常打开。"
            ) from exc
        data = result.get("data") if isinstance(result, dict) else {}
        if not isinstance(data, dict):
            data = {}
        debugger_address = self._extract_debugger_address(data or result)
        ws_endpoint = _first(data, [("ws",), ("wsEndpoint",), ("webSocketDebuggerUrl",)]) or None
        webdriver_url = _first(data, [("webdriver",), ("selenium",), ("seleniumUrl",)]) or None
        if not debugger_address and not ws_endpoint and not webdriver_url:
            raise RuntimeError(f"比特已打开窗口但未返回调试地址，请检查 /browser/open 响应: {result}")
        if debugger_address:
            logger.info("[Bit] open 返回摘要: debugger=%s", debugger_address)
        else:
            logger.info("[Bit] open 返回摘要: ws=%s", (ws_endpoint or "")[:120])
        return BitOpenResult(
            pid,
            result,
            debugger_address=debugger_address,
            webdriver_url=webdriver_url,
            ws_endpoint=ws_endpoint,
            created_by_run=created_by_run,
        )

    # 兼容 Roxy 侧调用习惯的别名。
    open_profile = open_window

    def close_window(self, profile_id: str) -> None:
        if not profile_id:
            return
        try:
            self.request("/browser/close", {"id": profile_id})
            logger.info("[Bit] 已关闭窗口：%s", profile_id)
        except Exception as exc:
            logger.warning("[Bit] 关闭窗口失败：%s", exc)

    close_profile = close_window

    def delete_window(self, profile_id: str) -> None:
        if not profile_id:
            return
        try:
            self.request("/browser/delete", {"id": profile_id})
            logger.info("[Bit] 已删除窗口：%s", profile_id)
        except Exception as exc:
            logger.warning("[Bit] 删除窗口失败：%s", exc)

    delete_profile = delete_window

    def cleanup_profile(self, opened: BitOpenResult | None) -> None:
        """任务结束清理：关闭窗口；一号一窗口时删除本轮创建的窗口。"""
        if not opened or not opened.profile_id:
            return
        keep_open = bool(getattr(_cfg, "BIT_KEEP_BROWSER_OPEN", False))
        if not keep_open:
            self.close_window(opened.profile_id)

        should_delete = (
            bool(getattr(_cfg, "BIT_ONE_PROFILE_PER_ACCOUNT", True))
            and bool(getattr(_cfg, "BIT_DELETE_PROFILE_AFTER_RUN", True))
            and bool(opened.created_by_run)
        )
        if should_delete:
            # 删除前尽量确保已关闭；若 keep_open=True 则不删除，便于调试保留现场。
            if keep_open:
                logger.info("[Bit] BIT_KEEP_BROWSER_OPEN=True，跳过删除窗口：%s", opened.profile_id)
                return
            self.delete_window(opened.profile_id)

    def health(self) -> dict:
        """健康检查（POST /health），用于 WebUI 连通性测试。"""
        return self.request("/health", {})

    @staticmethod
    def _extract_debugger_address(payload) -> str | None:
        value = _first(payload, [
            ("http",), ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debuggingPortUrl",), ("remoteDebuggingAddress",),
            ("data", "http"), ("data", "debuggerAddress"), ("data", "debugger_address"),
            ("data", "debugAddress"), ("data", "debuggingPortUrl"), ("data", "remoteDebuggingAddress"),
        ])
        if value:
            value = value.strip()
            # 兼容 http://127.0.0.1:xxxx / 127.0.0.1:xxxx / :xxxx / 9222
            value = value.replace("http://", "").replace("https://", "").strip("/")
            if value.startswith(":") and value[1:].isdigit():
                return f"127.0.0.1{value}"
            if value.isdigit():
                return f"127.0.0.1:{value}"
            if ":" in value and not value.startswith(":"):
                return value
        port = _first(payload, [
            ("debuggingPort",), ("debugging_port",), ("debug_port",), ("port",),
        ])
        if port:
            port = str(port).strip()
            if port.startswith(":"):
                port = port[1:]
            if port.isdigit():
                return f"127.0.0.1:{port}"
        return None
