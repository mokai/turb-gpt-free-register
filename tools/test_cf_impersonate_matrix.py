#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare 指纹体检：定位 /auth/login 的 403 到底该怪代理还是该怪 TLS 指纹。

背景（2026-09-21 实测）：
    查活失败日志里 /auth/login 顶层导航返回 403，很容易被误判成"代理出口被 CF 封了"，
    于是不停换 IP、加长重试。实际上 curl_cffi 的 impersonate 目标选择错误时，
    Cloudflare 同样会对**任何干净出口**下发 JS 挑战（403 + cf-mitigated=challenge），
    此时同一会话原地重试只会稳定复现 403。
    同一时刻同一出口下：chrome142 → 200，而 chrome146/145/136/131/133a → 403。

用途：
    1. 对当前 config.IMPERSONATE 做端到端体检（走查活同款导航头，走/不走代理各一次）；
    2. 横向扫描 curl_cffi 内置指纹，找出现在能被 CF 接受的目标。

用法：
    # 只体检当前配置（沿用 .env / config 里的代理策略）
    python3 tools/test_cf_impersonate_matrix.py

    # 指定出口代理一起体检
    python3 tools/test_cf_impersonate_matrix.py --proxy 'http://user:pass@host:port'

    # 强制直连，并扩大扫描范围
    python3 tools/test_cf_impersonate_matrix.py --proxy '' --scan-all

退出码：0 = 当前配置通过；1 = 当前配置被 CF 挑战。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TARGET = "https://chatgpt.com/auth/login"

# 常用候选；--scan-all 时改为扫描 curl_cffi 全部内置目标。
DEFAULT_CANDIDATES = [
    "chrome", "chrome142", "chrome145", "chrome146", "chrome150",
    "chrome136", "chrome133a", "chrome131", "chrome120",
    "safari", "safari184", "firefox", "edge101",
]


def _proxy_label(proxy: str | None) -> str:
    if proxy == "":
        return "直连"
    if proxy:
        return proxy
    return "按配置选路"


def _describe(resp) -> str:
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return (
        f"http={resp.status_code} cf-mitigated={headers.get('cf-mitigated') or '-'} "
        f"server={headers.get('server')} len={len(resp.text)}"
    )


def check_current_config(proxy: str | None) -> bool:
    """走查活同款 BrowserSession + 导航头，体检当前 IMPERSONATE。"""
    import config
    from core.session import BrowserSession

    print("=" * 78)
    print(f"当前配置：IMPERSONATE={config.IMPERSONATE} CHROME_MAJOR={config.CHROME_MAJOR}")
    print(f"          UA={config.USER_AGENT}")
    print(f"          出口={_proxy_label(proxy)}")
    print("=" * 78)

    session = BrowserSession(proxy=proxy, detect_exit_geo=True)
    print(f"画像：{session.fingerprint_summary_text()}")
    try:
        resp = session.get(
            TARGET,
            headers=session.get_chatgpt_navigate_headers(referer="", user_initiated=True),
            allow_redirects=True,
            timeout=15,
        )
    except Exception as exc:
        print(f"结论：请求异常 {type(exc).__name__}: {str(exc)[:200]}")
        print("      请先确认出口本身可达（超时通常是网络/代理问题，不是指纹问题）。")
        return False

    print(f"结果：{_describe(resp)}")
    ok = resp.status_code == 200
    if ok:
        print("结论：[通过] 当前指纹未被 Cloudflare 挑战，/auth/login 返回 200。")
    else:
        print("结论：[失败] 当前指纹被 Cloudflare 挑战。")
        print("      不要继续换 IP；先跑下面的扫描换一个能过的 impersonate。")
    return ok


def scan(proxy: str | None, scan_all: bool) -> None:
    """横向扫描候选指纹，输出哪些能过。"""
    from curl_cffi import requests as cr

    candidates = list(DEFAULT_CANDIDATES)
    if scan_all:
        try:
            import typing

            from curl_cffi.requests.impersonate import BrowserTypeLiteral

            candidates = sorted(set(typing.get_args(BrowserTypeLiteral)))
        except Exception as exc:
            print(f"（读取内置目标失败，退回默认候选：{type(exc).__name__}）")

    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    print()
    print("=" * 78)
    print(f"指纹扫描：出口={_proxy_label(proxy)} 候选={len(candidates)} 个")
    print("=" * 78)
    passed: list[str] = []
    for name in candidates:
        try:
            session = cr.Session(impersonate=name)
            resp = session.get(TARGET, timeout=15, proxies=proxies, allow_redirects=True)
            print(f"  {name:16s} {_describe(resp)}")
            if resp.status_code == 200:
                passed.append(name)
            session.close()
        except Exception as exc:
            print(f"  {name:16s} 异常 {type(exc).__name__}: {str(exc)[:100]}")

    print("-" * 78)
    if passed:
        print(f"可通过 Cloudflare 的目标：{', '.join(passed)}")
        print("建议：从上面结果里选一个主版本号较大且稳定的，同步更新")
        print("      config/browser.py 的 IMPERSONATE / CHROME_MAJOR / CHROME_FULL_VERSION")
        print("      / SEC_CH_UA / SEC_CH_UA_FULL_VERSION_LIST，然后重跑本脚本确认。")
    else:
        print("没有任何候选通过：更可能是出口 IP 被 CF 拉黑，换出口再试。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloudflare 指纹体检（/auth/login）")
    parser.add_argument(
        "--proxy",
        default=None,
        help="代理地址；不传=按配置选路，传空字符串=''=强制直连",
    )
    parser.add_argument("--scan-all", action="store_true", help="扫描 curl_cffi 全部内置指纹")
    parser.add_argument("--no-scan", action="store_true", help="只体检当前配置，不做指纹扫描")
    args = parser.parse_args()

    ok = check_current_config(args.proxy)
    if not args.no_scan:
        scan(args.proxy, args.scan_all)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
