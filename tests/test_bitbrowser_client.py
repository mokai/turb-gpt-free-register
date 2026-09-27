# -*- coding: utf-8 -*-
"""比特浏览器客户端单元测试（全部 mock，不依赖本地比特浏览器进程）。"""
from core import bitbrowser_client as bc


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._p = payload
        self.text = "OK"
        self.status_code = status_code

    def json(self):
        return self._p


class _FakeSession:
    headers = {}

    def __init__(self, routes):
        self.routes = routes  # {path_suffix: fn(body) -> payload}

    def post(self, url, json=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        handler = self.routes.get(path)
        if handler is None:
            raise AssertionError(f"unexpected path: {path}")
        return _FakeResp(handler(json or {}))


def test_proxy_url_to_bit_info():
    info = bc._proxy_url_to_bit_info("socks5h://user:pa%40ss@1.2.3.4:1080")
    assert info["proxyMethod"] == 2
    assert info["proxyType"] == "socks5"
    assert info["host"] == "1.2.3.4"
    assert info["port"] == "1080"
    assert info["proxyUserName"] == "user"
    assert info["proxyPassword"] == "pa@ss"


def test_extract_debugger_address_variants():
    assert bc.BitBrowserClient._extract_debugger_address({"data": {"http": "127.0.0.1:9222"}}) == "127.0.0.1:9222"
    assert bc.BitBrowserClient._extract_debugger_address({"http": ":9223"}) == "127.0.0.1:9223"
    assert bc.BitBrowserClient._extract_debugger_address({"debuggingPort": "9224"}) == "127.0.0.1:9224"
    assert bc.BitBrowserClient._extract_debugger_address({}) is None


def test_normalize_profile_id_placeholders():
    assert bc.BitBrowserClient._normalize_profile_id("-") == ""
    assert bc.BitBrowserClient._normalize_profile_id(" 无 ") == ""
    assert bc.BitBrowserClient._normalize_profile_id("123") == "123"


def test_lifecycle_mock(monkeypatch):
    from config import bitbrowser as cfg

    monkeypatch.setattr(cfg, "BIT_SELENIUM_TIMEOUT", 10, raising=False)
    monkeypatch.setattr(cfg, "BIT_GROUP_ID", "", raising=False)
    monkeypatch.setattr(cfg, "BIT_CREATE_USE_PROXY_POOL", False, raising=False)
    monkeypatch.setattr(cfg, "BIT_ONE_PROFILE_PER_ACCOUNT", True, raising=False)
    monkeypatch.setattr(cfg, "BIT_DELETE_PROFILE_AFTER_RUN", True, raising=False)
    monkeypatch.setattr(cfg, "BIT_KEEP_BROWSER_OPEN", False, raising=False)

    seen = {}

    def update(body):
        assert body.get("id") is None  # create
        assert "browserFingerPrint" in body
        assert body.get("name")
        seen["created"] = True
        return {"success": True, "data": {"id": "8888"}}

    def open_(body):
        assert body.get("id") == "8888"
        return {
            "success": True,
            "data": {"ws": "ws://127.0.0.1:9222/devtools/1", "http": "127.0.0.1:9222", "driver": "/tmp/chromedriver"},
        }

    def close(body):
        seen["closed"] = body.get("id")
        return {"success": True}

    def delete(body):
        seen["deleted"] = body.get("id")
        return {"success": True}

    client = bc.BitBrowserClient(api_base="http://127.0.0.1:54345")
    client.http = _FakeSession({"update": update, "open": open_, "close": close, "delete": delete})

    opened = client.open_window()
    assert opened.profile_id == "8888"
    assert opened.created_by_run is True
    assert opened.debugger_address == "127.0.0.1:9222"
    assert opened.browser_kind == "Bit"
    # open 响应里的 driver 路径须与 roxy_registration._build_driver 的读取方式兼容
    assert opened.raw["data"]["driver"] == "/tmp/chromedriver"

    client.cleanup_profile(opened)
    assert seen["closed"] == "8888"
    assert seen["deleted"] == "8888"


def test_one_profile_rejects_fixed_profile_id(monkeypatch):
    from config import bitbrowser as cfg

    monkeypatch.setattr(cfg, "BIT_ONE_PROFILE_PER_ACCOUNT", True, raising=False)
    client = bc.BitBrowserClient(api_base="http://127.0.0.1:54345")
    try:
        client.open_window("12345")
    except RuntimeError as exc:
        assert "一号一窗口" in str(exc)
    else:
        raise AssertionError("应拒绝固定窗口 ID")


def test_list_groups_empty_list_shape(monkeypatch):
    client = bc.BitBrowserClient(api_base="http://127.0.0.1:54345")
    client.http = _FakeSession({
        "list": lambda body: {"success": True, "data": {"list": [], "totalNum": 0}},
    })
    result = client.list_groups()
    assert result["ok"] is True
    assert result["items"] == []
