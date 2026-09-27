# -*- coding: utf-8 -*-
"""Cloudflare 风控人工处理：端到端浏览器冒烟（in-process Flask + Playwright）。

为什么必须在**同一个进程**里起 Flask：人工介入的待办表是后端进程内存里的
（`core/manual_intervention._entries`），跨进程没法种数据。这里直接
`make_server()` + `serve_forever()` 起一个临时端口的 WebUI，再让 Playwright 打进来，
所以前端轮询到的是真实接口数据。

覆盖（控制台错误数必须为 0）：
- 弹框自动出现、字段齐全（类型/任务/邮箱/当前页面/操作指引/复检状态/剩余时间）；
- 布局：标题与底部按钮都在视口内、弹框自身不滚动、背景页不滚动；
- 「我已处理完成」→ 状态区更新；页面恢复 → 绿底 + 隐藏操作按钮 + grace 后弹框自动关闭；
- 「取消本次任务」→ 弹框关闭、请求标记取消；「稍后处理」→ 收起但请求仍在；
- 任务表 `.cf-badge`「风控待处理」→ 点击重新打开弹框；
- 配置页：「风控 / 人工处理」分组与「注册方式 → 人工介入超时时间」都渲染出来。

用法::

    .venv/bin/python tools/smoke_cf_manual_intervention.py [--port 5099] [--headed]

截图输出到 `docs/cf-manual-intervention-*.png`。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 本机 shell 常带 HTTP_PROXY（宿主/沙箱代理），会把 127.0.0.1 的请求也带走。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

AUTH = "test-auth"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5099)
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认 headless）")
    args = parser.parse_args()

    from werkzeug.serving import make_server

    from core import manual_intervention as mi
    from webui.app import create_app

    shots_dir = ROOT / "docs"
    shots_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(auth_code=AUTH)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.0)

    mi.mark_pending(
        kind="cloudflare_challenge",
        title="需要你手动完成 Cloudflare 验证",
        guide=(
            "页面弹出了 Cloudflare 人机验证（“Verify you are human”）。请切到正在运行的指纹浏览器窗口，"
            "在那张提示框里点一下复选框，只要验证通过，脚本会自动检测到并继续下一步。"
        ),
        url="https://chatgpt.com/auth/login",
        email="demo-cf@example.com",
        job_id=123,
        task="注册",
        timeout=600,
        poll_interval=5,
    )

    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    results: list[tuple[str, bool, str]] = []
    shots: list[pathlib.Path] = []

    def check(name: str, ok, extra: str = "") -> None:
        results.append((name, bool(ok), extra))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())

            page.goto(f"http://127.0.0.1:{args.port}/login", wait_until="domcontentloaded")
            page.fill("#authCodeInput", AUTH)
            page.click("form button[type=submit]")
            page.wait_for_url(re.compile(r"/$|/index"), timeout=20000)

            # 1) 弹框自动弹出（前端 3s 轮询）
            page.wait_for_selector("#manualInterventionPanel:not(.hidden)", timeout=15000)
            check("弹框自动弹出", True)

            panel = page.locator("#manualInterventionPanel")
            head_box = page.locator("#manualInterventionPanel h3").bounding_box()
            foot_box = page.locator("#manualInterventionPanel .mi-foot").bounding_box()
            body_box = page.locator("#manualInterventionPanel .mi-body").bounding_box()
            inner = page.evaluate(
                "() => {const pl = document.querySelector('#manualInterventionPanel');"
                " return {scrollH: pl.scrollHeight, clientH: pl.clientHeight, vh: window.innerHeight,"
                " pageScroll: document.documentElement.scrollHeight, pageClient: document.documentElement.clientHeight};}"
            )
            check("标题在视口内", head_box["y"] >= 0, str(head_box))
            check("底部按钮在视口内", foot_box["y"] + foot_box["height"] <= inner["vh"] + 1, str(foot_box))
            check("弹框自身不滚动", abs(inner["scrollH"] - inner["clientH"]) <= 1, str(inner))
            check("正文区可滚动", body_box["height"] > 100, str(body_box))
            check("背景页面不滚动", inner["pageScroll"] == inner["pageClient"])

            text = panel.inner_text()
            check("显示类型", "Cloudflare 风控验证" in text)
            check("显示任务号", "#123" in text and "注册" in text)
            check("显示邮箱", "demo-cf@example.com" in text)
            check("显示当前页面地址", "chatgpt.com/auth/login" in text)
            check("显示操作指引", "指纹浏览器窗口" in text)
            check("显示复检状态", "等待" in text or "复检" in text)
            check("显示剩余时间", "秒后超时" in text)

            shots.append(shots_dir / "cf-manual-intervention-panel-preview.png")
            panel.screenshot(path=str(shots[-1]))

            # 2) 「我已处理完成」→ 后端立即复检并回写状态
            page.click("#btnManualInterventionAck")
            page.wait_for_function(
                "() => (document.querySelector('#manualInterventionStatus')||{}).textContent.includes('确认')"
                " || (document.querySelector('#manualInterventionStatus')||{}).textContent.includes('复检')",
                timeout=8000,
            )
            check("确认后状态区更新", True)

            # 3) 页面恢复 → 绿底 + 隐藏操作按钮
            item = mi.list_pending()[0]
            mi.note_status(
                item["request_id"], status=mi.STATUS_RECOVERED,
                status_text="页面已恢复正常，流程继续", detail="当前页面：登录页",
            )
            page.wait_for_function(
                "() => document.querySelector('#manualInterventionStatus').classList.contains('is-ok')",
                timeout=8000,
            )
            check("恢复后状态变绿", True)
            check(
                "恢复后隐藏操作按钮",
                page.locator("#btnManualInterventionAck").is_hidden()
                and page.locator("#btnManualInterventionCancel").is_hidden(),
            )
            shots.append(shots_dir / "cf-manual-intervention-recovered.png")
            panel.screenshot(path=str(shots[-1]))

            # 4) 终态请求在 grace(10s) 后被清理 → 弹框自动关闭
            page.wait_for_function(
                "() => document.querySelector('#manualInterventionPanel').classList.contains('hidden')",
                timeout=30000,
            )
            check("已恢复后弹框自动关闭", True)

            # 5) 取消路径
            cancel_item = mi.mark_pending(
                kind="cloudflare_challenge", title="需要你手动完成 Cloudflare 验证",
                guide="请手动处理", url="https://chatgpt.com/auth/login",
                email="demo-cancel@example.com", job_id=124, task="注册", timeout=600,
            )
            page.wait_for_selector("#manualInterventionPanel:not(.hidden)", timeout=15000)
            page.click("#btnManualInterventionCancel")
            page.wait_for_function(
                "() => document.querySelector('#manualInterventionPanel').classList.contains('hidden')",
                timeout=15000,
            )
            check("取消后弹框关闭", True)
            check("取消状态已记录", (mi.get_pending(cancel_item["request_id"]) or {}).get("cancelled") is True)

            # 6) 「稍后处理」只收起弹框，请求仍在等待
            later_item = mi.mark_pending(
                kind="cloudflare_challenge", title="需要你手动完成 Cloudflare 验证",
                guide="请手动处理", url="https://chatgpt.com/auth/login",
                email="demo-later@example.com", job_id=125, timeout=600,
            )
            page.wait_for_selector("#manualInterventionPanel:not(.hidden)", timeout=15000)
            page.click("#btnCloseManualIntervention")
            check("稍后处理可收起弹框", page.locator("#manualInterventionPanel").is_hidden())
            check("收起后请求仍在等待", mi.get_pending(later_item["request_id"]) is not None)

            # 7) 任务表「风控待处理」徽标 → 点击重开弹框
            badge = page.evaluate(
                """() => {
                    JOBS = [{id: 125, status: 'running', display_status: 'running', email: 'demo-later@example.com'}];
                    JOBS_TOTAL = 1;
                    renderJobs();
                    const b = document.querySelector('#jobsBodyV2 .cf-badge');
                    return b ? b.textContent.trim() : null;
                }"""
            )
            check("任务行显示风控徽标", badge == "风控待处理", str(badge))
            page.click("#jobsBodyV2 .cf-badge")
            page.wait_for_selector("#manualInterventionPanel:not(.hidden)", timeout=8000)
            check("点徽标重新打开弹框", True)
            page.click("#manualInterventionPanel #btnCloseManualIntervention")

            # 8) 配置页：风控分组 + 注册方式里的人工介入超时（分组有专用渲染器，必须查 DOM 文本）
            page.click('[data-tab="config"]')
            page.wait_for_selector("#configNavV2 .config-nav-v2-item", timeout=15000)
            nav_text = page.inner_text("#configNavV2")
            body_text = page.inner_text("#configSectionsV2")
            check("配置页出现风控分组", "风控 / 人工处理" in nav_text, nav_text[:200])
            check("注册方式分组仍在", "注册方式" in nav_text)
            check("风控总开关字段存在", "Cloudflare 风控页转人工处理" in body_text)
            check("恢复检测间隔字段存在", "恢复检测间隔(秒)" in body_text)
            check("人工介入超时字段存在", "人工介入超时时间" in body_text)
            check("人工介入超时已不在邮箱分组", "人工输入等待(秒)" not in body_text)
            check("注册方式特写含超时字段", "人工介入超时时间" in page.locator("#cfg-注册方式").inner_text())

            shots.append(shots_dir / "cf-manual-intervention-config.png")
            page.screenshot(path=str(shots[-1]))
            page.locator("#cfg-注册方式").scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            shots.append(shots_dir / "cf-manual-intervention-config-register.png")
            page.locator("#cfg-注册方式").screenshot(path=str(shots[-1]))
            page.locator("#cfg-风控-人工处理").scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            shots.append(shots_dir / "cf-manual-intervention-config-risk.png")
            page.locator("#cfg-风控-人工处理").screenshot(path=str(shots[-1]))

            browser.close()
    finally:
        server.shutdown()

    print("=== 结果 ===")
    for name, ok, extra in results:
        print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra and not ok else ""))
    failed = [n for n, ok, _ in results if not ok]
    print("失败数：%d / %d" % (len(failed), len(results)))
    print("控制台错误：%d" % len(errors))
    for e in errors[:5]:
        print("  -", e[:300])
    print("截图：")
    for s in shots:
        print("  ", s)
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
