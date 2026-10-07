# -*- coding: utf-8 -*-
"""已注册账号查活：优先复用已有 AT 预热后走 reauth OTP，成功刷新 AT 即视为正常。"""
import logging
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from core import db
from core.session import BrowserSession, close_browser_session
from core.codex_oauth import _account_registration_password, _account_totp_secret, _account_totp_code
from core.humanize import delay as human_delay
from core.chatgpt_auth import get_csrf_token, get_providers, probe_auth_session, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import (
    _follow_reauth_with_retry,
    _trigger_reauth_with_retry,
    _validate_reauth_otp,
    fetch_session,
    follow_oauth_callback,
)
from core.email_provider import wait_for_otp

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)

_SESSION_FINGERPRINT_KEYS = {
    "device_id",
    "sentinel_sid",
    "oai_session_id",
    "auth_session_logging_id",
    "datadog_trace_id",
    "datadog_parent_id",
    "react_listening_key",
    "react_container_key",
    "react_resources_key",
}

# Cloudflare 主动挑战页的正文特征。403 + cf-mitigated=challenge 或 CF 挑战页正文都说明
# 出口 IP 信誉或 TLS 指纹不匹配（见 tools/test_cf_impersonate_matrix.py 实测结论），
# 同一会话原地重试会稳定复现，必须换出口 / 重建指纹。
_CF_CHALLENGE_BODY_MARKERS = (
    "_cf_chl_opt",
    "/cdn-cgi/challenge-platform",
    "cf-chl-widget",
    "challenge-form",
    "just a moment",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)


def _is_cf_challenge_response(response) -> bool:
    """判断一个 HTTP 响应是否是 Cloudflare 挑战页（区别于真实业务 403）。"""
    if response is None:
        return False
    try:
        headers = getattr(response, "headers", None) or {}
    except Exception:
        headers = {}
    try:
        if str(headers.get("cf-mitigated") or "").strip().lower() == "challenge":
            return True
    except Exception:
        pass
    try:
        server = str(headers.get("server") or "").strip().lower()
    except Exception:
        server = ""
    if server != "cloudflare":
        return False
    try:
        body = str(getattr(response, "text", "") or "")[:20000].lower()
    except Exception:
        return False
    return any(marker in body for marker in _CF_CHALLENGE_BODY_MARKERS)


def _is_cf_challenge_error(exc: BaseException) -> bool:
    """异常链里是否带 Cloudflare 主动挑战。

    熔断后抛出的是本地 ``RuntimeError``，此时没有 ``response``，只能靠错误文本里
    附带过的 ``cf-mitigated=challenge`` 标记判断（见 :func:`_warm_login_fingerprint_context`）。
    """
    if _is_cf_challenge_response(getattr(exc, "response", None)):
        return True
    text = str(exc or "").lower()
    return "cf-mitigated=challenge" in text or "cf-mitigated: challenge" in text


def _report_cf_challenged_proxy(session) -> None:
    """把被 CF 挑战的出口登记进自适应路由冷却，避免下一轮继续抽到同一个坏出口。"""
    proxy = str(
        getattr(session, "proxy", "") or getattr(session, "proxy_target", "") or ""
    ).strip()
    if not proxy:
        return
    try:
        from config import proxy as proxy_cfg
        proxy_cfg.report_proxy_result(
            proxy,
            status=403,
            error="Cloudflare challenge (cf-mitigated=challenge)",
        )
    except Exception:
        logger.debug("[查活] 上报 CF 挑战出口失败", exc_info=True)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _new_fingerprint_pinned_session(
    email: str,
    proxy: str | None,
    fingerprint_state: dict | None = None,
) -> BrowserSession:
    """创建任务独占账号会话；同一路由尝试内固定完整身份与浏览器画像。"""
    state = fingerprint_state if fingerprint_state is not None else {}
    saved_profile = state.get("browser_profile")
    identity = str(email).strip().lower()
    # 和协议注册使用同一套生命周期配置：开启“同邮箱保持协议指纹”时，
    # 查活可重新构造注册阶段的 device/session/硬件画像；关闭时每次查活
    # 生成独立 seed。无论哪种模式，同一任务内部的所有阶段/重试都固定。
    fingerprint_seed = str(state.get("fingerprint_seed") or "").strip() or None
    if not state.get("fingerprint_initialized"):
        from config import register as register_cfg
        reuse_by_email = bool(
            getattr(register_cfg, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False)
        ) and not bool(state.get("force_fresh"))
        # “每次重新创建”必须和协议注册完全一致：不传 seed，让 BrowserSession
        # 生成真实的随机 UUID4。旧实现先生成随机 seed 再派生 UUID5，虽然值也
        # 随机，但 UUID version 位与注册时不同，属于可观测的指纹差异。
        fingerprint_seed = f"registration:{identity}" if reuse_by_email else None
        state["fingerprint_seed"] = fingerprint_seed or ""
        state["fingerprint_mode"] = (
            "registration_email_stable" if reuse_by_email else "fresh_per_check"
        )
        state["fingerprint_initialized"] = True
    session = BrowserSession(
        proxy=proxy,
        # 首次按当前出口生成地区画像；同一路由内部如需重建则原样复用。
        detect_exit_geo=not bool(saved_profile),
        browser_profile=dict(saved_profile) if isinstance(saved_profile, dict) else None,
        fingerprint_seed=fingerprint_seed,
        device_id=state.get("device_id"),
        auth_session_logging_id=state.get("auth_session_logging_id"),
        oai_session_id=state.get("oai_session_id"),
        sentinel_sid=state.get("sentinel_sid"),
    )
    for key in (
        "device_id", "auth_session_logging_id", "oai_session_id", "sentinel_sid",
    ):
        state.setdefault(key, str(getattr(session, key, "") or ""))
    if not saved_profile:
        generated_profile = getattr(session, "browser_profile", None)
        if isinstance(generated_profile, dict):
            state["browser_profile"] = dict(generated_profile)
    return session


def _fingerprint_mode_label(state: dict) -> str:
    mode = str(state.get("fingerprint_mode") or "")
    if mode == "registration_email_stable":
        return "同邮箱复用协议注册指纹"
    return "每次查活重新创建"


def _warm_login_fingerprint_context(session: BrowserSession) -> None:
    """复现 plus 纯协议注册成功样本的登录页初始化顺序。"""
    from core.chatgpt_bootstrap import anonymous_bootstrap

    logger.info(
        "[查活] 登录链预热：/auth/login 顶层导航 → anonymous bootstrap → "
        "providers → session → CSRF → session"
    )
    try:
        stage = "chatgpt.com /auth/login 顶层导航"
        nav = session.get(
            "https://chatgpt.com/auth/login",
            headers=session.get_chatgpt_navigate_headers(
                # 地址栏级顶层导航：无 Referer，Sec-Fetch-Site=none。
                referer="", user_initiated=True,
            ),
            allow_redirects=True,
            # 代理端口可连接不代表其上游 TLS 可用，避免坏节点长期占住 worker。
            timeout=12,
        )
        if int(getattr(nav, "status_code", 0) or 0) >= 400 and _is_cf_challenge_response(nav):
            # 登录页 document 只是 best-effort 指纹预热。CF 挑战它并不代表后面的
            # CSRF/Signin 一定不可用（实测同一出口下 document 403、bootstrap 200 会
            # 同时出现），把它当硬门槛会让本来能成功的换绑/查活直接中断。
            # 这里只记录特征并清掉可选预热触发的会话熔断，把判定权交给强制步骤。
            logger.warning(
                "[查活] /auth/login 顶层导航被 Cloudflare 挑战（cf-mitigated=challenge），"
                "跳过页面预热继续 CSRF/Signin：url=%s",
                getattr(nav, "url", "") or "https://chatgpt.com/auth/login",
            )
            _clear_optional_bootstrap_circuit(session)
        else:
            nav.raise_for_status()
            observe = getattr(session, "observe_chatgpt_document", None)
            if callable(observe):
                observe(nav)

        stage = "anonymous bootstrap"
        anonymous_bootstrap(session, strict=False)
        # best-effort bootstrap 的非关键接口不能阻断正式认证链。
        _clear_optional_bootstrap_circuit(session)

        stage = "NextAuth providers"
        get_providers(session)
        stage = "NextAuth anonymous session"
        probe_auth_session(session)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        url = getattr(response, "url", None) or getattr(response, "request", None)
        if url is not None and not isinstance(url, str):
            url = getattr(url, "url", None)
        detail = f"stage={stage} status={status or '?'} url={url or '?'}"
        # 区分「Cloudflare JS 挑战」与真正的 403 封禁。带 cf-mitigated=challenge 的
        # 403 说明出口 IP 信誉或 TLS 指纹不匹配，此时同一会话原地重试必然重复失败，
        # 必须换指纹/换出口；把该标志带进错误文本，账号列表里能直接看出来。
        cf_mitigated = ""
        try:
            cf_mitigated = str((getattr(response, "headers", None) or {}).get("cf-mitigated") or "")
        except Exception:
            cf_mitigated = ""
        if cf_mitigated:
            detail += f" cf-mitigated={cf_mitigated}（CF 挑战：出口 IP 信誉或 TLS 指纹不匹配）"
        raise RuntimeError(
            f"Recent Login 预热失败：{detail} error={type(exc).__name__}: {str(exc)[:240]}"
        ) from exc


def _network_preflight_with_retry(
    email: str,
    proxy: str | None,
    max_attempts: int = 4,
    fingerprint_state: dict | None = None,
) -> tuple[BrowserSession, str]:
    """CSRF → Signin 备用预检；失败时保留同一会话重试。

    `/api/auth/providers` 只是 NextAuth 的发现接口，signin 端点并不依赖它返回的
    内容。实际运行中该接口很容易先被 Cloudflare 拦截，如果把它作为硬门槛，后续
    本来可用的 CSRF/授权链永远不会执行。因此查活备用链不再把 providers 当作
    必经步骤。

    这里必须原样传递 ``proxy``：``None`` 表示按配置选代理，空字符串表示明确
    直连。之前用 ``proxy if proxy else None`` 把直连兜底误变成了再次抽取代理。
    """
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    state = fingerprint_state if fingerprint_state is not None else {}
    # 一次网络预检只创建一个 BrowserSession。403 响应下发的新 __cf_bm、
    # OAuth/设备上下文都保留在同一 Cookie Jar 中供下一轮使用。
    session = _new_fingerprint_pinned_session(email, proxy, state)
    logger.info("[查活] 指纹生命周期：%s", _fingerprint_mode_label(state))
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "[查活] 复用统一会话：proxy=%s device_id=%s oai_session_id=%s（网络预检第 %s/%s 次）",
            session.proxy or "配置随机/直连", session.device_id,
            str(getattr(session, "oai_session_id", "") or "")[:12] + "...",
            attempt, max_attempts,
        )
        logger.info("[查活] 指纹摘要：%s", session.fingerprint_summary_text())
        cf_hits_before = int(getattr(session, "cf_challenge_hits", 0) or 0)
        try:
            _warm_login_fingerprint_context(session)
            csrf = get_csrf_token(session)
            # 成功 Web 样本在 signin 前会再次确认匿名 NextAuth session。
            probe_auth_session(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            cf_challenged = _is_cf_challenge_error(exc) or bool(
                getattr(session, "cf_challenged_after", lambda _n: False)(cf_hits_before)
            )
            if cf_challenged:
                # CF 主动挑战绑定出口 IP 信誉 / TLS 指纹，同一会话原地重试只会稳定
                # 复现 403。立刻上报坏出口并抛出，让上层换一条出口 / 重建指纹，而不是
                # 把 max_attempts 次机会全浪费在同一个出口上。
                _report_cf_challenged_proxy(session)
                logger.warning(
                    "[查活] 检测到 Cloudflare 挑战（%s/%s），放弃同会话重试，改换出口/指纹：%s",
                    attempt, max_attempts, str(exc)[:200],
                )
                try:
                    close_browser_session(session)
                except Exception:
                    pass
                raise
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                try:
                    close_browser_session(session)
                except Exception:
                    pass
                raise
            _clear_optional_bootstrap_circuit(session)
            logger.warning(
                "[查活] 网络预检失败（%s/%s），保留当前 session/deviceId/CF Cookie 重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_fingerprint_for_account(session: BrowserSession) -> dict:
    """账号里只记录运行环境画像，不保存会话/设备标识。"""
    fp = session.fingerprint_summary()
    return {k: v for k, v in fp.items() if k not in _SESSION_FINGERPRINT_KEYS}


def _safe_fingerprint_text_for_account(session: BrowserSession) -> str:
    fp = _safe_fingerprint_for_account(session)
    parts = [
        f"proxy={BrowserSession._short_value(fp.get('proxy') or 'direct', 36)}",
        f"ua={BrowserSession._short_value(fp.get('user_agent'), 72)}",
        f"lang={fp.get('accept_language')}",
        f"tz={fp.get('timezone_iana')}({fp.get('timezone_offset_minutes')})",
        f"screen={fp.get('screen_width')}x{fp.get('screen_height')}@{fp.get('device_pixel_ratio')}",
        f"cpu={fp.get('hardware_concurrency')}",
        f"mem={fp.get('device_memory')}",
        f"geo={fp.get('geo_country') or '?'}:{fp.get('geo_city') or '?'}",
    ]
    return " ".join(parts)


def _extract_continue_url(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    return str(
        result.get("continue_url")
        or result.get("external_url")
        or result.get("url")
        or page.get("continue_url")
        or page.get("external_url")
        or page.get("url")
        or ""
    ).strip()


def _extract_factor_id(result: dict | None, continue_url: str) -> str:
    if isinstance(result, dict):
        page = result.get("page") or {}
        page = page if isinstance(page, dict) else {}
        payload = page.get("payload") or {}
        if isinstance(payload, dict):
            factor_id = str(payload.get("factor_id") or "").strip()
            if factor_id:
                return factor_id
        if isinstance(page.get("payload"), dict):
            factor_id = str(page["payload"].get("factor_id") or "").strip()
            if factor_id:
                return factor_id
    if "/mfa-challenge/" in continue_url:
        return continue_url.rstrip("/").rsplit("/", 1)[-1]
    return ""


def _password_verify(session: BrowserSession, password: str) -> dict:
    from core.openai_auth import build_sentinel_header, request_sentinel_token

    sentinel_resp = request_sentinel_token(session, "password_verify")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "password_verify")
    headers = session.get_auth_headers(referer="https://auth.openai.com/log-in/password")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers=headers,
        data=json.dumps({"password": password}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _mfa_issue_challenge(session: BrowserSession, factor_id: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/mfa-challenge")
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/issue_challenge",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "force_fresh_challenge": False}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _mfa_verify(session: BrowserSession, factor_id: str, code: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/mfa-challenge")
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "code": code}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _follow_continue_and_fetch(session: BrowserSession, continue_url: str, *, referer: str) -> dict:
    """完成 callback/session，并对 403 保留同会话 Cookie 做阶段内重试。

    callback 与 session 分开重试：callback 一旦成功就不重复消费 OAuth code；
    只有 callback 本身失败时才重放 continue_url。重试耗尽后抛给上层，由
    live_check_service 按既有策略换成独立直连会话完整兜底。
    """
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            follow_oauth_callback(session, continue_url, referer=referer)
            break
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            _clear_optional_bootstrap_circuit(session)
            delay = float(2 ** (attempt - 1))
            logger.warning(
                "[查活] OAuth callback 临时失败（%s/%s），保留当前 "
                "session/deviceId/CF Cookie，%.1fs 后重试：%s",
                attempt, max_attempts, delay, str(exc)[:200],
            )
            time.sleep(delay)

    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_session(session)
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            _clear_optional_bootstrap_circuit(session)
            delay = float(2 ** (attempt - 1))
            logger.warning(
                "[查活] Session/AT 拉取临时失败（%s/%s），保留当前 "
                "session/deviceId/CF Cookie，%.1fs 后重试：%s",
                attempt, max_attempts, delay, str(exc)[:200],
            )
            time.sleep(delay)
    raise RuntimeError("查活 Session/AT 拉取重试耗尽")


def _is_mfa_step(result: dict | None, continue_url: str) -> bool:
    """判断上一步响应是否落在 MFA challenge 上。"""
    page = result.get("page") if isinstance(result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    return "/mfa-challenge/" in str(continue_url or "") or page_type == "mfa_challenge"


def _maybe_complete_mfa(
    session: BrowserSession,
    email: str,
    result: dict | None,
    continue_url: str,
    *,
    stage: str,
) -> dict | None:
    """上一步若进入 MFA challenge，则用账号 totp_secret 完成 TOTP 并返回 session。

    不属于 MFA 步骤时返回 None，调用方继续原有流程。

    注意：密码登录和邮箱 OTP 登录都可能被服务端追加 MFA 挑战（账号开启 2FA 后
    login_or_signup 会在任意登录方式后插入 mfa-challenge）。两处必须共用本函数，
    否则某个登录分支会停在 mfa-challenge 页面上，随后拉 session 只能拿到
    「没有 accessToken」的假失败。
    """
    if not _is_mfa_step(result, continue_url):
        return None

    url = str(continue_url or "")
    factor_id = _extract_factor_id(result, url)
    if not factor_id:
        raise RuntimeError(f"{stage}后进入 MFA 但未拿到 factor_id: {result}")
    if not _account_totp_secret(email):
        raise RuntimeError(f"{stage}后进入 MFA，但账号没有 totp_secret：{email}")

    logger.info("[查活] 已进入 MFA challenge（%s），开始提交 TOTP：%s factor_id=%s", stage, email, factor_id)

    last_exc: Exception | None = None
    # TOTP 有 30s 时间窗，跨窗口边界或服务端时钟偏差会导致首次校验失败；
    # 用新生成的验证码重试一次即可，无需重新走整条登录链。
    for attempt in range(1, 3):
        try:
            _mfa_issue_challenge(session, factor_id)
            code = _account_totp_code(email)
            if not code:
                raise RuntimeError(f"无法生成 TOTP 验证码：{email}")
            mfa_result = _mfa_verify(session, factor_id, code)
        except Exception as exc:
            last_exc = exc
            if attempt >= 2:
                raise
            logger.warning("[查活] MFA TOTP 提交失败（%s/2），等待新时间窗后重试：%s", attempt, str(exc)[:160])
            time.sleep(4)
            continue
        mfa_continue_url = _extract_continue_url(mfa_result) or url
        if not mfa_continue_url:
            raise RuntimeError(f"MFA 验证成功但没有 continue_url: {mfa_result}")
        human_delay("api")
        return _follow_continue_and_fetch(
            session,
            mfa_continue_url,
            referer=f"https://auth.openai.com/mfa-challenge/{factor_id}",
        )
    raise last_exc if last_exc else RuntimeError("MFA TOTP 验证失败")


def _password_rejected_as_otp_fallback(exc: BaseException) -> bool:
    """密码登录被服务端明确拒绝时，允许回退邮箱 OTP 登录。

    仅对凭据类错误回退；403（Cloudflare/出口拦截）、5xx、网络异常仍然抛出，
    避免把真正的网络问题伪装成「密码不对」。
    """
    return _exception_status_code(exc) in (400, 401, 422)


def _stored_access_token(email: str) -> str:
    """读取本地账号已有 AT，用于先预热登录态再走稳定的 reauth 链。"""
    try:
        account = db.get_account_by_email(email)
        return str((account or {}).get("access_token") or "").strip()
    except Exception as exc:
        logger.debug("[查活] 读取已有 accessToken 失败，改走备用登录链：%s: %s", type(exc).__name__, exc)
        return ""


def _clear_optional_bootstrap_circuit(session: BrowserSession) -> None:
    """清理可选登录态预热造成的本地熔断，不影响后续正式认证请求。

    authenticated_bootstrap 是 best-effort 预热，其中个别旧接口返回 403 不等于
    reauth 链不可用；BrowserSession 的通用熔断器若保留该状态，会直接拦截后续
    `/api/auth/csrf`，导致稳定的 2FA 链也无法开始。
    """
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
        return
    if getattr(session, "blocked_until", 0.0):
        session.blocked_until = 0.0
        session.blocked_reason = ""


def _warm_authenticated_session(session: BrowserSession, access_token: str) -> None:
    """复用 2FA 已验证的登录态预热流程。预热失败不直接判定账号死亡。"""
    if not access_token:
        return
    from core.chatgpt_bootstrap import authenticated_bootstrap

    try:
        logger.info("[查活] 使用已有 accessToken 预热登录态...")
        authenticated_bootstrap(session, access_token, strict=False)
        logger.info("[查活] accessToken 预热完成，继续走 reauth OTP")
    except Exception as exc:
        # strict=False 已经会吞掉大部分单接口错误；这里仅兜住初始化异常。
        logger.warning("[查活] accessToken 预热失败，继续走 reauth OTP：%s: %s", type(exc).__name__, str(exc)[:180])
    finally:
        _clear_optional_bootstrap_circuit(session)


def _exception_response_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(getattr(response, "text", "") or "")


def _exception_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return None
    return status or None


# OpenAI 在「账号已改绑到别的邮箱」时会返回这段话。它既不是废号，也不是网络问题：
# 账号本身还活着，只是本地记录的主邮箱已经过期，需要用换绑后的新邮箱重查。
_EMAIL_SUPERSEDED_MARKERS = (
    "can no longer be used to sign in",
    "now uses a different email",
)


def _detect_email_superseded(text: str) -> str:
    """识别「本地记录的邮箱已被换绑」这一类失败，并给出可操作的提示。"""
    raw = str(text or "").lower()
    if not raw:
        return ""
    if not all(marker in raw for marker in _EMAIL_SUPERSEDED_MARKERS):
        return ""
    return (
        "邮箱已改绑：该账号已改用其他邮箱登录，本地记录的主邮箱已失效。"
        "账号本身仍然存活，请把账号主邮箱更新为换绑后的新邮箱（可参考账号备注）后重新查活。"
    )


def _validate_reauth_with_retry(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    max_otp_attempts: int = 3,
    email_source: str | None = None,
    purpose_prefix: str = "查活",
) -> str:
    """提交 reauth OTP；验证码错误时重新发送并重新取码。"""
    current_otp: str | None = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待重认证 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    email_source=email_source,
                    purpose=f"{purpose_prefix}-重认证验证码",
                )
            human_delay("otp_input")
            continue_url = _validate_reauth_otp(session, current_otp)
            if not continue_url:
                raise RuntimeError("重认证 OTP 验证响应缺少 continue_url")
            return str(continue_url)
        except AccountUnusableError:
            raise
        except Exception as exc:
            last_exc = exc
            body = _exception_response_text(exc)
            dead_code = detect_account_unusable_text(body) or detect_account_unusable_text(str(exc))
            if dead_code:
                raise AccountUnusableError(
                    f"账号已废弃（{dead_code}），邮箱不可再用",
                    error_code=dead_code,
                ) from exc

            status = _exception_status_code(exc)
            # reauth validate 的 403 可能是 Cloudflare/出口拦截；不要在同一已熔断
            # 会话上反复发送 OTP，交给上层的直连兜底处理。
            retryable_otp = status in (400, 401, 422)
            if attempt >= max_otp_attempts or not retryable_otp:
                raise
            logger.warning(
                "[查活] 重认证 OTP 无效/过期，重新发送后再取（%s/%s）：%s",
                attempt,
                max_otp_attempts,
                str(exc)[:180],
            )
            send_email_otp(session)
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("重认证 OTP 验证失败")


def _login_via_reauth(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
    purpose_prefix: str = "查活",
) -> dict:
    """按 2FA 已验证链路重新认证并刷新 ChatGPT session。"""
    auth_url = _trigger_reauth_with_retry(session, email)
    logger.info("[查活] reauth authorize URL 已获取")
    human_delay("api")
    final_url = _follow_reauth_with_retry(session, auth_url)
    dead_code = detect_account_unusable_text(final_url)
    if dead_code:
        raise AccountUnusableError(f"账号已废弃（{dead_code}）", error_code=dead_code)
    human_delay("navigate")
    logger.info("[查活] 已跟随 reauth authorize URL，开始等待邮箱 OTP")
    continue_url = _validate_reauth_with_retry(
        session,
        email,
        otp_after_ts,
        email_source=email_source,
        purpose_prefix=purpose_prefix,
    )
    logger.info("[查活] reauth OTP 验证通过，开始交换新 token")
    human_delay("api")
    return _follow_continue_and_fetch(
        session,
        continue_url,
        referer="https://auth.openai.com/email-verification",
    )


def _login_via_email_otp(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
    purpose_prefix: str = "查活",
) -> dict:
    """完成邮箱 OTP 登录，并跟随 OAuth callback 后拉取 ChatGPT session。"""
    validate_result = _validate_with_retry(
        session,
        email,
        otp_after_ts,
        email_source=email_source,
        purpose_prefix=purpose_prefix,
    )
    page = validate_result.get("page") if isinstance(validate_result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    continue_url = _extract_continue_url(validate_result)
    if not continue_url:
        raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
    if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
        raise RuntimeError(f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}")
    # 邮箱 OTP 通过后服务端可能追加 MFA 挑战（账号已开启 2FA），必须在这里消化掉，
    # 否则会停在 mfa-challenge 页面并错误地判定为「未拿到 accessToken」。
    mfa_session_info = _maybe_complete_mfa(
        session,
        email,
        validate_result,
        continue_url,
        stage="邮箱 OTP",
    )
    if mfa_session_info is not None:
        logger.info("[查活] 邮箱 OTP + MFA 登录完成：%s", email)
        return mfa_session_info
    logger.info("[查活] 邮箱 OTP 验证完成，开始跟随 OAuth callback")
    return _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/email-verification")


def _login_via_password_or_otp(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
    purpose_prefix: str = "查活",
    landing_url: str = "",
) -> dict:
    """按 authorize 落点页选择登录方式，并在需要时用 TOTP 完成 MFA。

    落点页是服务端对「这个账号该用哪种方式登录」的表态：
      * /log-in/password     → 服务端期望密码登录
      * /email-verification  → 服务端期望邮箱 OTP（此时对 password/verify 发请求会 400）
    因此优先依据落点页决定，其次才看本地是否存有注册密码。
    """
    password = _account_registration_password(email)
    landed_on_password = "/log-in/password" in str(landing_url or "")
    landed_on_otp = "email-verification" in str(landing_url or "")

    if not password:
        logger.info("[查活] 未找到注册密码，继续使用邮箱 OTP：%s", email)
        return _login_via_email_otp(
            session,
            email,
            otp_after_ts,
            email_source=email_source,
            purpose_prefix=purpose_prefix,
        )

    if landed_on_otp and not landed_on_password:
        # 有密码但服务端已经把我们带到邮箱验证页：此时发 password/verify 会被
        # 判为状态不匹配（400），直接走邮箱 OTP。
        logger.info("[查活] authorize 落在邮箱验证页，忽略本地密码，改走邮箱 OTP：%s", email)
        return _login_via_email_otp(
            session,
            email,
            otp_after_ts,
            email_source=email_source,
            purpose_prefix=purpose_prefix,
        )

    logger.info("[查活] 账号存在密码，优先走密码登录：%s", email)
    try:
        password_result = _password_verify(session, password)
    except Exception as exc:
        if not _password_rejected_as_otp_fallback(exc):
            raise
        logger.warning(
            "[查活] 密码登录被拒（%s: %s），回退邮箱 OTP：%s",
            _exception_status_code(exc) or type(exc).__name__,
            str(exc)[:160],
            email,
        )
        try:
            send_email_otp(session)
        except Exception as send_exc:
            logger.warning("[查活] 回退邮箱 OTP 时重新发送验证码失败（忽略）：%s", str(send_exc)[:160])
        return _login_via_email_otp(
            session,
            email,
            max(otp_after_ts, time.time()),
            email_source=email_source,
            purpose_prefix=purpose_prefix,
        )

    continue_url = _extract_continue_url(password_result)
    page = password_result.get("page") if isinstance(password_result, dict) else {}
    page = page if isinstance(page, dict) else {}

    mfa_session_info = _maybe_complete_mfa(
        session,
        email,
        password_result,
        continue_url,
        stage="密码登录",
    )
    if mfa_session_info is not None:
        return mfa_session_info

    page_type = str(page.get("type") or "")
    if "email-verification" in continue_url or page_type in {"email_verification", "email_otp_send"}:
        logger.info("[查活] 密码登录后仍进入邮箱 OTP，继续完成邮箱验证：%s", email)
        return _login_via_email_otp(
            session,
            email,
            otp_after_ts,
            email_source=email_source,
            purpose_prefix=purpose_prefix,
        )

    if continue_url:
        logger.info("[查活] 密码登录直接给出回调地址，继续完成回调：%s", email)
        return _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/log-in/password")

    raise RuntimeError(f"密码登录成功但没有可用 continue_url: {password_result}")


def _login_via_full_web_flow(
    email: str,
    proxy: str | None,
    *,
    email_source: str | None,
    fingerprint_state: dict,
) -> tuple[BrowserSession, dict]:
    """按 plus 纯协议注册的 Web 登录序列建立一份全新登录态。"""
    session, authorize_url = _network_preflight_with_retry(
        email,
        proxy,
        fingerprint_state=fingerprint_state,
    )
    otp_after_ts = time.time()
    final_url = follow_authorize(session, authorize_url)
    dead_code = detect_account_unusable_text(final_url)
    if dead_code:
        raise AccountUnusableError(
            f"账号已废弃（{dead_code}）",
            error_code=dead_code,
        )
    logger.info("[查活] authorize 落点页：%s", str(final_url or "")[:160])
    session_info = _login_via_password_or_otp(
        session,
        email,
        otp_after_ts,
        email_source=email_source,
        landing_url=str(final_url or ""),
    )
    return session, session_info


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    max_otp_attempts: int = 3,
    email_source: str | None = None,
    purpose_prefix: str = "查活",
) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    email_source=email_source,
                    purpose=f"{purpose_prefix}-登录验证码",
                )
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    clear_log: bool = True,
    email_source: str | None = None,
    fingerprint_state: dict | None = None,
) -> dict:
    """
    重新登录账号并刷新最新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    key = email.lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    fh: logging.FileHandler | None = None
    session: BrowserSession | None = None
    task_fingerprint_state = fingerprint_state if fingerprint_state is not None else {}
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始重新登录：%s", email)
        existing_access_token = _stored_access_token(email)
        has_totp = bool(_account_totp_secret(email))
        if existing_access_token and not has_totp:
            # 2FA 设置流程已经验证：先用已有 AT 预热 ChatGPT 登录态，再走
            # reauth → 邮箱 OTP → callback。该链路不依赖容易被 CF 拦截的
            # /api/auth/providers。已开启 TOTP 的账号保留密码 → MFA 路径，
            # 避免把 MFA challenge 误当成邮箱 OTP 页面。
            logger.info("[查活] 流程：登录态预热 → CSRF → Reauth Signin → Authorize → 邮箱 OTP → OAuth callback → Session/AT")
            session = _new_fingerprint_pinned_session(email, proxy, task_fingerprint_state)
            logger.info(
                "[查活] 指纹生命周期：%s",
                _fingerprint_mode_label(task_fingerprint_state),
            )
            logger.info(
                "[查活] 会话创建完成：proxy=%s device_id=%s（复用2FA稳定链路）",
                session.proxy or "直连/配置随机",
                session.device_id,
            )
            logger.info("[查活] 指纹摘要：%s", session.fingerprint_summary_text())
            _warm_authenticated_session(session, existing_access_token)
            human_delay("navigate")
            try:
                session_info = _login_via_reauth(
                    session,
                    email,
                    time.time(),
                    email_source=email_source,
                )
            except Exception as reauth_exc:
                if not _is_retryable_network_error(reauth_exc):
                    raise
                # reauth/callback 的同会话阶段重试已经耗尽。继续复用熔断的
                # Cookie Jar 没有意义；参考 plus 纯协议注册，建立干净会话并按
                # /auth/login → providers/session/csrf/session → signin 完整重登。
                failed_proxy = session.proxy if proxy is None else proxy
                logger.warning(
                    "[查活] AT reauth 链临时失败，切换干净会话执行纯协议完整登录：%s",
                    str(reauth_exc)[:240],
                )
                try:
                    close_browser_session(session)
                except Exception:
                    pass
                session, session_info = _login_via_full_web_flow(
                    email,
                    failed_proxy,
                    email_source=email_source,
                    fingerprint_state=task_fingerprint_state,
                )
        else:
            # 兼容没有本地 AT 或已开启 TOTP 的记录，按 plus 成功注册样本复现
            # 登录页 document 与完整 NextAuth 调用顺序。
            logger.info(
                "[查活] 流程：登录页 → Providers/Session/CSRF/Session → Signin → "
                "Authorize → 密码/邮箱 OTP → MFA(如有) → OAuth callback → Session/AT"
            )
            session, session_info = _login_via_full_web_flow(
                email,
                proxy,
                email_source=email_source,
                fingerprint_state=task_fingerprint_state,
            )
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")

        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        fp = _safe_fingerprint_for_account(session)
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "proxy_used": session.proxy or None,
            "fingerprint": fp,
            "fingerprint_text": _safe_fingerprint_text_for_account(session),
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(_exception_response_text(exc)) or detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        superseded = _detect_email_superseded(_exception_response_text(exc)) or _detect_email_superseded(str(exc))
        if superseded:
            logger.warning("[查活] 邮箱已改绑：%s %s", email, superseded)
            return {"ok": False, "status": "failed", "checked_at": checked_at, "error": superseded}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if session is not None:
                try:
                    close_browser_session(session)
                except Exception:
                    pass
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
