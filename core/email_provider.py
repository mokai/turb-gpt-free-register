# -*- coding: utf-8 -*-
"""
邮箱来源调度层。

EMAIL_SOURCE 支持单个或多个来源：
    "outlook"
    "cloudflare_domain"   # 自有域名 + QQ IMAP
    "cloudflare"          # Cloudflare Worker 临时邮箱
    "generic_api"
    "imap"
    "gptmail"
    "mailnest"
    "cloudmail"
    "remail"
    "outlook,generic_api,mailnest,cloudmail,remail"   # 按顺序兜底
    ["outlook", "generic_api", "mailnest", "cloudmail", "remail"]  # 也兼容列表写法
"""
import logging
import re
from datetime import datetime
from typing import Iterable

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail")


def parse_email_sources(value=None) -> list[str]:
    """把 EMAIL_SOURCE 解析为有序来源列表，去重并过滤空值。"""
    if value is None:
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().strip('"\'')
        if not s:
            continue
        if s not in _VALID_SOURCES:
            logger.warning(f"[EmailProvider] 未知邮箱来源 {s!r}，已忽略")
            continue
        if s not in out:
            out.append(s)
    return out or ["outlook"]


def _pick_from_source(source: str) -> str:
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    if source == "cloudflare":
        from core.cf_temp_mail_client import pick_account
        return pick_account().email
    if source == "cloudflare_domain":
        from core.qqmail_client import pick_domain_email
        return pick_domain_email()
    if source == "generic_api":
        from core.generic_api_mail_client import pick_account
        return pick_account().email
    if source == "imap":
        from core.imap_mail_client import pick_account
        return pick_account().email
    if source == "mailnest":
        from core.mailnest_client import pick_account
        return pick_account().email
    if source == "cloudmail":
        from core.cloudmail_client import pick_account
        return pick_account().email
    if source == "remail":
        from core.remail_client import pick_account
        return pick_account().email
    from core.outlook_client import pick_account
    return pick_account().email


def acquire_email() -> str:
    """根据 EMAIL_SOURCE 领取一个用于注册的邮箱地址；多个来源时按顺序兜底。"""
    sources = parse_email_sources()
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = _pick_from_source(source)
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


def acquire_email_from_source(source: str) -> str:
    """从调用方指定的单一来源领取邮箱，不受 EMAIL_SOURCE 兜底顺序影响。"""
    source = str(source or "").strip().lower()
    if source not in _VALID_SOURCES:
        raise ValueError(f"不支持的邮箱来源: {source}")
    email = _pick_from_source(source)
    logger.info("[EmailProvider] 指定来源领取邮箱: source=%s, email=%s", source, email)
    return email


def acquire_email_after_input(email: str | None = None) -> str:
    """在浏览器已找到邮箱输入框后领取邮箱。

    浏览器驱动把“找到输入框”和“领取邮箱”拆成两个阶段，避免页面加载、风控
    或入口识别失败时提前消耗邮箱。传入已有邮箱时不重复领取，兼容固定邮箱模式。
    """
    current = str(email or "").strip()
    if current:
        return current

    from config import email as _email_cfg

    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        raise RuntimeError("页面已找到邮箱输入框，但自动取邮箱未启用且未配置 REGISTER_EMAIL")
    allocated = str(acquire_email() or "").strip()
    if not allocated:
        raise RuntimeError("邮箱服务返回了空邮箱地址")
    logger.info("[EmailProvider] 已找到邮箱输入框，开始分配邮箱: %s", allocated)
    return allocated


def resolve_email_source(email: str) -> str:
    """根据邮箱判断实际来源，已注册账号优先使用落库来源。"""
    # 已注册账号的 email_source 是注册时的最终来源。必须先读它，不能因为
    # 当前进程里恰好残留了其它邮箱池上下文，或邮箱池顺序发生变化，就把同一
    # 地址误判到另一个服务商。
    registered_source = _registered_email_source(email)
    if registered_source:
        return registered_source

    from core.gptmail_client import get_account_context as get_gptmail_context
    if get_gptmail_context(email):
        return "gptmail"
    from core.cf_temp_mail_client import get_account_context as get_cf_context
    if get_cf_context(email):
        return "cloudflare"
    from core.mailnest_client import get_account_context as get_mailnest_context
    if get_mailnest_context(email):
        return "mailnest"
    from core.cloudmail_client import get_account_context as get_cloudmail_context
    if get_cloudmail_context(email):
        return "cloudmail"
    from core.remail_client import get_account_context as get_remail_context
    if get_remail_context(email):
        return "remail"

    from core import db
    if db.get_imap_email_by_email(email):
        return "imap"
    if db.get_generic_api_email_by_email(email):
        return "generic_api"
    if db.get_outlook_by_email(email):
        return "outlook"
    if db._find_domain_email(db._load_domain_pool(), email):  # 内部轻量查询，仅本项目使用
        return "cloudflare_domain"
    # 兜底：如果域名匹配 EMAIL_DOMAIN，则按域名邮箱处理
    try:
        from config import email as _email_cfg
        domain = (_email_cfg.EMAIL_DOMAIN or "").lower().strip()
        if domain and domain != "-" and email.lower().endswith("@" + domain):
            return "cloudflare_domain"
    except Exception:
        pass
    return parse_email_sources()[0]


def _normalize_explicit_email_source(value: str | None) -> str | None:
    """规范化调用方明确指定的邮箱来源。

    已注册账号的 ``email_source`` 是注册时落库的单一来源，查活时应优先使用
    这个值，而不是重新根据当前进程的临时邮箱上下文或全局 EMAIL_SOURCE 猜测。
    这里也兼容历史数据里偶尔保存的逗号/分号分隔值，取其中第一个有效来源。
    """
    if value is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    for item in raw.replace(";", ",").replace("|", ",").split(","):
        source = str(item or "").strip().strip("\"'").lower()
        if source in _VALID_SOURCES:
            return source
    return None


def _registered_email_source(email: str) -> str | None:
    """读取已注册账号落库的邮箱来源。"""
    try:
        from core import db

        account = db.get_account_by_email(email)
    except Exception:
        return None
    return _normalize_explicit_email_source((account or {}).get("email_source"))


def _manual_fallback_settings() -> tuple[bool, int]:
    """读取「自动取码失败转人工输入」的开关与等待时长。"""
    try:
        from config import email as _email_cfg
        enabled = bool(getattr(_email_cfg, "MANUAL_OTP_FALLBACK", True))
        timeout = int(getattr(_email_cfg, "MANUAL_OTP_TIMEOUT", 600) or 600)
    except Exception:
        enabled, timeout = True, 600
    return enabled, max(30, timeout)


# 这些异常代表「账号/任务本身不可继续」，人工输入也救不回来，不做兜底。
_NO_MANUAL_FALLBACK_EXC = {
    "AccountUnusableError", "StopRequested",
    "ManualOtpCancelled", "ManualOtpTimeoutError",
    "KeyboardInterrupt", "SystemExit",
}


def resolve_code_url(email: str, source: str | None = None) -> str:
    """解析该邮箱的取码地址，供人工输入弹框展示（取不到返回空串）。"""
    target = str(email or "").strip()
    if not target:
        return ""
    try:
        src = _normalize_explicit_email_source(source) or _registered_email_source(target) or resolve_email_source(target)
    except Exception:
        src = _normalize_explicit_email_source(source)
    from core import db

    # 1) 邮箱池里显式保存的取码地址（generic_api / imap 等）
    try:
        if src == "generic_api":
            row = db.get_generic_api_email_by_email(target)
        elif src == "imap":
            row = db.get_imap_email_by_email(target)
        elif src == "outlook":
            row = db.get_outlook_by_email(target)
        else:
            row = None
        link = str((row or {}).get("code_url") or "").strip()
        if link:
            return link
    except Exception:
        pass

    # 2) 账号素材行里可能内嵌取码地址（如 Remail pickup 链接）
    try:
        account = db.get_account_by_email(target) or {}
        for field in ("original_email_line", "copy_line"):
            line = str(account.get(field) or "")
            match = re.search(r"https?://\S+", line)
            if match:
                return match.group(0).rstrip(")")
    except Exception:
        pass

    # 2.1) Remail 账号：用保存的 service token 拼出 pickup 取件地址
    if src == "remail":
        try:
            from core import remail_client

            meta = remail_client._saved_context_metadata(target)
            token = str((meta or {}).get("service_token") or "").strip()
            link = remail_client.pickup_url(target, token)
            if link:
                return link
        except Exception:
            pass

    # 3) generic_api 约定拼接
    if src == "generic_api":
        try:
            return db._build_generic_api_code_url(target) or ""
        except Exception:
            return ""
    return ""


def _dispatch_auto_otp(source: str, email: str, after_ts: float, extra_kwargs: dict) -> str:
    """按来源分发到具体邮箱客户端取码（自动路径）。"""
    if source == "gptmail":
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare":
        from core.cf_temp_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare_domain":
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "generic_api":
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "imap":
        from core.imap_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "mailnest":
        from core.mailnest_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudmail":
        from core.cloudmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "remail":
        from core.remail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)


def _wait_manual_input(
    email: str,
    *,
    source: str,
    reason: str,
    purpose: str | None,
    timeout: int,
) -> str:
    """自动取码失败后转入人工输入，等待用户提交验证码。"""
    from core.manual_otp import request_manual_otp

    job_id = None
    try:
        from core import registration_service as svc
        job_id = getattr(svc._THREAD_CTX, "job_id", None)
    except Exception:
        job_id = None

    code_url = resolve_code_url(email, source)
    label = purpose or "邮箱验证码"
    logger.warning(
        "[OTP] 自动取码失败，转为人工输入：email=%s 用途=%s 原因=%s 等待=%ss",
        email, label, str(reason)[:160], timeout,
    )
    return request_manual_otp(
        email,
        timeout=timeout,
        job_id=job_id,
        purpose=label,
        title=f"需要手动输入验证码（{label}）",
        code_url=code_url,
        source=source,
        task=label,
        extra={"auto_error": str(reason)[:400]},
    )


def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    email_source: str | None = None,
    force_service: bool = False,
    purpose: str | None = None,
) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    - USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
      不再强制要求 Outlook clientId/refreshToken。
    - 自动取码失败/超时时，若开启 MANUAL_OTP_FALLBACK，则转为人工输入：
      页面弹出输入框提醒用户填码，提交后本函数返回该码，流程继续。
    """
    try:
        from config import email as _email_cfg
        use_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
    except Exception:
        use_service = True

    if not use_service and not force_service:
        from core.manual_otp import wait_for_manual_otp
        from config import email as _email_cfg
        timeout = int(max_wait if max_wait is not None else (getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180))
        job_id = None
        try:
            from core import registration_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        return wait_for_manual_otp(
            email,
            timeout=timeout,
            job_id=job_id,
            purpose=purpose or "邮箱验证码",
            title=f"需要手动输入验证码（{purpose or '邮箱验证码'}）",
            code_url=resolve_code_url(email, email_source),
            source=_normalize_explicit_email_source(email_source) or "",
        )

    extra_kwargs = {}
    if max_wait is not None:
        extra_kwargs["max_wait"] = max_wait
    if poll_interval is not None:
        extra_kwargs["poll_interval"] = poll_interval
    if settle_seconds is not None:
        extra_kwargs["settle_seconds"] = settle_seconds

    # 查活等已注册账号会传入注册时保存的来源；即使调用方没有显式传入，
    # 这里也先读取账号落库来源，再按当前进程上下文/邮箱池/全局配置兜底。
    source = (
        _normalize_explicit_email_source(email_source)
        or _registered_email_source(email)
        or resolve_email_source(email)
    )

    manual_enabled, manual_timeout = _manual_fallback_settings()
    try:
        return _dispatch_auto_otp(source, email, after_ts, extra_kwargs)
    except Exception as exc:
        # 账号废弃、任务被停止等属于「人工也救不回」的失败，保持原样抛出。
        if not manual_enabled or not str(email or "").strip() or type(exc).__name__ in _NO_MANUAL_FALLBACK_EXC:
            raise
        return _wait_manual_input(
            email,
            source=source,
            reason=f"{type(exc).__name__}: {exc}",
            purpose=purpose,
            timeout=manual_timeout,
        )



def email_material_line(email: str, source: str | None = None) -> str:
    """返回账号换绑后应保存的邮箱素材行。"""
    source = _normalize_explicit_email_source(source) or resolve_email_source(email)
    from core import db
    row = None
    if source == "outlook":
        row = db.get_outlook_by_email(email)
    elif source == "generic_api":
        row = db.get_generic_api_email_by_email(email)
    elif source == "imap":
        row = db.get_imap_email_by_email(email)
    if row:
        return str(row.get("copy_line") or email)
    return str(email or "")


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    source = resolve_email_source(email)
    if source == "gptmail":
        from core.gptmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare":
        from core.cf_temp_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare_domain":
        from core.qqmail_client import release_domain_email
        release_domain_email(email, status=status, note=note)
    elif source == "generic_api":
        from core.generic_api_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "imap":
        from core.imap_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "mailnest":
        from core.mailnest_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudmail":
        from core.cloudmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "remail":
        from core.remail_client import release_account
        release_account(email, status=status, note=note)
    else:
        from core.outlook_client import release_account
        release_account(email, status=status, note=note)
    return source


def release_email_if_unconsumed(email: str, note: str | None = None) -> bool:
    """回收仍停留在 used 的任务领取，且绝不覆盖已注册/已判废状态。"""
    if not (email or "").strip():
        return False

    source = resolve_email_source(email)
    from core import db

    if source == "outlook":
        changed = db.release_unconsumed_outlook(email, note=note)
    elif source == "generic_api":
        changed = db.release_unconsumed_generic_api_email(email, note=note)
    elif source == "imap":
        changed = db.release_unconsumed_imap_email(email, note=note)
    elif source == "cloudflare_domain":
        changed = db.release_unconsumed_domain_email(email, note=note)
    else:
        # 临时邮箱不重新进入本地池，只清理进程上下文；已有本地账号时保留上下文。
        if db.get_account_by_email(email) is not None:
            return False
        release_email(email, status="available", note=note)
        changed = True

    if changed:
        logger.info("[EmailProvider] 已回收未消耗邮箱: source=%s, email=%s", source, email)
    return changed


#: 换绑释放的原邮箱能回到本地邮箱池的来源；其余为远端临时邮箱，本地无池记录。
POOL_RECYCLE_SOURCES = ("outlook", "generic_api", "imap", "cloudflare_domain")


def recycle_released_email(
    email: str,
    source: str | None = None,
    *,
    credentials: dict | None = None,
    account_id: int | None = None,
    note: str | None = None,
) -> dict:
    """换绑成功后把原邮箱按**换绑前**的来源放回邮箱池。

    换绑成功后原邮箱已被服务端释放，可以重新用于注册，所以要把领取时被标记为
    used 的池记录放回 available。三处细节必须区分对待：

    1. 来源用调用方显式传入的 ``source``（换绑前的 ``account.email_source``），
       不能再用 ``resolve_email_source`` 现猜：换绑会把账号来源改写成新邮箱的
       来源，且临时邮箱上下文会随进程变化，猜错就会把邮箱记到别的来源上。
    2. 池内来源（outlook / generic_api / imap / cloudflare_domain）复位为
       available；临时邮箱来源本地没有池记录，直接 skipped，不伪造记录。
    3. 池记录缺失时只有 outlook（账号里存了同一份凭证）和域名邮箱（只需地址）
       能重建，其余来源的连接配置只存在于池记录里，只能 skipped 并说明原因。

    返回 db.recycle_email_to_pool 的结果字典 ``{"ok", "source", "mode", "message"}``。
    """
    from core import db

    target = str(email or "").strip()
    normalized = _normalize_explicit_email_source(source)
    if normalized is None:
        # 账号没落库来源时才按地址反查，仅作兜底（可能受进程内临时邮箱上下文影响）。
        normalized = resolve_email_source(target)
    if note is None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        note = f"账号 #{int(account_id)} 换绑释放，{stamp}" if account_id else f"换绑释放，{stamp}"

    result = db.recycle_email_to_pool(target, normalized, note=note, credentials=credentials)
    if result.get("ok"):
        logger.info(
            "[EmailProvider] 换绑释放的原邮箱已回池: source=%s, mode=%s, email=%s",
            result.get("source"), result.get("mode"), target,
        )
    else:
        logger.info(
            "[EmailProvider] 换绑释放的原邮箱未回池: source=%s, email=%s, reason=%s",
            result.get("source"), target, result.get("message"),
        )
    return result
