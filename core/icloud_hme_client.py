# -*- coding: utf-8 -*-
"""iCloud HME 邮箱客户端，对接 icloud-hme 聚合平台的开放 API。

与 Remail 的本质差别是「谁持有邮箱池」：

* Remail  —— 每次注册都**下单购买**一个邮箱，订单凭证（service token）必须由
  本地保存，失败回收靠把邮箱写回本地「通用 API 邮箱池」；
* iCloud HME —— 平台自己维护 HME 别名池，并按 **项目（project）** 维度记录占用。
  所以本地不需要池记录：**取邮箱＝预留，回收＝释放占用**。

调用契约（取自 icloud-hme 的 ``web_ui.py`` 与 ``docs/api-usage.md``）：

1. 取邮箱
   ``GET /api/v1/hme/available/next?project_code=X``
   服务端原子预留第一个可用别名（``project_store.reserve_first``），响应 ``item``
   即领取结果；没有可用别名时 ``item`` 为 ``null``。

2. 取验证码
   ``GET /api/v1/mailboxes/{alias}/messages?project_code=X&limit=1&force=1``
   响应根级 ``verification_code`` 已经是服务端解析好的验证码；``messages[0]``
   里同时带 ``subject`` / ``body_preview`` / ``date``，可用于时间过滤与兜底解析。

3. 回收
   ``POST /api/v1/hme/{alias}/release?project_code=X``
   **幂等**：移除该项目的占用标记，别名回到项目可用池；重复调用返回
   ``released: false``，且不影响其它项目对该邮箱的占用记录。

鉴权统一为 ``Authorization: Bearer <ICLOUD_HME_API_KEY>``；所有 ``/api/v1/*``
接口都要求 ``project_code`` 查询参数，且该 code 必须已在 icloud-hme 侧创建
（否则返回 HTTP 400 ``项目 code 不存在``）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from config import email as _email_cfg
from core.otp_utils import extract_code

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:5050"
REQUEST_TIMEOUT = 20

#: ChatGPT（OpenAI）验证码固定 6 位。
#:
#: 平台侧为了兼容其它项目用 4-8 位宽松匹配，会把「通知邮件」里的噪声当成验证码：
#: 邮件页脚的年份 ``© 2026``、跟踪链接里的 ``http://url3243.email.openai.com/...``。
#: 本客户端只服务 OpenAI 账号，因此只认 6 位。
#: 不能用 \b：验证码紧贴 Unicode 字母时字母与数字同属 \w，\b 不成立。
_OTP_LENGTH = 6
_CODE_RE = re.compile(r"(?<!\d)(\d{%d})(?!\d)" % _OTP_LENGTH)


def _accept_code(value) -> str:
    """只在整串恰好是 6 位数字时认账，其余一律丢弃并返回空串。

    实测故障（2026-10-05，账号 #58 换绑）：验证码邮件之外的 OpenAI 通知邮件会被
    解析出 4 位数字（``Your multi-factor authentication settings have been
    changed`` → 跟踪链接里的 ``3243``；``New sign-in to your OpenAI account`` →
    页脚年份 ``2026``）。提交给服务端只拿到 400 ``string_below_min_length``
    （expected minimum length 6, got 4），白白浪费三次 OTP 尝试。
    """
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{%d}" % _OTP_LENGTH, text) else ""


class IcloudHmeError(RuntimeError):
    """iCloud HME API 请求、领取或取码失败。"""


# 兼容调用方可能使用的命名。
IcloudHmeClientError = IcloudHmeError


@dataclass
class IcloudHmeAccount:
    """一次 iCloud HME 领取的上下文。"""

    email: str
    project_code: str
    account_id: str = ""
    label: str = ""
    account_name: str = ""


_CONTEXT_CACHE: dict[str, IcloudHmeAccount] = {}
_CONTEXT_LOCK = threading.RLock()


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url(value: str | None = None) -> str:
    """返回 API 根地址，容忍误填 ``/api``、``/api/v1`` 或文档路径。"""
    raw = str(
        value
        if value is not None
        else getattr(_email_cfg, "ICLOUD_HME_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE
    ).strip()
    if not raw:
        raw = DEFAULT_API_BASE
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "http://" + raw

    parsed = urlsplit(raw.rstrip("/"))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise IcloudHmeError(
            "iCloud HME API 地址无效，请填写 http://127.0.0.1:5050（不要填写接口路径）"
        )

    path = parsed.path.rstrip("/")
    # 用户可能直接粘贴管理端/文档地址，逐级回退到域名根。
    lowered = path.lower()
    for suffix in ("/api/v1", "/api", "/docs"):
        if lowered.endswith(suffix):
            path = path[: -len(suffix)]
            lowered = path.lower()
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")).rstrip("/")


def _request_timeout() -> int:
    try:
        value = int(
            getattr(_email_cfg, "ICLOUD_HME_REQUEST_TIMEOUT", REQUEST_TIMEOUT) or REQUEST_TIMEOUT
        )
    except (TypeError, ValueError):
        value = REQUEST_TIMEOUT
    return max(1, min(120, value))


def _api_key() -> str:
    value = str(getattr(_email_cfg, "ICLOUD_HME_API_KEY", "") or "").strip()
    if not value:
        raise IcloudHmeError(
            "iCloud HME API Key 未配置，请在 配置 → 邮箱 / OTP 填写 ICLOUD_HME_API_KEY"
        )
    return value


def _project_code() -> str:
    value = str(getattr(_email_cfg, "ICLOUD_HME_PROJECT_CODE", "") or "").strip()
    if not value:
        raise IcloudHmeError(
            "iCloud HME 项目 code 未配置，请在 配置 → 邮箱 / OTP 填写 ICLOUD_HME_PROJECT_CODE"
        )
    return value


def _release_enabled() -> bool:
    return bool(getattr(_email_cfg, "ICLOUD_HME_RELEASE_ON_FAILED", True))


def _error_message(payload, response) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        if message:
            return str(message)
    text = str(getattr(response, "text", "") or "").strip()
    return text[:240] if text else "服务端未返回错误信息"


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
):
    """调用 icloud-hme 的 ``/api/v1/*`` 接口并返回 JSON payload。

    该平台在 ``HTTP 200`` 下也可能带 ``{"ok": false, "error": ...}``，
    因此除了状态码，还要检查 ``ok`` 字段。
    """
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_api_key()}",
    }
    if headers:
        request_headers.update(headers)

    url = _base_url() + (path if str(path).startswith("/") else f"/{path}")
    try:
        response = requests.request(
            method.upper(),
            url,
            params=params,
            json=json_body,
            headers=request_headers,
            timeout=_request_timeout(),
        )
    except requests.RequestException as exc:
        raise IcloudHmeError(
            f"iCloud HME 请求失败 ({method.upper()} {path}): {type(exc).__name__}: {exc}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise IcloudHmeError(
                f"iCloud HME 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
                f"{_error_message(None, response)}"
            ) from exc
        raise IcloudHmeError(f"iCloud HME 响应不是 JSON ({method.upper()} {path})") from exc

    if response.status_code >= 400:
        if response.status_code == 401:
            raise IcloudHmeError("iCloud HME API Key 无效或已吊销")
        raise IcloudHmeError(
            f"iCloud HME 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
            f"{_error_message(payload, response)}"
        )

    if isinstance(payload, dict) and payload.get("ok") is False:
        raise IcloudHmeError(
            f"iCloud HME 返回失败 ({method.upper()} {path}): {_error_message(payload, response)}"
        )
    return payload


def _cache_context(account: IcloudHmeAccount) -> IcloudHmeAccount:
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE[_cache_key(account.email)] = account
    return account


def get_account_context(email: str) -> IcloudHmeAccount | None:
    with _CONTEXT_LOCK:
        return _CONTEXT_CACHE.get(_cache_key(email))


def _parse_timestamp(raw) -> float | None:
    """把平台的 ``date`` 字段解析成时间戳，解析不了返回 None。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 10_000_000_000 else value

    text = str(raw).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        value = float(text)
        return value / 1000.0 if value > 10_000_000_000 else value
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _first_value(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _message_code(message) -> str | None:
    """优先读服务端已解析好的验证码字段，缺失时才回退解析邮件正文。"""
    if not isinstance(message, dict):
        return None
    direct = _first_value(message, "verification_code", "verificationCode", "otp_code", "code")
    if direct:
        match = _CODE_RE.search(str(direct))
        if match:
            return match.group(1)

    text = "\n".join(
        str(message.get(key) or "")
        for key in ("subject", "body_preview", "body", "text")
    )
    if not text.strip():
        return None
    # 平台返回的是纯文本预览；extract_code 同时兼容完整 HTML 与 dict 入参。
    # 本地兜底同样是 4-8 位宽松匹配，必须再过一遍 6 位校验，否则通知邮件里的
    # 年份/跟踪链接编号会被当成验证码。
    return _accept_code(
        extract_code(
            {
                "from": str(message.get("from") or ""),
                "subject": str(message.get("subject") or ""),
                "text": text,
                "content": text,
            }
        )
    ) or None


def _payload_code(payload, after_ts: float | None) -> tuple[str | None, float | None]:
    """从取码响应里挑出最新的验证码，返回 ``(code, timestamp)``。"""
    if not isinstance(payload, dict):
        return None, None

    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        best_code, best_ts = None, float("-inf")
        for message in messages:
            if not isinstance(message, dict):
                continue
            timestamp = _parse_timestamp(
                _first_value(message, "date", "received_at", "receivedAt", "created_at")
            )
            if after_ts is not None and timestamp is not None and timestamp < after_ts - 30:
                continue
            code = _message_code(message)
            if not code:
                continue
            candidate = float("-inf") if timestamp is None else timestamp
            if candidate >= best_ts:
                best_code, best_ts = code, candidate
        if best_code:
            return best_code, None if best_ts == float("-inf") else best_ts

        # 有邮件列表但一封都没通过 after_ts（或都没解析出验证码）时**必须直接放弃**：
        # 根级 ``verification_code`` 是服务端对 ``messages[0]`` 的投影，属于同一批
        # 旧邮件，用它兜底等于绕过时间过滤。
        # 实测故障（2026-10-05，账号 #58 换绑）：after_ts 之后的新验证码邮件还没到，
        # 根级字段把 20 小时前那封「MFA 设置已变更」通知里的跟踪链接编号 "3243"
        # 当成验证码立刻返回，三次重试全部原样提交 → 400 长度不足，任务白跑。
        return None, None

    # 只有平台这一轮确实一封邮件都没返回时，根级字段才值得一用。
    root_code = _first_value(payload, "verification_code", "otp_code", "code")
    if root_code:
        match = _CODE_RE.search(str(root_code))
        if match:
            return match.group(1), None
    return None, None


def pick_account() -> IcloudHmeAccount:
    """预留一个可用 HME 邮箱并返回领取上下文。

    服务端在 ``available/next`` 内做原子预留，因此并发任务不会拿到同一个别名；
    但**预留在领取那一刻就已生效**，所以任务失败必须调用 :func:`release_account`
    把别名放回项目可用池，否则它会一直停留在 used 状态。
    """
    project_code = _project_code()
    payload = _request(
        "GET",
        "/api/v1/hme/available/next",
        params={"project_code": project_code},
    )
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        raise IcloudHmeError(
            f"iCloud HME 没有可用邮箱（project_code={project_code}）；"
            "请确认 icloud-hme 侧有 active 账号、邮箱分组为「可用」，"
            "且该邮箱未被本项目占用"
        )

    email = str(_first_value(item, "alias_email", "hme", "email") or "").strip()
    if not email or "@" not in email:
        raise IcloudHmeError(f"iCloud HME 返回的邮箱地址无效: {item!r}")

    account = IcloudHmeAccount(
        email=email,
        project_code=project_code,
        account_id=str(_first_value(item, "account_id", "accountId") or ""),
        label=str(item.get("label") or ""),
        account_name=str(item.get("account_name") or ""),
    )
    _cache_context(account)
    logger.info(
        "[iCloudHME] 已预留邮箱: %s project=%s account=%s",
        email,
        project_code,
        account.account_id or "-",
    )
    return account


def get_email() -> str:
    """兼容其他临时邮箱客户端的旧入口。"""
    return pick_account().email


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询 iCloud HME 邮箱，返回 ``after_ts`` 之后最新的验证码。

    与 Remail 不同，这里不需要 service token：取码只依赖 ``project_code`` +
    API Key，因此进程重启后也能直接继续取码。
    """
    target = str(email or "").strip()
    if not target:
        raise IcloudHmeError("iCloud HME 取码缺少邮箱地址")

    account = get_account_context(target)
    project_code = str(getattr(account, "project_code", "") or "").strip() or _project_code()

    try:
        wait_seconds = int(
            max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90)
        )
    except (TypeError, ValueError):
        wait_seconds = 90
    try:
        interval = int(
            poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3)
        )
    except (TypeError, ValueError):
        interval = 3
    try:
        settle = int(
            settle_seconds
            if settle_seconds is not None
            else getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5)
        )
    except (TypeError, ValueError):
        settle = 5
    interval = max(1, interval)
    settle = max(0, settle)

    quoted = quote(target, safe="")
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的验证码"

    logger.info("[iCloudHME] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        try:
            # force=1 强制刷新：邮箱侧缓存可能还停留在上一封邮件。
            payload = _request(
                "GET",
                f"/api/v1/mailboxes/{quoted}/messages",
                params={"project_code": project_code, "limit": 1, "force": 1},
            )
            code, timestamp = _payload_code(payload, after_ts)
            if code:
                candidate_time = float("-inf") if timestamp is None else timestamp
                if (
                    best_otp is None
                    or candidate_time > best_timestamp
                    or (candidate_time == best_timestamp and code != best_otp)
                ):
                    best_otp = code
                    best_timestamp = candidate_time
                    settle_until = time.monotonic() + settle
                    logger.info("[iCloudHME] 锁定 OTP 候选，等待 %ss 确认", settle)

            if best_otp and settle_until is not None and time.monotonic() >= settle_until:
                return best_otp
        except IcloudHmeError as exc:
            last_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 轮询期间任何异常都不应中断等待
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise IcloudHmeError(f"等待 iCloud HME 验证码超时: {target}; {last_error}")


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    """把邮箱放回本项目的可用池，返回是否确实完成了释放。

    只在 ``status == "available"``（注册失败但邮箱还没被 OpenAI 消耗）时真正调用
    平台的 release 接口；``failed`` / ``disabled`` / ``used`` 等状态仅清理进程内
    上下文——邮箱已被消耗，放回去只会让下次注册必然失败。

    release 是幂等的，且平台按项目独立记录占用，所以不会影响其它项目。
    与其它客户端的 ``release_account``（返回 ``None``）不同，这里返回布尔值，
    因为换绑释放路径需要据此判断是否真的通知到了平台。
    """
    target = str(email or "").strip()
    with _CONTEXT_LOCK:
        account = _CONTEXT_CACHE.pop(_cache_key(target), None)

    if status != "available":
        if account:
            logger.info(
                "[iCloudHME] 已释放取件上下文（不回收）: %s status=%s note=%s",
                target,
                status,
                note or "",
            )
        return False

    if not target:
        return False
    if not _release_enabled():
        logger.info("[iCloudHME] 回收开关已关闭，跳过释放: %s", target)
        return False

    try:
        project_code = str(getattr(account, "project_code", "") or "").strip() or _project_code()
    except IcloudHmeError as exc:
        logger.warning("[iCloudHME] 无法确定 project_code，跳过释放: %s %s", target, exc)
        return False

    try:
        payload = _request(
            "POST",
            f"/api/v1/hme/{quote(target, safe='')}/release",
            params={"project_code": project_code},
        )
    except IcloudHmeError as exc:
        logger.warning("[iCloudHME] 释放邮箱失败: %s %s", target, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - 回收失败不应影响注册主流程
        logger.warning("[iCloudHME] 释放邮箱异常: %s %s: %s", target, type(exc).__name__, exc)
        return False

    released = bool((payload or {}).get("released")) if isinstance(payload, dict) else False
    logger.info(
        "[iCloudHME] 邮箱已%s: %s project=%s note=%s",
        "放回可用池" if released else "确认处于可用状态（此前未被占用）",
        target,
        project_code,
        note or "",
    )
    return True


def list_projects() -> list[dict]:
    """查询平台已配置的项目，供配置页/诊断使用。"""
    payload = _request("GET", "/api/v1/projects")
    projects = payload.get("projects") if isinstance(payload, dict) else None
    return [item for item in projects if isinstance(item, dict)] if isinstance(projects, list) else []


def fetch_config() -> dict:
    """读取平台能力声明，用于连通性自检。"""
    payload = _request("GET", "/api/v1/config")
    return payload if isinstance(payload, dict) else {}
