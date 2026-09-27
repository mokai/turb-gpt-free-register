# -*- coding: utf-8 -*-
"""套餐查询原始响应探针。

用途：套餐查询返回「无 Plus 试用资格」时，看服务端**原始** accounts/check 响应里
到底有没有 eligible_promo_campaigns.plus，以及账号条目（accounts 字典）的键、
出口 GeoIP 是什么。用于区分「解析漏字段」与「这个出口确实没活动」。

与正式查询走同一条链路（同样的 headers、可选 document 预热、同一出口选择方式），
唯一区别是把原始 JSON 落盘，便于逐字段核对。

用法（在项目根目录、用项目 venv）：
    ./.venv/bin/python tools/probe_plan_raw.py account@example.com --groups VN
    ./.venv/bin/python tools/probe_plan_raw.py account@example.com --groups VN,JP,IN
    ./.venv/bin/python tools/probe_plan_raw.py account@example.com --groups VN --no-warm

注意：本机 shell 带 HTTP_PROXY 时请用
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        ./.venv/bin/python tools/probe_plan_raw.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402
from core.chatgpt_plan import (  # noqa: E402
    ACCOUNTS_CHECK_PATH,
    _common_headers,
    _warm_plan_session,
    normalize_token,
    parse_accounts_check,
    token_claims,
)
from core.session import BrowserSession  # noqa: E402


def _dump_promo(item: dict) -> dict:
    campaigns = item.get("eligible_promo_campaigns")
    if not isinstance(campaigns, dict):
        campaigns = {}
    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    plus = campaigns.get("plus")
    return {
        "promo_keys": sorted(campaigns.keys()),
        "plus_raw": plus,
        "offer_ids": [o.get("id") for o in offers if isinstance(o, dict)],
        "features": len(item.get("features") or []),
        "can_access_with_session": item.get("can_access_with_session"),
    }


def _digest(data: dict) -> None:
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        print("!! 响应里没有 accounts 对象，顶层键 =", sorted(data.keys()))
        return
    print(f"accounts 条目: {len(accounts)} 个 -> {sorted(accounts.keys())}")
    for key, item in accounts.items():
        if not isinstance(item, dict):
            print(f"  [{key}] 非对象，跳过")
            continue
        account = item.get("account") or {}
        entitlement = item.get("entitlement") or {}
        print(f"  [{key}]")
        print(f"    account.account_id   = {account.get('account_id')}")
        print(f"    account.plan_type    = {account.get('plan_type')!r}")
        print(f"    entitlement.plan     = {entitlement.get('subscription_plan')!r}")
        print(f"    has_active_subscription = {entitlement.get('has_active_subscription')!r}")
        print(f"    discount.promo_campaign_id = {(entitlement.get('discount') or {}).get('promo_campaign_id')!r}")
        info = _dump_promo(item)
        print(f"    eligible_promo_campaigns keys = {info['promo_keys']}")
        plus = info["plus_raw"]
        if isinstance(plus, dict):
            meta = plus.get("metadata") or {}
            print(f"    plus.id = {plus.get('id')!r}  metadata.discount={meta.get('discount')!r} duration={meta.get('duration')!r}")
        else:
            print(f"    plus = {plus!r}")
        print(f"    eligible_offers = {info['offer_ids']}")
        print(f"    features={info['features']} can_access_with_session={info['can_access_with_session']!r}")


def probe(email: str, group: str, *, warm: bool, tz: str, dump_dir: Path, geo: bool = True) -> None:
    acc = db.get_account_by_email(email)
    if not acc:
        print(f"!! 本地库里找不到账号 {email}")
        return
    token = normalize_token(acc.get("access_token") or "")
    if not token:
        print(f"!! {email} 没有 access_token")
        return

    from config.proxy import pick_proxy, resolve_group_pool

    pool = resolve_group_pool(group)
    if not pool:
        print(f"!! 代理分组 「{group}」 没有可用出口（检查 .env 的 PROXY_GROUPS）")
        return
    proxy = str(pick_proxy(pool=pool) or pool[0])

    print("=" * 72)
    print(f"账号 {email}  分组={group}  池大小={len(pool)}")
    claims = token_claims(token)
    print(f"JWT: email={claims.get('email')} account_id={claims.get('account_id')} claim_plan={claims.get('claim_plan_type')} expired={claims.get('token_expired')}")

    env: BrowserSession | None = None
    try:
        env = BrowserSession(
            proxy=proxy, detect_exit_geo=geo,
            fingerprint_seed=f"probe-plan:{email}:{uuid.uuid4()}",
        )
        g = env.exit_geo or {}
        print(f"出口: ip={g.get('ip')} country={g.get('country')} city={g.get('city')} "
              f"timezone={g.get('timezone')} org={g.get('org')} (detect_exit_geo={geo})")
        print(f"画像: {env.fingerprint_summary_text()}")
        effective_tz = str(tz or "").strip()
        if not effective_tz or effective_tz == "-":
            effective_tz = str(env.js_timezone_offset_min())
        if warm:
            _warm_plan_session(env)

        url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={effective_tz}"
        resp = env.get(url, headers=_common_headers(env, token, claims),
                       allow_redirects=False, timeout=20.0)
        text = resp.text or ""
        print(f"HTTP {resp.status_code}  cf-mitigated={(resp.headers or {}).get('cf-mitigated')!r}  tz={effective_tz}  bytes={len(text)}")

        dump_dir.mkdir(parents=True, exist_ok=True)
        out = dump_dir / f"plan-raw-{email.split('@')[0]}-{group}.json"
        out.write_text(text, encoding="utf-8")
        print(f"原始响应已保存: {out}")

        try:
            data = resp.json()
        except Exception:
            data = json.loads(text) if text.strip().startswith(("{", "[")) else None
        if not isinstance(data, dict):
            print("!! 响应不是 JSON 对象，前 300 字：")
            print(text[:300])
            return
        _digest(data)
        parsed = parse_accounts_check(data, token=token)
        print(f"解析结果: plan={parsed.get('current_plan_type')!r} "
              f"plus_trial_eligible={parsed.get('plus_trial_eligible')!r} "
              f"campaign={parsed.get('plus_trial_campaign_id')!r}")
    except Exception as exc:
        print(f"!! 请求失败: {type(exc).__name__}: {exc}")
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def via_service(email: str, groups: list[str], repeat: int) -> None:
    """直接调用 check_account_plan（与后台队列同一条路径），打印每次的 tz / 出口 / 结果。"""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from config.proxy import resolve_group_pool
    from core.chatgpt_plan import check_account_plan

    acc = db.get_account_by_email(email)
    if not acc:
        print(f"!! 本地库里找不到账号 {email}")
        return
    token = normalize_token(acc.get("access_token") or "")
    for i in range(repeat):
        for group in groups:
            pool = resolve_group_pool(group)
            res = check_account_plan(token, pool=pool, timezone_offset_min="-")
            print(f"  [service#{i + 1}] group={group} ok={res.get('ok')} "
                  f"plan={res.get('current_plan_type')!r} trial={res.get('plus_trial_eligible')!r} "
                  f"tz={res.get('timezone_offset_min')!r} proxy_used={res.get('proxy_used')!r} "
                  f"err={res.get('error')!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="套餐查询原始响应探针")
    ap.add_argument("email", help="账号邮箱")
    ap.add_argument("--groups", default="VN", help="代理分组名，逗号分隔（默认 VN）")
    ap.add_argument("--tz", default="-", help="timezone_offset_min，默认按出口自动")
    ap.add_argument("--no-warm", action="store_true", help="跳过 chatgpt.com document 预热")
    ap.add_argument("--no-geo", action="store_true",
                    help="关闭出口地理探测（复现 GEO 探测失败时回退默认 BROWSER_LOCALE_PROFILE 的场景）")
    ap.add_argument("--dump-dir", default="logs/plan-raw", help="原始响应保存目录")
    ap.add_argument("--via-service", action="store_true",
                    help="直接调用 check_account_plan（后台队列同路径），打印 tz/出口/结果")
    ap.add_argument("--repeat", type=int, default=1, help="配合 --via-service 的重复次数")
    args = ap.parse_args()

    dump_dir = Path(args.dump_dir)
    groups = [g.strip() for g in str(args.groups).split(",") if g.strip()]
    if args.via_service:
        via_service(args.email, groups, max(1, args.repeat))
        return 0
    for group in groups:
        probe(args.email, group, warm=not args.no_warm, tz=args.tz,
              dump_dir=dump_dir, geo=not args.no_geo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
