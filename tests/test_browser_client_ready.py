# -*- coding: utf-8 -*-
"""本地指纹浏览器客户端（比特 / Roxy）"打不开"时的提示与预检。

覆盖：
- 客户端没启动（端口无人监听）→ BrowserClientUnavailable + 中文"请先启动 XXX 浏览器"提示；
- 探测到任意 HTTP 响应即视为客户端在跑（401/404 也算）；
- try_request 不吞掉"客户端没启动"，保证 WebUI 能拿到提示；
- /api/jobs 提交前拦截；
- /api/browser-client/check 永远 200，用 ok 字段表达结果。
"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import bitbrowser_client as bc
from core import browser_client_ready as ready
from core import roxybrowser_client as rc
from webui.app import create_app


def _closed_port() -> int:
    """拿一个当前没人监听的端口（bind 后立刻释放）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 约定
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, *args):  # 静音
        return


@pytest.fixture()
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_client_down_message_prompts_user_to_start_browser():
    port = _closed_port()
    client = bc.BitBrowserClient(api_base=f"http://127.0.0.1:{port}")
    with pytest.raises(ready.BrowserClientUnavailable) as excinfo:
        client.request("/health", {})
    text = str(excinfo.value)
    assert "请先启动「比特浏览器」客户端" in text
    assert "BIT_API_BASE" in text
    assert f"127.0.0.1:{port}" in text


def test_roxy_client_down_message_prompts_user_to_start_browser():
    port = _closed_port()
    client = rc.RoxyBrowserClient(api_base=f"http://127.0.0.1:{port}")
    with pytest.raises(ready.BrowserClientUnavailable) as excinfo:
        client.request("POST", "/browser/open", json_body={})
    text = str(excinfo.value)
    assert "请先启动「RoxyBrowser」客户端" in text
    assert "ROXY_API_BASE" in text


def test_try_request_does_not_swallow_client_down():
    """try_request 是"接口探测"用的宽松封装，但不能把"客户端没启动"降级成探测失败。"""
    port = _closed_port()
    with pytest.raises(ready.BrowserClientUnavailable):
        bc.BitBrowserClient(api_base=f"http://127.0.0.1:{port}").try_request("/group/list", {})
    with pytest.raises(ready.BrowserClientUnavailable):
        rc.RoxyBrowserClient(api_base=f"http://127.0.0.1:{port}").try_request("GET", "/browser/workspace")


def test_any_http_response_means_client_is_running(stub_server):
    """客户端在跑但接口路径不对时（404），不应该报"请启动浏览器"。"""
    assert ready.ensure_client_ready("比特浏览器", stub_server, config_key="BIT_API_BASE") is True
    ready.ensure_client_ready("RoxyBrowser", stub_server, config_key="ROXY_API_BASE")


def test_loopback_disables_env_proxy():
    import requests

    session = requests.Session()
    assert ready.disable_env_proxy_for_loopback(session, "http://127.0.0.1:54345") is True
    assert session.trust_env is False

    session2 = requests.Session()
    assert ready.disable_env_proxy_for_loopback(session2, "http://api.example.com:8080") is False
    assert session2.trust_env is True


def test_check_registration_driver_only_for_local_browser_drivers(monkeypatch):
    from config import roxybrowser as roxy_cfg

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "protocol", raising=False)
    assert ready.check_registration_driver() is None

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "bit", raising=False)
    from config import bitbrowser as bit_cfg

    monkeypatch.setattr(bit_cfg, "BIT_API_BASE", f"http://127.0.0.1:{_closed_port()}", raising=False)
    error = ready.check_registration_driver()
    assert error and "请先启动「比特浏览器」客户端" in error


def test_jobs_api_blocks_when_driver_client_is_down(monkeypatch):
    from config import roxybrowser as roxy_cfg

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "roxy", raising=False)
    monkeypatch.setattr(roxy_cfg, "ROXY_API_BASE", f"http://127.0.0.1:{_closed_port()}", raising=False)

    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    response = client.post("/api/jobs", json={"count": 1, "workers": 1})

    assert response.status_code == 400
    assert "请先启动「RoxyBrowser」客户端" in response.get_json()["error"]

    checked = client.get("/api/browser-client/check")
    body = checked.get_json()
    assert checked.status_code == 200
    assert body["ok"] is False and body["applicable"] is True
    assert body["label"] == "RoxyBrowser"
    assert "请先启动「RoxyBrowser」客户端" in body["error"]


def test_browser_client_check_skips_other_drivers(monkeypatch):
    from config import roxybrowser as roxy_cfg

    monkeypatch.setattr(roxy_cfg, "REGISTRATION_DRIVER", "protocol", raising=False)
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    body = client.get("/api/browser-client/check").get_json()
    assert body["ok"] is True and body["applicable"] is False
