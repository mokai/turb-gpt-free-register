# -*- coding: utf-8 -*-
"""通过比特浏览器（BitBrowser）+ Selenium 执行 ChatGPT 注册。

页面操作逻辑与 RoxyBrowser 注册流程完全一致（同一套 OpenAI 注册页面），
这里复用 core/roxy_registration 的共享函数，只替换浏览器窗口生命周期：
- 窗口管理走比特本地 API（core/bitbrowser_client.py，POST + success/data/msg）
- 日志前缀 [Bit注册]，超时/无头等读 config/bitbrowser.py
"""
from __future__ import annotations

import logging
import platform
import time
from contextvars import Token
from pathlib import Path
from typing import Callable

from config import bitbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import SeleniumTrafficTracker
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.bitbrowser_client import BitBrowserClient, BitOpenResult
from core.roxy_registration import (
    _SELENIUM_TIMEOUT_OVERRIDE,
    _build_driver,
    _check_manual_stop,
    _click_continue,
    _click_resend_email_otp,
    _clear_otp_inputs,
    _complete_profile_page,
    _fetch_chatgpt_session,
    _fill_password_page_if_present,
    _maybe_accept,
    _page_warmup,
    _safe_get,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[Bit注册]"


def _center_browser_window(driver) -> None:
    """把可见的比特浏览器窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "BIT_OPEN_HEADLESS", False)):
        return
    try:
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Bit] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:
        logger.warning("[Bit] 浏览器窗口居中失败，继续执行：%s", exc)


def run_bit_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """比特浏览器自动化注册入口。"""
    client = BitBrowserClient()
    opened = client.open_window()
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker: SeleniumTrafficTracker | None = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None
    timeout_token: Token | None = None

    def _traffic_checkpoint() -> None:
        if traffic_tracker is not None:
            try:
                traffic_tracker.checkpoint()
            except Exception as exc:
                logger.debug("[Bit注册] 刷新浏览器流量统计失败：%s", exc)

    try:
        timeout_token = _SELENIUM_TIMEOUT_OVERRIDE.set(int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90))
        driver = _build_driver(opened, driver_label="[Bit]")
        try:
            # 共享页面操作函数按这个前缀输出日志（见 roxy_registration._log_prefix）。
            driver._registration_log_prefix = _LOG_PREFIX
        except Exception:
            pass
        try:
            traffic_tracker = SeleniumTrafficTracker(driver, label="Bit")
        except Exception as exc:
            # 统计失败不应影响注册主流程。
            logger.warning("[Bit注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
        data_saver = BrowserDataSaver(label="Bit")
        if traffic_tracker is not None:
            traffic_tracker.attach_data_saver(data_saver)
        data_saver.install_selenium(driver)
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90))
        try:
            driver.set_script_timeout(12)
        except Exception:
            pass
        logger.info("[Bit注册] 开始：%s，window=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Bit注册] 打开登录页：https://chatgpt.com/auth/login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        _traffic_checkpoint()
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Bit注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _traffic_checkpoint()
        _check_manual_stop()

        # 新版注册流如果邮箱后直接进入验证码页，也优先点击“使用密码继续”进入
        # /create-account/password，设置密码并把密码写入账号 extra.registration_password。
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        _traffic_checkpoint()
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Bit注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    # 兜底：OpenAI 重发验证码时常常是同一封邮件（时间戳不变），
                    # after_ts 过滤会把它当成旧邮件忽略。先宽松取最新一条验证码，
                    # 取到就直接用它重试提交，避免误点“重新发送”后死等。
                    fallback_otp = None
                    try:
                        fallback_otp = wait_for_otp(email, after_ts=0.0, max_wait=15, poll_interval=3)
                    except Exception:
                        fallback_otp = None
                    if fallback_otp:
                        logger.info(
                            "[Bit注册][OTP] 取码接口超时但宽松取到最新验证码，直接重试提交：%s (fallback)",
                            fallback_otp,
                        )
                        current_otp = fallback_otp
                        continue
                    logger.warning(
                        "[Bit注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Bit注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Bit注册][OTP] 已填写邮箱验证码")
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Bit注册][OTP] 已提交邮箱验证码，等待资料页或登录态")
            except Exception as exc:
                logger.info("[Bit注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=30)
            _traffic_checkpoint()
            if outcome == 'accepted':
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning("[Bit注册][OTP] 验证码错误/过期，准备重新发送并重新获取验证码（%s/%s）", otp_attempt + 1, max_otp_attempts)
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            _traffic_checkpoint()
            human_delay("api")
            current_otp = None

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Bit注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        _traffic_checkpoint()
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("[Bit注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _fetch_chatgpt_session(driver, timeout=120)
        _traffic_checkpoint()
        access_token = session_info["accessToken"]
        logger.info("[Bit注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Bit注册] 当前比特浏览器自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                # 注册流程本身已创建比特一号一窗口。这里不能再新建第二个比特窗口；
                # 复用当前注册窗口，先清理 Cookie/session/localStorage/cache，再开始 Codex 授权。
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Bit注册][Codex] ENABLE_CODEX_AUTO=True，复用当前注册比特窗口执行 Codex 授权，不创建新窗口")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
                _traffic_checkpoint()
            else:
                logger.info("[Bit注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Bit注册")
        _traffic_checkpoint()
        if traffic_tracker is not None:
            network_traffic = traffic_tracker.stop()
        if data_saver is not None:
            data_saver.stop()
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "bitbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            data_saver.stop()
        logger.error("[Bit注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Bit注册] 失败详情", exc_info=True)
        # 未确认创建前回收邮箱；确认后避免重复使用。
        try:
            if email:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"Bit注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            data_saver.stop()
        if timeout_token is not None:
            try:
                _SELENIUM_TIMEOUT_OVERRIDE.reset(timeout_token)
            except Exception:
                pass
        if driver and not bool(_cfg.BIT_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if not bool(_cfg.BIT_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
