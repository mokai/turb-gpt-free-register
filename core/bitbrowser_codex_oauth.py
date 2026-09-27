# -*- coding: utf-8 -*-
"""通过比特浏览器（BitBrowser）执行 Codex OAuth 授权。

页面授权流程复用 roxy_codex_oauth；这里只做浏览器适配：
- 独立运行时用 BitBrowserClient 创建/清理比特窗口
- 复用注册窗口时（reuse_existing_profile=True）行为与 Roxy 一致
"""
from __future__ import annotations

import logging

from config import bitbrowser as _cfg
from core.bitbrowser_client import BitBrowserClient

logger = logging.getLogger(__name__)


def _bit_codex_overrides() -> dict:
    return {
        "client_factory": BitBrowserClient,
        "selenium_timeout": int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90),
        "callback_timeout": int(getattr(_cfg, "BIT_CODEX_CALLBACK_TIMEOUT", 180) or 180),
        "keep_browser_open": bool(getattr(_cfg, "BIT_KEEP_BROWSER_OPEN", False)),
    }


def run_bit_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
) -> dict:
    """比特浏览器 Codex OAuth 入口；内部委托 roxy_codex_oauth 并挂上比特适配。"""
    from core.roxy_codex_oauth import _CODEX_BROWSER_OVERRIDES, run_roxy_codex_oauth

    token = _CODEX_BROWSER_OVERRIDES.set(_bit_codex_overrides())
    try:
        return run_roxy_codex_oauth(
            email,
            otp_provider=otp_provider,
            proxy=proxy,
            force=force,
            existing_driver=existing_driver,
            existing_opened=existing_opened,
            reuse_existing_profile=reuse_existing_profile,
            clear_existing_state=clear_existing_state,
        )
    finally:
        try:
            _CODEX_BROWSER_OVERRIDES.reset(token)
        except Exception:
            pass
