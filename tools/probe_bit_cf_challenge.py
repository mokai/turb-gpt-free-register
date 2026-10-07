# -*- coding: utf-8 -*-
"""比特浏览器 Cloudflare 风控页体检。

建一个与生产完全一致的临时比特窗口（走 config/proxy 的代理池 + 随机指纹），
导航到目标地址，然后**每 N 秒采样一次**页面状态，直到出现真实页面元素。

用途：判断「页面被 CF 托管挑战」到底是
  (a) 会自己放行、只是流程等得太短  → 改等待预算即可；
  (b) 一直不放行 / 出现拦截页(1020) → 才需要换出口 IP 或换环境。

用法：
    ./.venv/bin/python tools/probe_bit_cf_challenge.py
    ./.venv/bin/python tools/probe_bit_cf_challenge.py --url https://chatgpt.com/ --wait 90
    ./.venv/bin/python tools/probe_bit_cf_challenge.py --interval 2 --json /tmp/cf.json
    ./.venv/bin/python tools/probe_bit_cf_challenge.py --guard              # 顺带验证 CF 守卫
    ./.venv/bin/python tools/probe_bit_cf_challenge.py --guard --guard-grace 0   # 强制走弹框分支

``--guard`` 会直接调用生产用的 ``core/cf_challenge.handle()``，验证"检测 → 静默等自动放行
（或登记人工介入）→ 恢复后返回 True"这条链路在真实窗口里是否成立。
本进程是独立进程，人工介入登记表在内存里，**不会**在 WebUI 上弹框打扰用户。

注意：
- 打本地比特 API 需要回环直连，启动前清掉 HTTP(S)_PROXY 且 no_proxy 只写回环：
      env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
          no_proxy='127.0.0.1,localhost,::1' ./.venv/bin/python tools/probe_bit_cf_challenge.py
- 跑完自动关窗 + 删窗；不会消耗邮箱、不影响正在跑的任务。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

from core.bitbrowser_client import BitBrowserClient, BitOpenResult  # noqa: E402
from core.roxy_registration import _build_driver  # noqa: E402

DEFAULT_URL = "https://chatgpt.com/auth/login"

POLL_JS = r"""
const out = {};
out.url = location.href;
out.title = document.title;
out.navLang = navigator.language;
out.navLangs = (navigator.languages || []).join('|');
out.tz = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '';
out.ua = navigator.userAgent;
out.cfOpt = !!window._cf_chl_opt;
out.challengeEl = !!document.querySelector('#challenge-running, #challenge-stage, #cf-chl-widget, .cf-turnstile, [id^="cf-chl"], #challenge-form');
out.turnstile = !!document.querySelector('iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]');
try {
  out.cfScripts = Array.prototype.slice.call(document.scripts)
      .map(function (s) { return s.src; }).filter(function (s) { return /cdn-cgi|challenge|turnstile/.test(s); });
} catch (e) { out.cfScripts = []; }
out.inputCount = document.querySelectorAll('input').length;
out.hasEmail = !!document.querySelector('input[type=email], input[name=email], #email, input[id*=email], input[autocomplete*=email]');
try { out.bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 160); } catch (e) { out.bodyText = ''; }
return out;
"""

ASYNC_IP = r"""
const cb = arguments[arguments.length - 1];
fetch('https://api.ipify.org?format=json', {cache: 'no-store'})
  .then(function (r) { return r.json(); }).then(function (d) { cb(d); })
  .catch(function (e) { cb({error: String(e)}); });
"""


def is_challenged(info: dict) -> bool:
    return bool(info.get("cfOpt") or info.get("challengeEl") or info.get("turnstile"))


def _run_guard(driver, grace_override: int = -1, timeout_override: int = -1) -> tuple[bool, float | None, str]:
    """直接跑一次生产用的 cf_challenge.handle()，返回 (是否检测到风控, 恢复耗时, 备注)。

    注意：本进程的内存登记表与 WebUI 进程不共享，所以这里登记的"人工介入"不会弹到
    WebUI 上；跑完主动清掉，避免残留。
    """
    from core import cf_challenge as cf
    from core import manual_intervention as mi

    if grace_override >= 0:
        cf._cfg.CF_CHALLENGE_GRACE_SECONDS = grace_override
    if timeout_override > 0:
        cf._email_cfg.MANUAL_OTP_TIMEOUT = timeout_override
    cf.set_context(email="probe@local", job_id=None, task="CF 守卫冒烟")
    first = cf.detect(driver)
    challenged = bool(first.get("detected"))
    print("[守卫] 首次检测：detected=%s kind=%s title=%r 特征=%s"
          % (challenged, first.get("kind"), str(first.get("title"))[:40],
             ",".join(first.get("signals") or []) or "-"), flush=True)
    if not challenged:
        cf.clear_context()
        return False, None, "页面本身就是正常页，守卫按预期零副作用返回 False"

    started = time.time()
    try:
        ok = cf.handle(driver, email="probe@local", task="CF 守卫冒烟", label="探针导航")
        err = ""
    except Exception as exc:  # 超时/取消都会抛，冒烟里只记录不抛出
        ok, err = False, "%s: %s" % (type(exc).__name__, str(exc)[:120])
    elapsed = time.time() - started
    pending = mi.list_pending()
    note = "宽限=%ss 复检间隔=%ss 人工介入条目=%d%s" % (
        cf._grace_seconds(), cf._poll_interval(), len(pending), (" 异常=" + err) if err else "",
    )
    print("[守卫] handle() 返回=%s 耗时=%.1fs %s" % (ok, elapsed, note), flush=True)
    for item in pending:
        mi.clear_pending(item.get("id") or item.get("request_id") or "")
    cf.clear_context()
    cf._active_keys.clear()
    cf._last_done.clear()
    return True, (elapsed if ok else None), note


def sample(driver, label: str) -> dict:
    try:
        return driver.execute_script(POLL_JS) or {}
    except Exception as exc:
        return {"err": "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:70])}


def main() -> int:
    parser = argparse.ArgumentParser(description="比特浏览器 Cloudflare 风控页体检")
    parser.add_argument("--url", default=DEFAULT_URL, help="目标地址，默认 %s" % DEFAULT_URL)
    parser.add_argument("--wait", type=int, default=90, help="最长观察秒数，默认 90")
    parser.add_argument("--interval", type=int, default=4, help="采样间隔秒数，默认 4")
    parser.add_argument("--json", default="", help="把采样时间线写到这个文件")
    parser.add_argument("--guard", action="store_true", help="导航后直接跑一次 core/cf_challenge.handle()，验证守卫生效")
    parser.add_argument("--guard-grace", type=int, default=-1, help="覆盖 CF_CHALLENGE_GRACE_SECONDS（默认用配置值）")
    parser.add_argument("--guard-timeout", type=int, default=-1, help="覆盖 MANUAL_OTP_TIMEOUT（默认用配置值；冒烟时调小避免长时间等待）")
    args = parser.parse_args()

    client = BitBrowserClient()
    opened: BitOpenResult | None = None
    driver = None
    timeline: list[dict] = []
    verdict = "未得出结论"
    exit_ip: dict = {}
    try:
        pid = client.create_window()
        print("[探针] 已建窗（代理池 + 随机指纹）：%s" % pid, flush=True)
        result = client.request("/browser/open", {"id": pid})
        data = result.get("data") if isinstance(result, dict) else {}
        debugger = BitBrowserClient._extract_debugger_address(data or result)
        print("[探针] debugger=%s" % debugger, flush=True)
        opened = BitOpenResult(pid, result, debugger_address=debugger, created_by_run=True)

        driver = _build_driver(opened, driver_label="[Bit]")
        try:
            driver.set_page_load_timeout(45)
            driver.set_script_timeout(15)
        except Exception:
            pass

        try:
            driver.get(args.url)
        except Exception as exc:
            print("[探针] driver.get 异常（继续观察）：%s" % type(exc).__name__, flush=True)
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        try:
            exit_ip = driver.execute_async_script(ASYNC_IP)
        except Exception as exc:
            exit_ip = {"error": type(exc).__name__}

        started = time.time()
        cleared_at: float | None = None
        first_cf = False

        if args.guard:
            first_cf, cleared_at, guard_note = _run_guard(driver, args.guard_grace, args.guard_timeout)
            info = sample(driver, "after-guard")
            info["t"] = round(time.time() - started, 1)
            timeline.append(info)
            if not first_cf:
                verdict = "未被 CF 挑战（守卫生效，零副作用返回 False）"
            elif cleared_at is not None:
                verdict = "CF 守卫生效：检测到风控后 %.1fs 恢复并返回 True（%s）" % (cleared_at, guard_note)
            else:
                verdict = "CF 守卫未等到恢复，任务会按超时失败（%s）" % guard_note
        else:
            while time.time() - started < args.wait:
                info = sample(driver, "poll")
                info["t"] = round(time.time() - started, 1)
                timeline.append(info)
                challenged = is_challenged(info)
                first_cf = first_cf or challenged
                print("  +%5.1fs title=%-40r cfOpt=%-5s turnstile=%-5s inputs=%s emailField=%-5s %s"
                      % (info["t"], str(info.get("title"))[:38], info.get("cfOpt"), info.get("turnstile"),
                         info.get("inputCount"), info.get("hasEmail"), info.get("err") or ""), flush=True)
                if info.get("hasEmail") and not challenged:
                    cleared_at = info["t"]
                    break
                time.sleep(max(1, args.interval))

            if first_cf and cleared_at is not None:
                verdict = "会被 CF 挑战，且约 %.0fs 后自行放行（流程等待预算必须大于这个值）" % cleared_at
            elif first_cf:
                verdict = "会被 CF 挑战，观察 %ss 内未放行（考虑人工介入通道或换出口 IP）" % args.wait
            else:
                verdict = "未被 CF 挑战"

        print("\n[探针] 出口 IP：%s" % json.dumps(exit_ip, ensure_ascii=False), flush=True)
        if timeline:
            last = timeline[-1]
            print("[探针] 语言/时区：%s / %s" % (last.get("navLang"), last.get("tz")), flush=True)
            scripts = last.get("cfScripts") or []
            if scripts:
                print("[探针] CF 脚本：%s" % json.dumps(scripts[:3], ensure_ascii=False), flush=True)
        print("[探针] 结论：%s" % verdict, flush=True)
    except Exception as exc:
        print("[探针] 失败：%s: %s" % (type(exc).__name__, exc), flush=True)
        verdict = "探针异常：%s" % type(exc).__name__
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None:
            try:
                client.cleanup_profile(opened)
                print("[探针] 已关窗并删除：%s" % opened.profile_id, flush=True)
            except Exception as exc:
                print("[探针] 清理失败：%s" % exc, flush=True)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"url": args.url, "exitIp": exit_ip, "verdict": verdict, "timeline": timeline},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
