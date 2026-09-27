# -*- coding: utf-8 -*-
"""通用「需要人工处理」通道（core/manual_intervention.py）。

覆盖：
- 登记 → 定时复检 → 页面恢复即返回，且请求带 guide/url/email/job_id 等前端要用的字段；
- 用户点「我已处理完成」能立刻唤醒一次复检（不必等下一个轮询周期）；
- 用户取消 → 等待侧抛 ManualInterventionCancelled；
- 超时 → 抛 ManualInterventionTimeout；
- 任务被手动停止 → 等待侧抛 StopRequested，不会空等。
"""
import threading
import time

import pytest

from core import manual_intervention as mi


@pytest.fixture(autouse=True)
def _clean():
    mi._entries.clear()
    yield
    mi._entries.clear()


def test_probe_recovery_returns_and_keeps_item_for_ui():
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"recovered": True, "status_text": "页面已恢复正常，流程继续", "detail": "当前页面：登录页"}
        return {"recovered": False, "status_text": "仍是 Cloudflare 人机验证", "detail": "页面标题：Just a moment"}

    result = mi.request_manual_intervention(
        probe=probe,
        kind="cloudflare_challenge",
        title="需要你手动完成 Cloudflare 验证",
        guide="请在浏览器窗口里点击验证框",
        url="https://chatgpt.com/auth/login",
        email="a@b.com",
        job_id=7,
        task="注册",
        timeout=60,
        poll_interval=1,
    )

    assert result["status"] == mi.STATUS_RECOVERED
    assert result["checks"] == 2
    assert result["email"] == "a@b.com"
    assert result["job_id"] == 7
    assert result["kind_label"] == "Cloudflare 风控验证"

    # 结束后请求在前端多停留几秒（grace），让用户看到"已恢复"，而不是瞬间消失
    items = mi.list_pending()
    assert len(items) == 1
    assert items[0]["status"] == mi.STATUS_RECOVERED
    assert items[0]["guide"] == "请在浏览器窗口里点击验证框"
    assert items[0]["remaining"] is not None


def test_ack_wakes_up_wait_immediately():
    state = {"recovered": False}
    started = {"at": None}

    def probe():
        return {"recovered": state["recovered"], "status_text": "仍是风控页"}

    holder = {}

    def worker():
        started["at"] = time.time()
        holder["result"] = mi.request_manual_intervention(
            probe=probe,
            kind="cloudflare_challenge",
            title="需要你手动完成 Cloudflare 验证",
            timeout=120,
            poll_interval=30,   # 故意很长：只能靠 ack 唤醒
        )

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    deadline = time.time() + 5
    while time.time() < deadline and not mi.list_pending():
        time.sleep(0.05)
    items = mi.list_pending()
    assert items, "应当已登记一条待人工处理请求"

    mi.ack_user_done(items[0]["request_id"])
    state["recovered"] = True
    t.join(timeout=5)

    assert not t.is_alive(), "ack 后应立即复检并返回，而不是等 30 秒轮询"
    assert holder["result"]["status"] == mi.STATUS_RECOVERED
    assert time.time() - started["at"] < 10


def test_cancel_raises_cancelled():
    holder = {}

    def worker():
        try:
            mi.request_manual_intervention(
                probe=lambda: {"recovered": False},
                kind="cloudflare_challenge",
                title="需要你手动完成 Cloudflare 验证",
                timeout=120,
                poll_interval=30,
            )
        except Exception as exc:  # noqa: BLE001 - 测试要拿到原始异常类型
            holder["exc"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not mi.list_pending():
        time.sleep(0.05)

    mi.cancel_request(mi.list_pending()[0]["request_id"], reason="用户在 WebUI 取消人工处理")
    t.join(timeout=5)

    assert isinstance(holder.get("exc"), mi.ManualInterventionCancelled)
    assert "取消" in str(holder["exc"])


def test_timeout_raises_timeout_error():
    with pytest.raises(mi.ManualInterventionTimeout):
        mi.request_manual_intervention(
            probe=lambda: {"recovered": False, "status_text": "仍是风控页"},
            kind="cloudflare_challenge",
            title="需要你手动完成 Cloudflare 验证",
            timeout=5,
            poll_interval=1,
        )


def test_stop_requested_aborts_wait(monkeypatch):
    from core import registration_service as svc

    class _Stop(RuntimeError):
        pass

    def _raise():
        raise _Stop("任务已被用户手动停止")

    monkeypatch.setattr(svc, "check_stop_requested", _raise, raising=False)

    started = time.time()
    with pytest.raises(_Stop):
        mi.request_manual_intervention(
            probe=lambda: {"recovered": False},
            kind="cloudflare_challenge",
            title="需要你手动完成 Cloudflare 验证",
            timeout=120,
            poll_interval=1,
        )
    assert time.time() - started < 10


def test_terminal_status_is_purged_after_grace():
    """终态（恢复/超时/取消）要能自动消失，否则弹框会一直挂在页面上。"""
    item = mi.mark_pending(kind="cloudflare_challenge", title="需要你手动完成 Cloudflare 验证", timeout=60)
    rid = item["request_id"]

    mi.note_status(rid, status=mi.STATUS_RECOVERED, status_text="页面已恢复正常，流程继续")
    view = mi.get_pending(rid)
    assert view["status"] == mi.STATUS_RECOVERED
    assert view["resolved_at"]

    # 直接在"未来"时刻清理，避免测试等 10 秒
    mi._purge_resolved(now=time.time() + mi._RESOLVED_GRACE_SECONDS + 1)
    assert mi.get_pending(rid) is None


def test_probe_exception_does_not_abort_waiting():
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("浏览器连接断了")
        return {"recovered": True, "status_text": "页面已恢复正常"}

    result = mi.request_manual_intervention(
        probe=probe,
        kind="cloudflare_challenge",
        title="需要你手动完成 Cloudflare 验证",
        timeout=30,
        poll_interval=1,
    )
    assert result["status"] == mi.STATUS_RECOVERED
    assert calls["n"] == 2
