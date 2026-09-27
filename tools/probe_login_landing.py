"""探测某个邮箱在 OpenAI 登录链上的落点页，判断它当前是哪种账号状态。

用途：账号列表查活失败时，先确定「这个邮箱现在到底对应什么」。
典型场景：账号被换绑到新邮箱后，本地记录的主邮箱已失效——旧邮箱的落点页
看起来正常，但提交验证码会返回 400；此时用本工具探测新邮箱即可确认。

判定规则（落点页）：
  /log-in/password      → 已存在账号，服务端要求密码登录
  /email-verification   → 已存在账号，服务端要求邮箱 OTP
  create-account /about-you → 不是已注册账号（会走注册流程）
  含封禁关键词           → 账号已废弃

用法：
    .venv/bin/python tools/probe_login_landing.py <email> [<email> ...]
    .venv/bin/python tools/probe_login_landing.py --proxy "" <email>     # 强制直连
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.account_liveness import _network_preflight_with_retry  # noqa: E402
from core.openai_auth import detect_account_unusable_text, follow_authorize  # noqa: E402


def probe(email: str, proxy: str | None) -> str:
    """返回判定结论字符串。"""
    print(f"\n=== 探测 {email} ===", flush=True)
    session = None
    try:
        session, authorize_url = _network_preflight_with_retry(email, proxy, fingerprint_state={})
        final_url = follow_authorize(session, authorize_url)
        print("  落点页:", final_url, flush=True)

        dead = detect_account_unusable_text(final_url)
        if dead:
            verdict = f"账号已废弃 -> {dead}"
        elif "/log-in/password" in final_url:
            verdict = "已存在账号（服务端要求密码）"
        elif "email-verification" in final_url:
            verdict = "已存在账号（服务端要求邮箱 OTP，可能是已失效的旧邮箱）"
        elif "create-account" in final_url or "about-you" in final_url:
            verdict = "不是已注册账号（进入注册流程）"
        else:
            verdict = "未知，需人工确认"
        print("  判定:", verdict, flush=True)
        return verdict
    except Exception as exc:
        verdict = f"探测异常 {type(exc).__name__}: {str(exc)[:200]}"
        print(" ", verdict, flush=True)
        return verdict
    finally:
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="探测邮箱在 OpenAI 登录链上的落点页")
    ap.add_argument("emails", nargs="+", help="要探测的邮箱，可传多个")
    ap.add_argument("--proxy", default=None,
                    help="出口代理；不传=按代理池自动选路，传空串=强制直连")
    args = ap.parse_args()
    for addr in args.emails:
        probe(addr, args.proxy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
