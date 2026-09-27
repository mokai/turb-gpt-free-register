# -*- coding: utf-8 -*-
"""注册任务失败后的手动查活兜底。

背景：注册流程在「已点击资料页提交按钮，等待 OAuth 跳转」之后失败（典型报错
"等待 /api/auth/session accessToken 超时"）时，账号在 OpenAI 侧大概率已创建
成功，但本地尚未写入账号列表。这里提供按任务发起的手动查活：
    - 校验任务是否满足手动查活条件（failed + 走完资料页提交 + 没有本地账号）
    - 复用 check_account_liveness 的完整登录链（无注册密码时自动走邮箱 OTP）
    - 查活成功才把账号写入账号列表（save_account_data），并回填任务 account_id

日志复用 account_liveness.log_path(email)，即 logs/live-check-<email>.log，
前端可直接用现有 /api/accounts/live-check-log 轮询。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path

logger = logging.getLogger(__name__)

# 注册日志里资料页提交成功的标志行。
PROFILE_SUBMIT_MARKER = "已点击资料页提交按钮，等待 OAuth 跳转"
# 该步骤之后最典型的失败报错（_fetch_chatgpt_session 超时）。
OAUTH_SESSION_TIMEOUT_KEY = "等待 /api/auth/session accessToken 超时"

_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def is_running(email: str) -> bool:
    with _LOCK:
        return str(email or "").strip().lower() in _RUNNING


def _append_check_log(email: str, line: str) -> None:
    try:
        p = log_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} [INFO] [手动查活] {line}\n")
    except Exception:
        logger.debug("[手动查活] 写日志失败: email=%s", email, exc_info=True)


def _log_contains_marker(log_file: str | None, tail_bytes: int = 120_000) -> bool:
    """任务日志尾部是否包含资料页提交成功的标志行。"""
    if not log_file:
        return False
    try:
        p = Path(log_file)
        if not p.exists():
            return False
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read()
        return PROFILE_SUBMIT_MARKER in data.decode("utf-8", errors="replace")
    except Exception:
        return False


def eligible_for_manual_live_check(job: dict | None) -> bool:
    """判断失败任务是否可以发起手动查活。

    条件（全部满足）：
      1. 任务状态为 failed；
      2. 任务有邮箱；
      3. 本地还没有该邮箱的账号记录（有账号就直接走账号列表的查活）；
      4. 以下任一成立：
         a. payload 里有 manual_live_check_ready=True（显式标记）；
         b. error_message 命中 OAuth session 超时（只可能在资料页提交后发生）；
         c. 任务日志尾部包含资料页提交标志行。
    """
    if not job:
        return False
    if str(job.get("status") or "") != "failed":
        return False
    email = str(job.get("email") or "").strip()
    if not email:
        return False
    try:
        if db.get_account_by_email(email) is not None:
            return False
    except Exception:
        return False

    payload_ready = bool(job.get("manual_live_check_ready"))
    error_text = str(job.get("error_message") or "")
    if payload_ready or OAUTH_SESSION_TIMEOUT_KEY in error_text:
        return True
    return _log_contains_marker(job.get("log_file"))


def start_job_live_check(job_id: int) -> dict:
    """为失败任务发起后台手动查活；成功后账号写入账号列表。"""
    job = db.get_job(int(job_id))
    if not job:
        return {"accepted": False, "error": "任务不存在"}
    if not eligible_for_manual_live_check(job):
        return {"accepted": False, "error": "该任务不满足手动查活条件（需为已失败、无本地账号、且已走过资料页提交的任务）"}

    email = str(job.get("email") or "").strip()
    with _LOCK:
        if email.lower() in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该邮箱已在手动查活中"}

    with _LOCK:
        _RUNNING.add(email.lower())

    def _finish() -> None:
        with _LOCK:
            _RUNNING.discard(email.lower())

    # 直接启动线程（保持与项目其它后台任务一致的 daemon 模式）
    t = threading.Thread(
        target=_run_manual_check,
        args=(int(job_id), email, _finish),
        name=f"manual-live-check-{job_id}",
        daemon=True,
    )
    t.start()

    _append_check_log(email, f"已启动手动查活：任务 #{job_id}，查活成功后将写入账号列表")
    return {"accepted": True, "job_id": int(job_id), "email": email, "status": "queued"}


def _run_manual_check(job_id: int, email: str, finish_fn) -> None:
    try:
        _append_check_log(email, "开始重新登录验证账号是否存活（无注册密码时自动走邮箱 OTP）")
        result = check_account_liveness(email, proxy=None, clear_log=False, email_source=None)
        ok = bool(result.get("ok"))
        if not ok:
            _append_check_log(
                email,
                f"手动查活失败：{result.get('status') or 'failed'} {result.get('error') or ''}",
            )
            return

        access_token = str(result.get("access_token") or "")
        session_info = result.get("session") or {}
        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        # 与注册成功路径保持一致：save_account_data 会写 SQLite、恢复 Remail
        # 上下文（进程内没有时靠池记录的 code_url 兜底）并按配置触发自动 2FA/查套餐。
        from core.account_export import save_account_data

        row_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=None,
            email_source=_resolve_source(email),
            proxy_used=result.get("proxy_used"),
            extra={
                "user": user,
                "account": account,
                "expires": session_info.get("expires"),
                "note": "注册失败（OAuth 跳转超时）后通过手动查活找回",
                "manual_live_check": {
                    "job_id": job_id,
                    "recovered_at": datetime.now().isoformat(timespec="seconds"),
                },
            },
        )
        db.update_job(job_id, account_id=row_id)
        db.update_job_payload_fields(job_id, {"manual_live_check_done": True})
        _mark_email_used(email)
        _append_check_log(
            email,
            f"手动查活成功：user_id={user.get('id') or '-'} plan={account.get('planType') or '-'}，"
            f"已写入账号列表（account_id={row_id}）",
        )
        logger.info("[手动查活] 成功: job=%s email=%s account_id=%s", job_id, email, row_id)
    except Exception as exc:
        logger.exception("[手动查活] 后台异常: %s", email)
        _append_check_log(email, f"手动查活后台异常：{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        try:
            finish_fn()
        except Exception:
            pass


def _resolve_source(email: str) -> str | None:
    try:
        from core.email_provider import resolve_email_source

        return resolve_email_source(email)
    except Exception:
        return None


def _mark_email_used(email: str) -> None:
    """查活成功后把邮箱池条目标为 used，避免被再次领取注册。"""
    try:
        from core.email_provider import release_email

        release_email(email, status="used", note="注册失败后手动查活成功，账号已加入账号列表")
    except Exception:
        logger.debug("[手动查活] 标记邮箱池 used 失败: %s", email, exc_info=True)
