"""验证「某账号的注册密码」是否能登录「另一个邮箱」，判定两者是否为同一账号。

背景：账号换绑邮箱后，本地记录的主邮箱会过期，但账号本身仍然存活。换绑前后
密码不变，因此「用旧账号的注册密码去验证新邮箱」是判定归属关系最可靠的方法
（比备注、比邮箱池状态都可靠）。本工具只发网络请求，不改动任何本地数据。

用法：
    .venv/bin/python tools/probe_account_email_linkage.py <旧邮箱> <待验证邮箱>

判定：
  * 新邮箱落点是 /log-in/password 且密码校验通过 → 同一账号（换绑后的新邮箱）
  * 密码校验 400/401                          → 很可能不是同一账号
  * 新邮箱落点是 email-verification / 注册流程  → 该邮箱不是密码账号，无法判定
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.account_liveness import (  # noqa: E402
    _extract_continue_url,
    _extract_factor_id,
    _network_preflight_with_retry,
    _password_verify,
)
from core.codex_oauth import _account_registration_password, _account_totp_secret  # noqa: E402
from core.openai_auth import follow_authorize  # noqa: E402


def probe(old_email: str, candidate_email: str, proxy: str | None) -> bool:
    """返回 True 表示确认两者是同一个账号。"""
    password = _account_registration_password(old_email)
    print(f"旧邮箱   = {old_email}")
    print(f"候选邮箱 = {candidate_email}")
    print(f"注册密码长度 = {len(password)} | TOTP = {'有' if _account_totp_secret(old_email) else '无'}")
    if not password:
        print("该账号没有本地注册密码，无法用密码做归属判定；改用 tools/probe_login_landing.py 看落点页。")
        return False

    session = None
    try:
        session, authorize_url = _network_preflight_with_retry(candidate_email, proxy, fingerprint_state={})
        final_url = follow_authorize(session, authorize_url)
        print("  候选邮箱落点页:", final_url)
        if "/log-in/password" not in final_url:
            print("  该邮箱未要求密码登录，无法用密码判定归属。")
            return False

        result = _password_verify(session, password)
        continue_url = _extract_continue_url(result)
        page = result.get("page") if isinstance(result, dict) else {}
        page = page if isinstance(page, dict) else {}
        print(f"  密码校验通过 ✔  page_type={page.get('type')} continue_url={continue_url}")
        if "/mfa-challenge/" in continue_url or page.get("type") == "mfa_challenge":
            print("  进入 MFA challenge，factor_id =", _extract_factor_id(result, continue_url))
        print("  结论：该密码对候选邮箱有效 → 两者是同一个账号")
        return True
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        print(f"  密码校验失败 ✗ status={status} {type(exc).__name__}: {str(exc)[:200]}")
        print("  结论：该密码对候选邮箱无效 → 很可能不是同一个账号")
        return False
    finally:
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="用注册密码验证邮箱归属关系")
    ap.add_argument("old_email", help="账号本地记录的主邮箱")
    ap.add_argument("candidate_email", help="待验证的候选邮箱（如换绑后的新邮箱）")
    ap.add_argument("--proxy", default=None,
                    help="出口代理；不传=按代理池自动选路，传空串=强制直连")
    args = ap.parse_args()
    ok = probe(args.old_email, args.candidate_email, args.proxy)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
