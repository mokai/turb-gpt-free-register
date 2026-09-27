# -*- coding: utf-8 -*-
"""探测比特浏览器自带 chromedriver 支持哪些 Selenium / CDP 端点。

背景：比特浏览器 `/browser/open` 返回的 chromedriver 与内核版本号一致，但 Selenium
`execute_cdp_cmd()` 会报 `WebDriverException: Message: unhandled request`——该错误来自
chromedriver 的 HTTP 路由层（`http_handler.cc` 的 "unhandled request" 分支），说明
**这个 chromedriver 没有注册该端点**，与内核版本无关。

本脚本创建临时比特窗口，逐个试探端点，输出结论与可用的替代通道：
  1. Selenium execute_cdp_cmd（POST /session/<id>/goog/cdp/execute）
  2. chromedriver 旧端点 /session/<id>/chromium/send_command_and_get_result
  3. 常规 W3C 命令 set_page_load_timeout / get / execute script
  4. 直接用 DevTools WebSocket 发 CDP（opened.ws_endpoint）

用法：
    ./.venv/bin/python tools/probe_bit_selenium_cdp.py
跑完自动关闭并删除临时窗口。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.bitbrowser_client import BitBrowserClient  # noqa: E402


def _safe(label: str, fn):
    try:
        value = fn()
        print(f"[OK]   {label} -> {value!r}")
        return True, value
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return False, exc


def main() -> int:
    client = BitBrowserClient()
    opened = None
    driver = None
    try:
        # 一号一窗口模式下 open_window() 会自己创建窗口，不能传固定 profile_id。
        opened = client.open_window()
        print(f"临时窗口: {opened.profile_id}")
        raw = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        print(f"debugger={opened.debugger_address} ws={opened.ws_endpoint}")
        print(f"driver={raw.get('driver')}")

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.page_load_strategy = "eager"
        driver_path = str(raw.get("driver") or "")
        service = Service(
            executable_path=driver_path or None,
            log_output="/tmp/probe_bit_chromedriver.log",
            service_args=["--verbose"],
        )
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        driver = webdriver.Chrome(service=service, options=options)
        session_id = driver.session_id
        base = ""
        for attr in ("service_url",):
            base = str(getattr(getattr(driver, "service", None), attr, "") or "")
            if base:
                break
        if not base:
            base = str(getattr(driver.command_executor, "_remote_server_addr", "") or "")
            if base and not base.startswith("http"):
                base = f"http://{base}"
        base = base.rstrip("/")
        print(f"session_id={session_id} chromedriver={base}")

        # 1) Selenium 的 CDP 端点
        _safe("execute_cdp_cmd(Network.enable)", lambda: driver.execute_cdp_cmd("Network.enable", {}))
        # 2) chromedriver 旧 CDP 端点
        import requests

        def legacy():
            resp = requests.post(
                f"{base}/session/{session_id}/chromium/send_command_and_get_result",
                json={"cmd": "Network.enable", "params": {}},
                timeout=10,
            )
            return resp.status_code, resp.text[:200]

        _safe("legacy send_command_and_get_result", legacy)
        # 3) 常规命令
        _safe("set_page_load_timeout(30)", lambda: driver.set_page_load_timeout(30))
        _safe("execute_script('1+1')", lambda: driver.execute_script("return 1+1"))
        _safe("get(about:blank)", lambda: driver.get("about:blank"))
        _safe("title", lambda: driver.title)

        # 4) DevTools WebSocket 直连
        def ws_cdp():
            import websocket

            ws_url = opened.ws_endpoint
            if not ws_url:
                return "no ws endpoint"
            conn = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
            try:
                conn.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
                deadline = time.time() + 8
                while time.time() < deadline:
                    msg = json.loads(conn.recv())
                    if msg.get("id") == 1:
                        return msg
                return "timeout"
            finally:
                conn.close()

        _safe("DevTools WebSocket Network.enable", ws_cdp)
        return 0
    finally:
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
