# -*- coding: utf-8 -*-
"""复现比特注册任务在「建窗口 → 连 Selenium → 装省流量/流量统计」阶段的失败。

对比 tools/probe_bit_selenium_cdp.py（纯净连接，CDP 正常）可以定位是哪一步、
哪条命令在真实流程里报 `WebDriverException: Message: unhandled request`。

用法：
    ./.venv/bin/python tools/probe_bit_flow_setup.py
跑完自动关闭并删除临时窗口。
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from config import bitbrowser as _cfg  # noqa: E402
from core.bitbrowser_client import BitBrowserClient  # noqa: E402
from core.browser_data_saver import BrowserDataSaver  # noqa: E402
from core.browser_traffic import SeleniumTrafficTracker  # noqa: E402
from core.roxy_registration import _SELENIUM_TIMEOUT_OVERRIDE, _build_driver  # noqa: E402


def step(label: str, fn):
    try:
        value = fn()
        print(f"\n>>> [OK] {label} -> {value!r}")
        return True, value
    except Exception as exc:  # noqa: BLE001
        print(f"\n>>> [FAIL] {label} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
        traceback.print_exc()
        return False, exc


def main() -> int:
    client = BitBrowserClient()
    opened = None
    driver = None
    tracker = None
    saver = None
    try:
        ok, opened = step("client.open_window()", client.open_window)
        if not ok:
            return 1
        raw = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
        print(f"    driver 路径={raw.get('driver')} ws={opened.ws_endpoint}")

        token = _SELENIUM_TIMEOUT_OVERRIDE.set(int(getattr(_cfg, "BIT_SELENIUM_TIMEOUT", 90) or 90))
        ok, driver = step("_build_driver(opened)", lambda: _build_driver(opened, driver_label="[Bit]"))
        if not ok:
            return 1
        try:
            driver._registration_log_prefix = "[Bit注册]"
        except Exception:
            pass

        ok, tracker = step("SeleniumTrafficTracker(driver, label='Bit')", lambda: SeleniumTrafficTracker(driver, label="Bit"))
        ok2, saver = step("BrowserDataSaver(label='Bit')", lambda: BrowserDataSaver(label="Bit"))
        if tracker is not None and saver is not None:
            step("tracker.attach_data_saver(saver)", lambda: tracker.attach_data_saver(saver))
        if saver is not None and driver is not None:
            step("saver.install_selenium(driver)", lambda: saver.install_selenium(driver))

        step("driver.set_page_load_timeout(90)", lambda: driver.set_page_load_timeout(90))
        step("driver.set_script_timeout(12)", lambda: driver.set_script_timeout(12))
        step("driver.execute_cdp_cmd('Network.enable')", lambda: driver.execute_cdp_cmd("Network.enable", {}))
        step("driver.get_log('performance')", lambda: driver.get_log("performance")[:0])
        step("driver.get('about:blank')", lambda: driver.get("about:blank"))

        try:
            _SELENIUM_TIMEOUT_OVERRIDE.reset(token)
        except Exception:
            pass
        return 0
    finally:
        for name, obj, meth in (("tracker", tracker, "stop"), ("saver", saver, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None:
            try:
                client.cleanup_profile(opened)
                print("已清理临时窗口")
            except Exception as exc:  # noqa: BLE001
                print(f"清理失败: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
