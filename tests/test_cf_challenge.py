# -*- coding: utf-8 -*-
"""Cloudflare 风控页识别 + 人工处理接入（core/cf_challenge.py）。

覆盖：
- 识别 Turnstile / 全屏挑战页 / 拦截页；
- 检测到风控 → 先按 CF_CHALLENGE_GRACE_SECONDS 静默等自动放行（自行放行则不弹框）；
- 宽限期过后仍被风控 → 弹框登记（带 email/job_id/页面地址/操作指引）→ 页面恢复后流程继续；
- 拦截页（1020）不自愈，跳过宽限期直接弹框；
- 页面正常时零副作用（不登记任何请求）；
- 去抖：刚处理完不会立刻重复弹框；
- 总开关关闭时不介入；
- 注册流程里"找不到元素"实际是风控页时，处理完能自动重试成功（_find_any 集成）。

注意：用例里的 `_patch_cfg` 默认把 `CF_CHALLENGE_GRACE_SECONDS` 压成 0（真实默认 50s），
否则每个风控用例都要真等几十秒、且第一次复检就"自行放行"导致断言失真。
"""
import pytest

from core import cf_challenge as cf
from core import manual_intervention as mi
from core import roxy_registration as roxy


class _FakeElement:
    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class _FakeDriver:
    """只实现风控检测与元素查找需要的接口。

    detections: 依次返回的检测结果；用完后默认返回"当前页面没有风控"。
    reveal_after_recovery: 风控页期间页面里没有目标元素；一旦检测到"页面已恢复正常"，
    元素才出现（模拟用户在指纹浏览器里点过验证框后页面重新渲染）。
    """

    def __init__(self, detections=(), reveal_after_recovery=False):
        self.detections = list(detections)
        self.reveal_after_recovery = bool(reveal_after_recovery)
        self.find_calls = 0
        self.hooks = []
        self.recovered = not self.reveal_after_recovery

    def execute_script(self, script, *args):
        if "turnstile-widget" in script:  # 风控检测脚本
            self.hooks.append("detect")
            if self.detections:
                info = self.detections.pop(0)
            else:
                info = {"detected": False, "kind": "", "url": "https://chatgpt.com/auth/login", "title": "ChatGPT"}
            if not info.get("detected"):
                self.recovered = True
            return info
        return None

    def find_elements(self, by, selector):
        self.find_calls += 1
        if not self.recovered:
            return []
        return [_FakeElement()]


class _NavDriver(_FakeDriver):
    """_safe_get 需要的最小导航接口。"""

    def __init__(self, detections=()):
        super().__init__(detections=detections)
        self.url = "about:blank"

    def get(self, url):
        self.url = url

    def set_page_load_timeout(self, timeout):
        pass

    def set_script_timeout(self, timeout):
        pass

    @property
    def current_url(self):
        return self.url


def _challenge(kind="turnstile"):
    return {
        "detected": True,
        "kind": kind,
        "url": "https://chatgpt.com/auth/login",
        "title": "Just a moment...",
        "signals": ["turnstile-widget", "title"],
        "text": "Verify you are human",
    }


@pytest.fixture(autouse=True)
def _clean():
    mi._entries.clear()
    cf._active_keys.clear()
    cf._last_done.clear()
    cf.clear_context()
    yield
    mi._entries.clear()
    cf._active_keys.clear()
    cf._last_done.clear()
    cf.clear_context()


def _patch_cfg(monkeypatch, **values):
    # 默认关闭"静默宽限期"（真实默认 50s）：否则每个风控用例都要真等几十秒，
    # 且第一次 detect 后挑战就被"自行放行"了，断言全部失真。
    # 需要验证宽限期的用例显式传 CF_CHALLENGE_GRACE_SECONDS 覆盖。
    values.setdefault("CF_CHALLENGE_GRACE_SECONDS", 0)
    for key, value in values.items():
        monkeypatch.setattr(cf._cfg, key, value, raising=False)


def test_detect_flags_turnstile_page():
    driver = _FakeDriver(detections=[_challenge("turnstile")])
    info = cf.detect(driver)
    assert info["detected"] is True
    assert info["kind"] == "turnstile"


def test_detect_swallows_browser_errors():
    class _Broken:
        def execute_script(self, script, *args):
            raise RuntimeError("unhandled request")

    info = cf.detect(_Broken())
    assert info["detected"] is False
    assert "unhandled request" in info["error"]


def test_detect_script_keeps_key_signals():
    """锁住检测脚本里的关键判据（2026-10-03 实测：挑战页 t=0 时只有 _cf_chl_opt，
    turnstile 组件还没注入，只靠 DOM 选择器会漏报）。"""
    js = cf._DETECT_JS
    assert "_cf_chl_opt" in js, "必须检查 window._cf_chl_opt（CF 挑战页 t=0 唯一的强特征）"
    assert "cf-chl-opt-empty" in js
    assert "challenges.cloudflare.com" in js          # turnstile 跨域 iframe
    assert "input[name=\"cf-turnstile-response\"]" in js
    assert "#challenge-stage" in js and "#challenge-form" in js
    assert "#cf-chl-widget" in js                     # 新版控件容器
    assert "cdn-cgi" in js and "challenge-platform" in js   # 弱特征（正则里斜杠有转义）
    assert "बस कुछ पल" in js, "印地语挑战页标题（印度出口 IP 的默认语言）"


def test_handle_registers_pending_and_returns_after_recovery(monkeypatch):
    _patch_cfg(monkeypatch, CF_CHALLENGE_POLL_INTERVAL=1)
    # 第一次检测=风控页；复检时页面已恢复（默认返回 not detected）
    driver = _FakeDriver(detections=[_challenge()])
    cf.set_context(email="a@b.com", job_id=11, task="注册")

    seen = {}
    real_request = mi.request_manual_intervention

    def _spy(**kwargs):
        seen.update(kwargs)
        return real_request(**kwargs)

    monkeypatch.setattr(mi, "request_manual_intervention", _spy)

    assert cf.handle(driver, email="a@b.com", job_id=11, task="注册", label="导航") is True
    assert seen["email"] == "a@b.com"
    assert seen["job_id"] == 11
    assert seen["kind"] == cf.KIND
    assert seen["url"] == "https://chatgpt.com/auth/login"
    assert "指纹浏览器窗口" in seen["guide"] and "复选框" in seen["guide"]
    assert seen["timeout"] == cf._timeout()   # 与「人工介入超时时间」同一个配置

    items = mi.list_pending()
    assert items and items[0]["status"] == mi.STATUS_RECOVERED
    assert items[0]["job_id"] == 11


def test_handle_returns_false_without_side_effects_on_normal_page():
    driver = _FakeDriver()
    assert cf.handle(driver, email="a@b.com", job_id=12, label="导航") is False
    assert mi.list_pending() == []


def test_handle_dedupes_immediate_repeat(monkeypatch):
    _patch_cfg(monkeypatch, CF_CHALLENGE_POLL_INTERVAL=1)
    driver = _FakeDriver(detections=[_challenge(), _challenge()])
    assert cf.handle(driver, email="a@b.com", job_id=13, label="导航") is True
    # 去抖窗口内再次检测到风控不重复弹框（否则会在探针循环里反复打扰用户）
    assert cf.handle(driver, email="a@b.com", job_id=13, label="导航") is False


def test_disabled_switch_skips_everything(monkeypatch):
    _patch_cfg(monkeypatch, CF_CHALLENGE_MANUAL_WAIT=False)
    driver = _FakeDriver(detections=[_challenge()])
    assert cf.handle(driver, email="a@b.com", job_id=14, label="导航") is False
    assert mi.list_pending() == []


def test_timeout_uses_shared_manual_intervention_config(monkeypatch):
    """等待上限与「人工介入超时时间」(MANUAL_OTP_TIMEOUT) 是同一个配置。"""
    monkeypatch.setattr(cf._email_cfg, "MANUAL_OTP_TIMEOUT", 900, raising=False)
    assert cf._timeout() == 900
    monkeypatch.setattr(cf._email_cfg, "MANUAL_OTP_TIMEOUT", 10, raising=False)
    assert cf._timeout() == 30   # 下限保护，避免配错成 0/负数后立刻超时


def test_timeout_propagates_so_task_ends(monkeypatch):
    """用户一直不处理 → 超时异常必须抛出去让任务自动结束（不能吞掉）。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_GRACE_SECONDS=0)
    driver = _FakeDriver(detections=[_challenge()])
    cf.set_context(email="a@b.com", job_id=16, task="注册")

    def _timeout_now(**kwargs):
        raise mi.ManualInterventionTimeout("等待人工处理超时（600s）")

    monkeypatch.setattr(mi, "request_manual_intervention", _timeout_now)
    with pytest.raises(mi.ManualInterventionTimeout):
        cf.handle(driver, email="a@b.com", job_id=16, label="导航")
    # 超时后要去抖表已释放，用户手动处理后重新检测仍能再次弹框
    assert cf._active_keys == set()


def test_grace_period_self_release_skips_popup(monkeypatch):
    """实测 chatgpt.com 的托管挑战约 42s 会自行放行：宽限期内不该打扰用户，
    也不该让上层走"元素缺失 → refresh"（refresh 会把挑战重置）。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_GRACE_SECONDS=2, CF_CHALLENGE_POLL_INTERVAL=1)
    driver = _FakeDriver(detections=[_challenge()])   # 复检时默认返回"已放行"

    def _no_popup(**kwargs):
        raise AssertionError("宽限期内已自行放行，不该弹框")

    monkeypatch.setattr(mi, "request_manual_intervention", _no_popup)
    assert cf.handle(driver, email="a@b.com", job_id=18, label="导航") is True
    assert mi.list_pending() == []
    assert cf._active_keys == set()          # 没登记过人工介入
    assert driver.hooks.count("detect") >= 2  # 确实复检过


def test_grace_period_expiry_falls_back_to_manual(monkeypatch):
    """宽限期过后仍被风控 → 才登记人工介入（'blocked' 拦截页不等宽限期，见下）。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_GRACE_SECONDS=1, CF_CHALLENGE_POLL_INTERVAL=1)
    driver = _FakeDriver(detections=[_challenge()] * 4)   # 一直没放行，最后才恢复
    cf.set_context(email="a@b.com", job_id=19, task="注册")

    assert cf.handle(driver, email="a@b.com", job_id=19, task="注册", label="导航") is True
    items = mi.list_pending()
    assert items and items[0]["status"] == mi.STATUS_RECOVERED
    assert items[0]["job_id"] == 19


def test_blocked_kind_skips_grace_period(monkeypatch):
    """1020 拦截页不会自愈，必须立刻弹框（不浪费宽限期）。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_GRACE_SECONDS=60, CF_CHALLENGE_POLL_INTERVAL=1)
    driver = _FakeDriver(detections=[_challenge("blocked")])
    cf.set_context(email="a@b.com", job_id=20, task="注册")

    assert cf.handle(driver, email="a@b.com", job_id=20, task="注册", label="导航") is True
    items = mi.list_pending()
    assert items and items[0]["status"] == mi.STATUS_RECOVERED
    assert items[0]["extra"]["kind"] == "blocked"
    assert driver.hooks.count("detect") < 4   # 没有耗掉 60s 宽限期


def test_safe_get_handles_challenge_after_navigation(monkeypatch):
    """导航到登录页后页面被风控接管：_safe_get 会提醒用户并等页面恢复。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_POLL_INTERVAL=1)
    cf.set_context(email="a@b.com", job_id=17, task="注册")
    driver = _NavDriver(detections=[_challenge()])

    roxy._safe_get(driver, "https://chatgpt.com/auth/login", timeout=10, attempts=1,
                   accept_hosts=("chatgpt.com", "auth.openai.com"))

    assert driver.hooks.count("detect") >= 2   # 导航后检测 + 复检
    items = mi.list_pending()
    assert items and items[0]["status"] == mi.STATUS_RECOVERED
    assert items[0]["url"] == "https://chatgpt.com/auth/login"
    assert items[0]["job_id"] == 17


def test_find_any_retries_after_user_solved_challenge(monkeypatch):
    """注册流程里"找不到元素"其实是风控页：用户处理完后自动重试成功。"""
    _patch_cfg(monkeypatch, CF_CHALLENGE_POLL_INTERVAL=1)
    cf.set_context(email="a@b.com", job_id=15, task="注册")
    # 风控页期间查不到元素 -> 检测到风控并等用户处理 -> 页面恢复后重试拿到元素
    driver = _FakeDriver(detections=[_challenge()], reveal_after_recovery=True)

    element = roxy._find_any(driver, ["input[type='email']"], timeout=1)
    assert isinstance(element, _FakeElement)
    assert driver.hooks.count("detect") >= 2


def test_find_any_still_raises_when_not_challenge(monkeypatch):
    driver = _FakeDriver(detections=[], reveal_after_recovery=True)

    with pytest.raises(RuntimeError, match="找不到页面元素"):
        roxy._find_any(driver, ["input[type='email']"], timeout=1)
    assert mi.list_pending() == []
