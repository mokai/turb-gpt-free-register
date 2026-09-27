# -*- coding: utf-8 -*-
"""人工输入通道（WebUI 弹框 / CLI 终端），供自动取码失败时兜底。

设计目标
- 任何后台步骤（注册、查活、换绑、Codex 授权…）在自动取不到验证码时，都调用
  :func:`request_manual_otp` 挂起等待人工输入，而不是直接判失败。
- WebUI 通过 :func:`list_pending` 轮询待办，弹出输入框并调用
  :func:`submit_manual_otp` 提交；提交后等待侧立即返回，流程继续。
- 请求携带上下文元数据（邮箱、取码地址、用途、所属任务、超时倒计时），
  前端据此展示并做浏览器提醒。

用法::

    from core.manual_otp import request_manual_otp, ManualOtpCancelled

    code = request_manual_otp(
        email, timeout=600, purpose="查活-登录验证码",
        code_url="https://.../pickup?token=st_xxx", task="查活",
    )

兼容旧接口：``wait_for_manual_otp`` / ``mark_waiting`` / ``clear_waiting`` /
``list_waiting`` / ``pop_manual_otp`` 保留原语义。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# email(lower) -> list[code]  支持同一邮箱多次验证码
_codes: dict[str, list[str]] = defaultdict(list)
# email(lower) -> Event
_events: dict[str, threading.Event] = {}
# email(lower) -> 待输入请求元数据
_pending: dict[str, dict[str, Any]] = {}

# 允许的验证码位数（OpenAI 通常 6 位，放宽兼容）
_CODE_LENGTHS = (4, 5, 6, 7, 8)


class ManualOtpCancelled(RuntimeError):
    """等待侧被显式取消（用户点了「取消本次输入」）。"""


class ManualOtpTimeoutError(TimeoutError):
    """等待人工验证码超时。"""


def _norm(email: str) -> str:
    return str(email or "").strip().lower()


def _event_for(email: str) -> threading.Event:
    key = _norm(email)
    ev = _events.get(key)
    if ev is None:
        ev = threading.Event()
        _events[key] = ev
    return ev


def validate_code(code: str) -> str:
    """校验并规范化验证码；不合法抛 ValueError。"""
    cleaned = str(code or "").strip().replace(" ", "").replace("-", "")
    if not cleaned:
        raise ValueError("验证码为空")
    if not cleaned.isdigit() or len(cleaned) not in _CODE_LENGTHS:
        raise ValueError(f"验证码格式看起来不对: {code!r}（应为 4-8 位数字）")
    return cleaned


# ----------------------------------------------------------------------------
# 请求侧：后台步骤调用
# ----------------------------------------------------------------------------

def mark_waiting(
    email: str,
    job_id: int | None = None,
    *,
    purpose: str | None = None,
    title: str | None = None,
    code_url: str | None = None,
    source: str | None = None,
    task: str | None = None,
    timeout: int | None = None,
    request_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    """登记一条「等待人工输入」的请求，供 WebUI 轮询展示。"""
    key = _norm(email)
    now = time.time()
    meta = {
        "request_id": request_id or uuid.uuid4().hex,
        "email": str(email or ""),
        "job_id": job_id,
        "title": title or "需要手动输入验证码",
        "purpose": purpose or "邮箱验证码",
        "code_url": code_url or "",
        "source": source or "",
        "task": task or purpose or "",
        "since": now,
        "timeout": int(timeout) if timeout else None,
        "deadline": (now + int(timeout)) if timeout else None,
    }
    if extra:
        meta["extra"] = dict(extra)
    with _lock:
        _pending[key] = meta
        _event_for(key).clear()
    logger.warning(
        "[ManualOTP] 需要人工输入验证码：email=%s 用途=%s 取码地址=%s timeout=%ss job=%s",
        email, meta["purpose"], meta["code_url"] or "-", meta["timeout"] or "-", job_id or "-",
    )
    return dict(meta)


def clear_waiting(email: str) -> None:
    key = _norm(email)
    with _lock:
        _pending.pop(key, None)


def _pending_view(meta: dict, now: float | None = None) -> dict:
    item = dict(meta)
    deadline = item.get("deadline")
    now = time.time() if now is None else now
    if deadline:
        item["remaining"] = max(0, int(deadline - now))
        item["expired"] = deadline <= now
    else:
        item["remaining"] = None
        item["expired"] = False
    item["waiting_seconds"] = int(now - float(item.get("since") or now))
    return item


def list_pending() -> list[dict]:
    """列出当前所有等待人工输入的请求（含剩余秒数），供 WebUI 轮询。"""
    with _lock:
        return [_pending_view(dict(v)) for v in _pending.values()]


def list_waiting() -> list[dict]:
    """兼容旧接口：等价于 :func:`list_pending`。"""
    return list_pending()


def get_pending(email: str) -> dict | None:
    key = _norm(email)
    with _lock:
        meta = _pending.get(key)
        return _pending_view(dict(meta)) if meta else None


def submit_manual_otp(email: str, code: str, *, request_id: str | None = None) -> dict:
    """提交人工验证码；唤醒等待侧。"""
    key = _norm(email)
    if not key:
        raise ValueError("email 为空")
    cleaned = validate_code(code)
    with _lock:
        meta = _pending.get(key)
        if request_id and meta and meta.get("request_id") and meta["request_id"] != request_id:
            raise ValueError("该验证码请求已失效（可能已被其他弹框处理），请刷新后重试")
        _codes[key].append(cleaned)
        _event_for(key).set()
    logger.info("[ManualOTP] 已提交验证码：email=%s（%s 位）", email, len(cleaned))
    return {"ok": True, "email": email, "code": cleaned}


def cancel_manual_otp(email: str, reason: str | None = None, *, request_id: str | None = None) -> dict:
    """取消等待：等待侧会抛 :class:`ManualOtpCancelled`，让任务快速失败而不是空等。"""
    key = _norm(email)
    if not key:
        raise ValueError("email 为空")
    with _lock:
        meta = _pending.get(key)
        if request_id and meta and meta.get("request_id") and meta["request_id"] != request_id:
            raise ValueError("该验证码请求已失效，请刷新后重试")
        if meta is not None:
            meta["cancelled"] = True
            meta["cancel_reason"] = reason or ""
        else:
            meta = {"email": email, "cancelled": True}
            _pending[key] = meta
        _event_for(key).set()
    logger.warning("[ManualOTP] 已取消人工输入：email=%s reason=%s", email, reason or "-")
    return {"ok": True, "email": email, "cancelled": True}


def pop_manual_otp(email: str) -> str | None:
    key = _norm(email)
    with _lock:
        queue = _codes.get(key) or []
        if not queue:
            return None
        code = queue.pop(0)
        if not queue:
            _event_for(key).clear()
        return code


def _has_tty() -> bool:
    try:
        import sys
        return bool(getattr(sys, "stdin", None) and sys.stdin.isatty())
    except Exception:
        return False


def _read_stdin_line_nonblocking() -> str:
    """非阻塞读取一行终端输入（只在交互式 TTY 下有效），取不到返回空串。

    不能用阻塞 input()：那会让 WebUI 提交的验证码无法及时被消费。
    """
    try:
        import select
        import sys
        stdin = sys.stdin
        if not stdin or not stdin.isatty():
            return ""
        ready, _, _ = select.select([stdin], [], [], 0)
        if not ready:
            return ""
        return (stdin.readline() or "").strip()
    except Exception:
        return ""


def _check_stop_requested() -> None:
    """支持任务被手动停止：等待期间定期检查停止信号。"""
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except Exception:
        pass


def wait_for_manual_otp(
    email: str,
    *,
    timeout: int = 180,
    job_id: int | None = None,
    purpose: str | None = None,
    title: str | None = None,
    code_url: str | None = None,
    source: str | None = None,
    task: str | None = None,
    extra: dict | None = None,
) -> str:
    """阻塞等待人工验证码；优先吃已提交的 code。

    Raises:
        ManualOtpCancelled: 用户取消。
        ManualOtpTimeoutError: 超时未提交。
    """
    key = _norm(email)
    if not key:
        raise RuntimeError("人工验证码：email 为空")

    # 已有预提交验证码 → 直接使用
    existing = pop_manual_otp(email)
    if existing:
        clear_waiting(email)
        return existing

    meta = mark_waiting(
        email, job_id=job_id, purpose=purpose, title=title,
        code_url=code_url, source=source, task=task, timeout=timeout, extra=extra,
    )

    end = time.time() + max(10, int(timeout))
    ev = _event_for(key)
    has_tty = _has_tty()
    try:
        while time.time() < end:
            code = pop_manual_otp(email)
            if code:
                return code

            current = get_pending(email)
            if current and current.get("cancelled"):
                raise ManualOtpCancelled(
                    f"已取消手动输入验证码：{email}"
                    + (f"（{current.get('cancel_reason')}）" if current.get("cancel_reason") else "")
                )

            if has_tty:
                # 交互式 CLI：等 0.5 秒（吸收 WebUI 提交），再非阻塞看一眼终端输入
                ev.wait(timeout=0.5)
                typed = _read_stdin_line_nonblocking()
                if typed:
                    submit_manual_otp(email, typed)
                    code = pop_manual_otp(email)
                    if code:
                        return code
                continue

            ev.wait(timeout=1.0)
            _check_stop_requested()

        raise ManualOtpTimeoutError(
            f"等待人工输入验证码超时（{timeout}s）：{email}；"
            f"用途={meta.get('purpose')}，取码地址={meta.get('code_url') or '未提供'}"
        )
    finally:
        clear_waiting(email)


# ----------------------------------------------------------------------------
# 对外统一入口
# ----------------------------------------------------------------------------

def request_manual_otp(
    email: str,
    *,
    timeout: int = 600,
    job_id: int | None = None,
    purpose: str | None = None,
    title: str | None = None,
    code_url: str | None = None,
    source: str | None = None,
    task: str | None = None,
    extra: dict | None = None,
) -> str:
    """请求人工输入验证码并等待返回（其他步骤统一调用此函数）。"""
    return wait_for_manual_otp(
        email,
        timeout=timeout,
        job_id=job_id,
        purpose=purpose,
        title=title,
        code_url=code_url,
        source=source,
        task=task,
        extra=extra,
    )
