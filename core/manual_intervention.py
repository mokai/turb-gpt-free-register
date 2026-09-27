# -*- coding: utf-8 -*-
"""通用「需要人工处理」通道（WebUI 弹框提醒 + 定时复检）。

与 :mod:`core.manual_otp` 的区别：

- ``manual_otp``：需要用户**输入一个值**（邮箱验证码），提交后调用方拿到值继续。
- ``manual_intervention``：需要用户在**指纹浏览器窗口里手动操作**（典型场景是
  Cloudflare 风控页的“Verify you are human”验证框，跨域 iframe 里，脚本无法代点）。
  本模块只负责：
    1. 把这条请求登记给 WebUI（弹框 + 浏览器提醒 + 标题闪烁）；
    2. 由调用方传入 ``probe``，按 ``poll_interval`` **定时复检页面是否恢复正常**；
    3. 把每次复检结果回写到请求上，前端实时展示（第 N 次复检 / 仍是验证页 / 已恢复）。

用法::

    from core.manual_intervention import request_manual_intervention

    result = request_manual_intervention(
        kind="cloudflare_challenge",
        title="需要你手动完成 Cloudflare 验证",
        detail="请在浏览器窗口里点击验证框…",
        url=page_url,
        email=email,
        job_id=job_id,
        task="注册",
        timeout=600,
        poll_interval=5,
        probe=lambda: probe_page(driver),   # -> {"recovered": bool, "status_text": str, "detail": str}
    )

``probe`` 返回 ``recovered=True`` 表示页面已恢复正常，本函数立即返回；
用户点「取消本次任务」会抛 :class:`ManualInterventionCancelled`；
等待超过 ``timeout`` 会抛 :class:`ManualInterventionTimeout`；
任务被用户停止（``registration_service.check_stop_requested``）时抛 ``StopRequested``。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 状态机：waiting（等待人工处理）→ checking（复检中）→ recovered/timeout/cancelled
STATUS_WAITING = "waiting"
STATUS_CHECKING = "checking"
STATUS_RECOVERED = "recovered"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"

# 已结束（recovered/cancelled/timeout）的请求在前端多停留几秒，让用户看到最终提示后自动消失。
_RESOLVED_GRACE_SECONDS = 10
# 复检最小间隔，防止把浏览器问爆。
_MIN_POLL_INTERVAL = 1.0

_kind_labels = {
    "cloudflare_challenge": "Cloudflare 风控验证",
}

_lock = threading.RLock()
# request_id -> {"meta": dict, "event": threading.Event}
_entries: dict[str, dict[str, Any]] = {}


class ManualInterventionCancelled(RuntimeError):
    """等待侧被显式取消（用户点了「取消本次任务」）。"""


class ManualInterventionTimeout(TimeoutError):
    """等待人工处理超时。"""


def kind_label(kind: str) -> str:
    return _kind_labels.get(str(kind or "").strip(), str(kind or "").strip() or "人工处理")


def _now() -> float:
    return time.time()


def _entry(request_id: str) -> dict[str, Any] | None:
    with _lock:
        return _entries.get(str(request_id or "").strip())


def _set_meta(meta: dict, **fields) -> None:
    with _lock:
        meta.update(fields)


def _view(meta: dict, now: float | None = None) -> dict:
    """构造给前端的只读视图（含剩余秒数 / 已等待秒数 / 是否已过期）。"""
    item = dict(meta)
    now = _now() if now is None else now
    deadline = item.get("deadline")
    if deadline:
        item["remaining"] = max(0, int(float(deadline) - now))
        item["expired"] = float(deadline) <= now
    else:
        item["remaining"] = None
        item["expired"] = False
    item["waiting_seconds"] = int(now - float(item.get("since") or now))
    item["kind_label"] = kind_label(item.get("kind"))
    return item


def _purge_resolved(now: float | None = None) -> None:
    now = _now() if now is None else now
    with _lock:
        for rid, entry in list(_entries.items()):
            resolved_at = entry["meta"].get("resolved_at")
            if resolved_at and now - float(resolved_at) > _RESOLVED_GRACE_SECONDS:
                _entries.pop(rid, None)


def mark_pending(
    *,
    kind: str,
    title: str,
    detail: str = "",
    guide: str = "",
    status_text: str = "",
    email: str | None = None,
    job_id: int | None = None,
    task: str = "",
    url: str = "",
    timeout: int | None = None,
    poll_interval: int | None = None,
    extra: dict | None = None,
) -> dict:
    """登记一条「等待人工处理」的请求，供 WebUI 轮询展示。

    ``guide`` 是给用户的固定操作指引；``detail`` 会被 ``wait_for_recovery`` 每轮
    复检时覆盖成"当前页面信息"，两者不要混用。
    """
    now = _now()
    request_id = uuid.uuid4().hex
    meta: dict[str, Any] = {
        "request_id": request_id,
        "kind": str(kind or "manual"),
        "kind_label": kind_label(kind),
        "title": title or "需要人工处理",
        "detail": detail or "",
        "guide": guide or "",
        "status": STATUS_WAITING,
        "status_text": status_text or "等待你手动处理…",
        "email": str(email or ""),
        "job_id": int(job_id) if job_id else None,
        "task": str(task or ""),
        "url": str(url or ""),
        "since": now,
        "timeout": int(timeout) if timeout else None,
        "deadline": (now + int(timeout)) if timeout else None,
        "poll_interval": int(poll_interval) if poll_interval else None,
        "checks": 0,
        "last_check_at": None,
        "user_ack_at": None,
        "resolved_at": None,
        "cancelled": False,
        "cancel_reason": "",
    }
    if extra:
        meta["extra"] = dict(extra)
    with _lock:
        _entries[request_id] = {"meta": meta, "event": threading.Event()}
    logger.warning(
        "[人工介入] 需要用户手动处理：kind=%s 标题=%s 邮箱=%s 任务=%s 页面=%s timeout=%ss",
        meta["kind"], meta["title"], meta["email"] or "-", meta["job_id"] or "-",
        meta["url"] or "-", meta["timeout"] or "-",
    )
    return _view(meta, now)


def list_pending() -> list[dict]:
    """列出当前所有等待人工处理的请求（含剩余秒数），供 WebUI 轮询。"""
    _purge_resolved()
    with _lock:
        items = [_view(dict(entry["meta"])) for entry in _entries.values()]
    items.sort(key=lambda x: float(x.get("since") or 0))
    return items


def get_pending(request_id: str) -> dict | None:
    entry = _entry(request_id)
    if not entry:
        return None
    return _view(dict(entry["meta"]))


def active_request(*, kind: str | None = None, job_id: int | None = None, email: str | None = None) -> dict | None:
    """查找仍在等待中的同类请求（用于去重，避免同一个风控页反复弹框）。"""
    _purge_resolved()
    mail = str(email or "").strip().lower()
    with _lock:
        for entry in _entries.values():
            meta = entry["meta"]
            if meta.get("resolved_at") or meta.get("cancelled"):
                continue
            if kind and str(meta.get("kind")) != str(kind):
                continue
            if job_id and meta.get("job_id") and int(meta["job_id"]) != int(job_id):
                continue
            if mail and str(meta.get("email") or "").strip().lower() not in ("", mail):
                continue
            return _view(dict(meta))
    return None


def note_status(
    request_id: str,
    *,
    status: str | None = None,
    status_text: str | None = None,
    detail: str | None = None,
    checks: int | None = None,
) -> dict | None:
    """更新请求的实时状态（复检进度），并唤醒等待侧立即复检一次。"""
    entry = _entry(request_id)
    if not entry:
        return None
    fields: dict[str, Any] = {}
    if status is not None:
        fields["status"] = status
    if status_text is not None:
        fields["status_text"] = status_text
    if detail is not None:
        fields["detail"] = detail
    if checks is not None:
        fields["checks"] = int(checks)
    fields["last_check_at"] = _now()
    if status in (STATUS_RECOVERED, STATUS_TIMEOUT, STATUS_CANCELLED):
        # 终态：标记结束时间，前端展示完 grace 秒后自动消失（不必等下一次轮询调用方清理）。
        fields.setdefault("resolved_at", _now())
    _set_meta(entry["meta"], **fields)
    entry["event"].set()
    return _view(dict(entry["meta"]))


def ack_user_done(request_id: str) -> dict:
    """用户在弹框里点「我已处理完成」：标记确认，并立即触发一次复检。"""
    entry = _entry(request_id)
    if not entry:
        raise ValueError("该人工处理请求已结束，请刷新页面")
    meta = entry["meta"]
    if meta.get("resolved_at") or meta.get("cancelled"):
        raise ValueError("该人工处理请求已结束，无需再次确认")
    _set_meta(meta, user_ack_at=_now(), status_text="已收到你的确认，正在复检页面…")
    entry["event"].set()
    logger.info("[人工介入] 用户标记已处理完成，立即复检：request_id=%s", request_id)
    return _view(dict(meta))


def cancel_request(request_id: str, reason: str | None = None) -> dict:
    """取消等待：等待侧抛 :class:`ManualInterventionCancelled`，对应任务立即失败。"""
    entry = _entry(request_id)
    if not entry:
        raise ValueError("该人工处理请求已结束，请刷新页面")
    meta = entry["meta"]
    if meta.get("resolved_at"):
        raise ValueError("该人工处理请求已结束，无需取消")
    _set_meta(
        meta,
        cancelled=True,
        cancel_reason=str(reason or "用户取消人工处理"),
        status=STATUS_CANCELLED,
        status_text=str(reason or "用户取消人工处理"),
    )
    entry["event"].set()
    logger.warning("[人工介入] 用户取消人工处理：request_id=%s reason=%s", request_id, reason or "-")
    return _view(dict(meta))


def _check_stop_requested() -> None:
    """支持任务被手动停止：等待期间定期检查停止信号。"""
    try:
        from core.registration_service import check_stop_requested

        check_stop_requested()
    except ImportError:
        return


def wait_for_recovery(
    request_id: str,
    probe: Callable[[], dict | None],
    *,
    timeout: int,
    poll_interval: int | None = None,
) -> dict:
    """定时调用 ``probe`` 复检页面，直到恢复 / 用户取消 / 超时。

    ``probe()`` 需返回 ``{"recovered": bool, "status_text": str, "detail": str}``。
    probe 抛异常不会中断等待，只记一次「复检失败」，下一轮继续。
    """
    entry = _entry(request_id)
    if not entry:
        raise ValueError("人工处理请求不存在或已结束")

    meta = entry["meta"]
    event: threading.Event = entry["event"]
    interval = max(_MIN_POLL_INTERVAL, float(poll_interval or meta.get("poll_interval") or 5))
    total_timeout = max(5, int(timeout or meta.get("timeout") or 600))
    end = _now() + total_timeout
    checks = int(meta.get("checks") or 0)
    started_at = _now()

    while True:
        entry = _entry(request_id)
        if entry is None:
            raise ManualInterventionCancelled("人工处理请求已被移除")
        meta, event = entry["meta"], entry["event"]

        if meta.get("cancelled"):
            raise ManualInterventionCancelled(
                "已取消人工处理：" + str(meta.get("cancel_reason") or "用户取消")
            )

        checks += 1
        _set_meta(meta, status=STATUS_CHECKING, checks=checks, last_check_at=_now(),
                  status_text="正在复检页面（第 %s 次）…" % checks)
        try:
            result = probe() or {}
        except Exception as exc:  # probe 失败不能拖垮等待
            result = {"recovered": False, "detail": "复检失败：%s: %s" % (type(exc).__name__, str(exc)[:200])}

        if result.get("recovered"):
            resolved_at = _now()
            _set_meta(
                meta,
                status=STATUS_RECOVERED,
                status_text=str(result.get("status_text") or "页面已恢复正常，流程继续"),
                detail=str(result.get("detail") or meta.get("detail") or ""),
                resolved_at=resolved_at,
                checks=checks,
                last_check_at=resolved_at,
            )
            event.set()
            logger.info(
                "[人工介入] 页面已恢复：request_id=%s 复检次数=%s 等待=%.1fs",
                request_id, checks, resolved_at - started_at,
            )
            return _view(dict(meta))

        _set_meta(
            meta,
            status=STATUS_WAITING,
            status_text=str(result.get("status_text") or "页面仍是风控页，等待你手动处理…"),
            detail=str(result.get("detail") or meta.get("detail") or ""),
            checks=checks,
            last_check_at=_now(),
        )

        remaining = end - _now()
        if remaining <= 0:
            resolved_at = _now()
            _set_meta(
                meta,
                status=STATUS_TIMEOUT,
                status_text="等待人工处理超时（%ss）" % total_timeout,
                resolved_at=resolved_at,
            )
            event.set()
            raise ManualInterventionTimeout(
                "等待人工处理超时（%ss）：%s" % (total_timeout, meta.get("title") or "")
            )

        # 用户在弹框里的任何动作（确认/取消）都会 set 这个事件，立即进入下一轮复检。
        event.clear()
        event.wait(timeout=min(interval, max(0.5, remaining)))
        _check_stop_requested()


def request_manual_intervention(
    *,
    probe: Callable[[], dict | None],
    timeout: int = 600,
    poll_interval: int | None = None,
    **meta_fields,
) -> dict:
    """登记请求 → 定时复检 → 自动清理，返回最终状态视图。"""
    meta = mark_pending(timeout=timeout, poll_interval=poll_interval, **meta_fields)
    request_id = meta["request_id"]
    try:
        return wait_for_recovery(request_id, probe, timeout=timeout, poll_interval=poll_interval)
    finally:
        entry = _entry(request_id)
        if entry is not None:
            with _lock:
                entry["meta"].setdefault("resolved_at", _now())
            entry["event"].set()
            _purge_resolved()


def clear_pending(request_id: str) -> None:
    """立即移除一条请求（任务异常退出时收尾用）。"""
    with _lock:
        _entries.pop(str(request_id or "").strip(), None)
