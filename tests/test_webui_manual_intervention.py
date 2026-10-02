# -*- coding: utf-8 -*-
"""WebUI 通用页面人工处理 API。

覆盖：
- GET /api/manual-intervention/pending：无请求时返回空列表；有待处理时返回完整字段；
- POST /api/manual-intervention/ack：立即复检（唤醒等待侧）；
- POST /api/manual-intervention/cancel：等待侧抛 ManualInterventionCancelled，任务失败；
- 请求已结束时给出可读错误而不是 500。
"""
import threading
import time

from webui.app import create_app

from core import manual_intervention as mi


def _client():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def _wait_pending(timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        items = mi.list_pending()
        if items:
            return items
        time.sleep(0.05)
    return []


def setup_function(_func=None):
    mi._entries.clear()


def teardown_function(_func=None):
    mi._entries.clear()


def test_pending_is_empty_by_default():
    client = _client()
    r = client.get("/api/manual-intervention/pending")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "items": []}


def test_pending_exposes_fields_frontend_needs():
    mi.mark_pending(
        kind="page_verification",
        title="需要你手动完成页面验证",
        guide="请在浏览器窗口里完成页面验证",
        url="https://chatgpt.com/auth/login",
        email="a@b.com",
        job_id=42,
        task="注册",
        timeout=600,
        poll_interval=5,
    )
    client = _client()
    items = client.get("/api/manual-intervention/pending").get_json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind_label"] == "页面验证"
    assert item["email"] == "a@b.com"
    assert item["job_id"] == 42
    assert item["url"] == "https://chatgpt.com/auth/login"
    assert item["guide"]
    assert item["remaining"] > 0
    assert item["status"] == mi.STATUS_WAITING


def test_ack_wakes_waiter():
    holder = {}

    def worker():
        try:
            holder["result"] = mi.request_manual_intervention(
                probe=lambda: {"recovered": holder.get("done", False)},
                kind="page_verification",
                title="需要你手动完成页面验证",
                timeout=60,
                poll_interval=30,   # 抽很长，只有 ack 能唤醒
            )
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    threading.Thread(target=worker, daemon=True).start()
    items = _wait_pending()
    assert items, "应当已登记一条待人工处理请求"

    client = _client()
    holder["done"] = True
    r = client.post("/api/manual-intervention/ack", json={"request_id": items[0]["request_id"]})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    deadline = time.time() + 5
    while time.time() < deadline and "result" not in holder and "exc" not in holder:
        time.sleep(0.05)
    assert holder.get("result", {}).get("status") == mi.STATUS_RECOVERED


def test_cancel_ends_task():
    holder = {}

    def worker():
        try:
            mi.request_manual_intervention(
                probe=lambda: {"recovered": False},
                kind="page_verification",
                title="需要你手动完成页面验证",
                timeout=60,
                poll_interval=30,
            )
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    threading.Thread(target=worker, daemon=True).start()
    items = _wait_pending()
    assert items

    client = _client()
    r = client.post(
        "/api/manual-intervention/cancel",
        json={"request_id": items[0]["request_id"], "reason": "用户在 WebUI 取消人工处理"},
    )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    deadline = time.time() + 5
    while time.time() < deadline and "exc" not in holder:
        time.sleep(0.05)
    assert isinstance(holder.get("exc"), mi.ManualInterventionCancelled)


def test_unknown_request_id_returns_readable_error():
    client = _client()
    r = client.post("/api/manual-intervention/ack", json={"request_id": "nope"})
    assert r.status_code == 400
    assert "已结束" in r.get_json()["error"]

    r2 = client.post("/api/manual-intervention/cancel", json={})
    assert r2.status_code == 400
    assert "request_id" in r2.get_json()["error"]
