#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按出口体检 `auth.openai.com/api/accounts/authorize` 的 Cloudflare 挑战情况。

背景（2026-10-07 实测）：
    换绑 / 查活的**致命跳**是 `auth.openai.com/api/accounts/authorize`。
    在同一个 `chrome146` 指纹 + 同一套项目自管 UA/Client Hints 下，**结果只跟出口 IP 有关**：
      健康：IN:Surat / IN:Delhi / 直连(JP)      → 200
      被挑战：IN:Shillong / Bengaluru / Ghaziabad / Varanasi / Ludhiana
              → 403 + `cf-mitigated=challenge`
    而 `tools/test_cf_impersonate_matrix.py` 只体检 `/auth/login`，**测不出这一跳的差异**
    （`/auth/login` 文档导航被挑战是常态，代码已容忍它，不是致命点）。

    所以本工具专门体检 authorize：**每个出口一个干净会话 → 单次文档导航 → 分类结果**。

用法:
    .venv/bin/python tools/probe_authorize_health.py                    # 体检默认/任务分组池
    .venv/bin/python tools/probe_authorize_health.py --limit 20 --workers 4
    .venv/bin/python tools/probe_authorize_health.py --group VN
    .venv/bin/python tools/probe_authorize_health.py --direct            # 直连也算一个候选
    .venv/bin/python tools/probe_authorize_health.py --emit-healthy /tmp/healthy_exits.txt
    .venv/bin/python tools/probe_authorize_health.py --json /tmp/authorize_health.json

输出里的出口只显示 `host:port` + `country/session` 标记，**不回显凭据**；
`--emit-healthy` 写出的文件才是可直接粘回代理池的完整 URL。

退出码：0 = 至少一个出口健康；1 = 全部被挑战 / 全部失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

LOGIN_PAGE = "https://chatgpt.com/auth/login"
VERDICT_HEALTHY = "健康"
VERDICT_CHALLENGED = "CF 挑战"
VERDICT_403 = "403(非挑战)"
VERDICT_OTHER = "其它状态"
VERDICT_ERROR = "请求异常"


def build_authorize_url() -> str:
    """用项目真实协议常量拼一个 authorize URL（CF 判定发生在业务逻辑之前，state 可任意）。"""
    from config import openai_protocol as proto

    query = {
        "client_id": proto.OPENAI_CLIENT_ID,
        "scope": proto.OPENAI_SCOPE,
        "response_type": "code",
        "redirect_uri": proto.OPENAI_REDIRECT_URI,
        "audience": proto.OPENAI_AUDIENCE,
        "ui_locales": "en-US",
        "prompt": "login",
        "screen_hint": "login_or_signup",
        "ccaps": "login_methods chatgpt_login_finalizer_v1",
        "auth_return_target_category": "chatgpt_home",
        "state": "probe-authorize-health",
    }
    return "https://auth.openai.com/api/accounts/authorize?" + urlencode(query, quote_via=quote)


def mask_proxy(proxy: str) -> str:
    """把代理 URL 压成可读标记，绝不回显用户名/密码。"""
    if not proxy:
        return "直连"
    try:
        parts = urlsplit(proxy)
        user = parts.username or ""
        country = re.search(r"country[_-]([A-Za-z]{2})", user)
        session = re.search(r"session[_-]([A-Za-z0-9]+)", user)
        tags = []
        if country:
            tags.append(country.group(1).upper())
        if session:
            tags.append(f"session-{session.group(1)[:8]}")
        host = parts.hostname or "<proxy>"
        suffix = f"  [{'/'.join(tags)}]" if tags else ""
        return f"{host}:{parts.port or ''}{suffix}"
    except Exception:
        return "<proxy>"


def classify(status: int | None, cf_mitigated: str, error: str) -> str:
    if status is None:
        return VERDICT_ERROR
    if status == 200:
        return VERDICT_HEALTHY
    if status == 403 and (cf_mitigated or "").lower() == "challenge":
        return VERDICT_CHALLENGED
    if status == 403:
        return VERDICT_403
    return VERDICT_OTHER


def probe_one(index: int, proxy: str, url: str, detect_geo: bool) -> dict:
    """干净会话单次打 authorize。返回结构化结果，不抛异常。"""
    from core.session import BrowserSession

    item: dict = {
        "index": index,
        "proxy": proxy,
        "label": mask_proxy(proxy),
        "geo": "",
        "status": None,
        "cf_mitigated": "",
        "verdict": VERDICT_ERROR,
        "detail": "",
    }
    session = None
    try:
        session = BrowserSession(proxy=proxy, detect_exit_geo=detect_geo)
        try:
            text = session.fingerprint_summary_text()
            geo = text.split("geo=")[-1] if "geo=" in text else ""
            item["geo"] = geo.strip()
        except Exception:
            pass
        resp = session.get(
            url,
            headers=session.get_auth_navigate_headers(referer="https://chatgpt.com/"),
            allow_redirects=True,
            timeout=15,
        )
        headers = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
        item["status"] = int(getattr(resp, "status_code", 0) or 0)
        item["cf_mitigated"] = str(headers.get("cf-mitigated") or "")
        item["detail"] = f"len={len(getattr(resp, 'text', '') or '')}"
    except Exception as exc:
        item["detail"] = f"{type(exc).__name__}: {str(exc)[:110]}"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    item["verdict"] = classify(item["status"], item["cf_mitigated"], item["detail"])
    return item


def collect_candidates(args) -> list[str]:
    """按 --proxy / --group / 默认池收集候选出口（保序去重）。

    ``--limit`` 只截断代理池部分，``--direct`` 追加的直连永远保留（常用来做对照）。
    """
    from config import proxy as proxy_cfg

    raw: list[str] = []
    if args.proxy:
        raw.extend(args.proxy)
    elif args.group:
        raw.extend(proxy_cfg.proxy_group_pool(args.group))
    else:
        task_pool = proxy_cfg.task_pool_active()
        raw.extend(list(task_pool) if task_pool else list(proxy_cfg.PROXY_POOL))

    seen: set[str] = set()
    candidates: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(key)
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]
    if args.direct:
        candidates.append("")
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="体检出口对 auth.openai.com/authorize 的 CF 挑战")
    parser.add_argument("--proxy", action="append", default=None,
                        help="指定出口（可重复；空字符串 '' 表示直连）。给了就不再读代理池")
    parser.add_argument("--group", default="", help="体检指定代理分组（如 VN / IN）")
    parser.add_argument("--direct", action="store_true", help="把直连也作为一个候选")
    parser.add_argument("--limit", type=int, default=25, help="最多体检多少条（0=不限，默认 25）")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    parser.add_argument("--no-geo", action="store_true", help="跳过出口地区探测（更快）")
    parser.add_argument("--emit-healthy", default="", help="把健康出口的完整 URL 写到该文件")
    parser.add_argument("--json", dest="json_path", default="", help="把完整结果写到该 JSON 文件")
    parser.add_argument("--verbose", action="store_true", help="打印底层 [CF] 日志")
    args = parser.parse_args()

    if not args.verbose:
        logging.getLogger("core").setLevel(logging.ERROR)
        logging.getLogger("config").setLevel(logging.ERROR)

    url = build_authorize_url()
    candidates = collect_candidates(args)
    if not candidates:
        print("没有候选出口：代理池为空，或 --proxy/--group 没给到有效值。")
        return 1

    print("=" * 78)
    print("出口体检：GET https://auth.openai.com/api/accounts/authorize（干净会话·单次）")
    print(f"候选 {len(candidates)} 条，并发 {max(1, args.workers)}，地区探测={'关' if args.no_geo else '开'}")
    print("=" * 78)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(probe_one, i + 1, proxy, url, not args.no_geo)
            for i, proxy in enumerate(candidates)
        ]
        for fut in futures:
            try:
                results.append(fut.result())
            except Exception as exc:  # 理论上不会发生，probe_one 自身兜底
                results.append({
                    "index": 9999, "proxy": "", "label": "<unknown>", "geo": "",
                    "status": None, "cf_mitigated": "", "verdict": VERDICT_ERROR,
                    "detail": f"{type(exc).__name__}: {str(exc)[:110]}",
                })
    results.sort(key=lambda r: r["index"])

    print(f"{'#':>3}  {'出口':38s} {'地区':14s} {'状态':>4}  {'cf-mitigated':13s} 判定")
    print("-" * 78)
    for item in results:
        status = item["status"] if item["status"] is not None else "-"
        print(
            f"{item['index']:>3}  {item['label'][:38]:38s} {item['geo'][:14]:14s} "
            f"{str(status):>4}  {item['cf_mitigated'][:13]:13s} {item['verdict']}"
        )
        if item["verdict"] in (VERDICT_ERROR, VERDICT_OTHER) and item["detail"]:
            print(f"      └─ {item['detail']}")
    print("-" * 78)

    healthy = [r for r in results if r["verdict"] == VERDICT_HEALTHY]
    challenged = [r for r in results if r["verdict"] == VERDICT_CHALLENGED]
    failed = [r for r in results if r["verdict"] == VERDICT_ERROR]
    print(
        f"健康 {len(healthy)} / CF挑战 {len(challenged)} / 请求异常 {len(failed)} "
        f"/ 其它 {len(results) - len(healthy) - len(challenged) - len(failed)}"
    )
    if healthy:
        rate = len(healthy) / len(results) * 100
        print(f"健康率 {rate:.0f}%。换绑按 {rate / 100:.2f} 单出口成功率折算："
              f"4 条路线上限的全灭概率约 {((1 - rate / 100) ** 4 * 100):.0f}%，"
              f"6 条约 {((1 - rate / 100) ** 6 * 100):.0f}%。")
    else:
        print("没有任何出口健康：先换代理供应商/段，再谈指纹与重试。")

    if args.emit_healthy:
        target = pathlib.Path(args.emit_healthy)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(r["proxy"] for r in healthy) + ("\n" if healthy else ""),
                          encoding="utf-8")
        print(f"已写出 {len(healthy)} 条健康出口 → {target}")

    if args.json_path:
        target = pathlib.Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整结果 → {target}")

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
