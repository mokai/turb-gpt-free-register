# -*- coding: utf-8 -*-
"""Cloudflare 风控页识别 + 人工处理通道。

注册/授权过程中，某个页面可能被 Cloudflare 接管：

- **Turnstile / 托管挑战**：页面里出现跨域 iframe 的 “Verify you are human” 复选框，
  脚本点不到（跨域），必须由用户在指纹浏览器窗口里手动点击；
- **全屏挑战页**：“Just a moment… / 正在验证您的浏览器”，通常几秒后自动放行，
  卡住时需要用户手动刷新；
- **拦截页**：“Attention Required / Sorry, you have been blocked (1020)”，
  这是出口 IP 或被 Cloudflare 判定为高风险，一般要换代理/环境。

本模块负责：把“页面被风控、需要人工处理”这件事登记到 WebUI（弹框提醒），
然后按 :data:`CF_CHALLENGE_POLL_INTERVAL` **定时复检页面是否恢复正常**，
恢复后注册流程自动继续。

配置（``.env`` / WebUI 可改）：

- ``CF_CHALLENGE_MANUAL_WAIT``：总开关（``config/browser.py``），默认 True；
- ``CF_CHALLENGE_GRACE_SECONDS``：先静默等待挑战自行放行的秒数（默认 50，0=立刻弹框）；
- ``CF_CHALLENGE_POLL_INTERVAL``：复检间隔秒数（``config/browser.py``），默认 5；
- 等待上限：与「人工介入超时时间」共用 ``MANUAL_OTP_TIMEOUT``（``config/email.py``，默认 600 秒）。
  用户一直不处理就超时，对应任务自动结束（失败），不会一直挂着。
"""
from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar

from config import browser as _cfg
from config import email as _email_cfg
from core import manual_intervention

logger = logging.getLogger(__name__)

KIND = "cloudflare_challenge"

# 同一任务/邮箱在这段时间内不重复弹框：刚处理完又立刻复检失败属于抖动，
# 反复弹框比让它走一次失败的等待更糟。
_DEDUPE_SECONDS = 10.0

_CTX: ContextVar[dict] = ContextVar("cf_challenge_ctx", default={})
_guard_lock = threading.Lock()
_active_keys: set[str] = set()
_last_done: dict[str, float] = {}

_HINTS = {
    "turnstile": (
        "页面弹出了 Cloudflare 人机验证（“Verify you are human”）。"
        "请切到正在运行的指纹浏览器窗口，在那张提示框里点一下复选框（必要时在框内再点一次“Verify/确认”）。"
        "只要验证通过，脚本会自动检测到并继续下一步。"
    ),
    "interstitial": (
        "页面被 Cloudflare 全屏风控页接管（“Just a moment… / 正在验证您的浏览器”）。"
        "多数情况下会自动放行；如果超过 10 秒仍停在这个页面，请在浏览器窗口里按一次 F5 刷新。"
    ),
    "blocked": (
        "页面被 Cloudflare 拦截（Attention Required / Access denied，常见 1020）。"
        "这通常是出口 IP 或浏览器环境被判高风险：建议先在 WebUI 里换一个代理出口、"
        "必要时重建浏览器环境，然后点「我已处理完成」让脚本立刻复检。"
    ),
}


# ----------------------------------------------------------------------------
# 任务上下文（email / job_id），供深处共享函数（导航、元素等待）调用时带上下文
# ----------------------------------------------------------------------------

def set_context(*, email: str | None = None, job_id: int | None = None, task: str = "") -> None:
    _CTX.set({"email": email, "job_id": job_id, "task": task})


def update_context(**fields) -> None:
    ctx = dict(_CTX.get() or {})
    ctx.update({k: v for k, v in fields.items() if v is not None})
    _CTX.set(ctx)


def clear_context() -> None:
    _CTX.set({})


def current_context() -> dict:
    ctx = dict(_CTX.get() or {})
    if not ctx.get("job_id"):
        try:
            from core import registration_service as svc

            job_id = getattr(svc._THREAD_CTX, "job_id", None)
            if job_id:
                ctx["job_id"] = int(job_id)
        except Exception:
            pass
    return ctx


def _key(job_id: int | None, email: str | None) -> str:
    if job_id:
        return "job:%s" % job_id
    return "mail:%s" % str(email or "").strip().lower()


def _begin(key: str) -> bool:
    """返回 True 表示本次调用可以弹框（无并发、且不在去抖窗口内）。"""
    now = time.time()
    with _guard_lock:
        if key in _active_keys:
            return False
        if now - float(_last_done.get(key) or 0) < _DEDUPE_SECONDS:
            return False
        _active_keys.add(key)
        return True


def _end(key: str) -> None:
    with _guard_lock:
        _active_keys.discard(key)
        _last_done[key] = time.time()


# ----------------------------------------------------------------------------
# 识别
# ----------------------------------------------------------------------------

_DETECT_JS = r"""
const url = String(location.href || '');
const title = String(document.title || '');
const bodyText = String((document.body && document.body.innerText) || '').slice(0, 4000);
const html = String((document.documentElement && document.documentElement.innerHTML) || '').slice(0, 30000);
const has = (sel) => { try { return !!document.querySelector(sel); } catch (e) { return false; } };
const strong = [];
const weak = [];
// ⭐ 最可靠的一条：window._cf_chl_opt 是 CF 挑战脚本自己注入的全局变量，
// 正常页面没有它。实测被挑战的 chatgpt.com 页面在 **t=0** 就已经有 _cf_chl_opt，
// 但那时 turnstile 组件还没注入（iframe/input 都还没有）—— 只靠 DOM 选择器会漏报。
// 配合"页面自己没有任何表单控件"使用，避免正常页面被误判。
const inputCount = document.querySelectorAll('input, textarea, form').length;
if (window._cf_chl_opt && inputCount === 0) strong.push('cf-chl-opt-empty');
if (window._cf_chl_opt && (has('#challenge-stage') || has('#challenge-form') || has('#cf-chl-widget'))) {
  strong.push('cf-chl-opt-dom');
}
// Turnstile / 托管挑战：跨域 iframe 里的复选框，脚本点不到，必须人工点击
if (has('.cf-turnstile') || has('#turnstile-wrapper') || has('input[name="cf-turnstile-response"]')
    || has('iframe[src*="challenges.cloudflare.com"]')) strong.push('turnstile-widget');
// “Just a moment…” 全屏挑战页
if (has('#challenge-stage') || has('#challenge-form') || has('#cf-challenge-running')
    || has('#cf-please-wait') || has('#cf-wrapper') || has('#cf-chl-widget')
    || has('[id^="cf-chl"]')) strong.push('interstitial-dom');
if (/challenges\.cloudflare\.com/i.test(url)) strong.push('cf-host');
if (/^\s*(just a moment|attention required|one more step|checking your browser|请稍候|稍候|正在进行安全验证|正在验证|बस कुछ पल प्रतीक्षा करें|しばらくお待ちください|잠시만 기다려 주세요)/i.test(title)) strong.push('title');
if (/verify you are human|confirm you are human|确认您是真人|验证您是人类|verify you are a human/i.test(bodyText)) strong.push('turnstile-text');
if (/enable javascript and cookies to continue|checking your browser before accessing|正在检查你的浏览器|正在验证您的浏览器/i.test(bodyText)) strong.push('interstitial-text');
if (has('[data-sitekey]')) weak.push('sitekey');
if (/cdn-cgi\/challenge-platform/i.test(html)) weak.push('cf-platform');
const blocked = /you have been blocked|access denied|error 1020|sorry, you have been blocked|已被阻止|拒绝访问/i.test(title + ' ' + bodyText);
let kind = '';
if (blocked) kind = 'blocked';
else if (strong.indexOf('turnstile-widget') >= 0 || strong.indexOf('turnstile-text') >= 0) kind = 'turnstile';
else if (strong.length) kind = 'interstitial';
return {
  detected: blocked || strong.length > 0,
  kind: kind,
  url: url,
  title: title.slice(0, 120),
  signals: strong.concat(weak).slice(0, 8),
  text: bodyText.slice(0, 300)
};
"""


def detect(driver) -> dict:
    """检测当前页面是否是 Cloudflare 风控页。

    返回 ``{"detected": bool, "kind": "turnstile|interstitial|blocked", "url", "title",
    "signals": [...], "error": str}``；任何异常都返回 ``detected=False``（不能因为
    检测本身出问题就打断注册流程）。
    """
    try:
        info = driver.execute_script(_DETECT_JS)
    except Exception as exc:
        return {"detected": False, "kind": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    if not isinstance(info, dict):
        return {"detected": False, "kind": "", "error": "检测脚本未返回结果"}
    info["detected"] = bool(info.get("detected"))
    return info


def probe(driver) -> dict:
    """复检页面是否恢复正常（供 manual_intervention 定时调用）。"""
    info = detect(driver)
    if info.get("error"):
        return {
            "recovered": False,
            "status_text": "复检失败，稍后重试…",
            "detail": "页面检测异常：%s" % info.get("error"),
        }
    if info.get("detected"):
        kind = str(info.get("kind") or "interstitial")
        return {
            "recovered": False,
            "status_text": "仍是 Cloudflare %s，等待你手动处理…" % _kind_cn(kind),
            "detail": "页面标题：%s；命中特征：%s" % (
                info.get("title") or "-", ", ".join(info.get("signals") or []) or "-",
            ),
        }
    return {
        "recovered": True,
        "status_text": "页面已恢复正常，流程继续",
        "detail": "当前页面：%s" % (info.get("title") or info.get("url") or "-"),
    }


def _kind_cn(kind: str) -> str:
    return {"turnstile": "人机验证", "interstitial": "验证页", "blocked": "拦截页"}.get(str(kind), "验证页")


# ----------------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------------

def handle(
    driver,
    *,
    email: str | None = None,
    job_id: int | None = None,
    task: str = "",
    label: str = "",
) -> bool:
    """检测到 Cloudflare 风控页时弹框提醒用户并定时复检。

    返回 True 表示"确实检测到风控并且现在已经恢复，可以继续"；返回 False 表示
    当前页面没有风控（正常路径，零副作用）。
    """
    if not _enabled():
        return False
    info = detect(driver)
    if not info.get("detected"):
        return False

    kind = str(info.get("kind") or "interstitial")

    # 宽限期：实测 chatgpt.com 的托管挑战是**非交互**的、约 42 秒会自行放行。这段时间里
    # 既不该打扰用户，也不该让上层走"元素没找到 → refresh"的分支（refresh 会把挑战重置）。
    # 只有宽限期过后仍被风控，才登记人工介入。'blocked'（1020 拦截页）不会自愈，直接人工。
    grace = _grace_seconds()
    if grace > 0 and kind != "blocked":
        logger.warning(
            "%s 检测到 Cloudflare %s，先静默等待自动放行（最多 %ss）：url=%s title=%s 特征=%s",
            _log_prefix(label), _kind_cn(kind), grace,
            str(info.get("url") or "")[:200], info.get("title") or "-",
            ", ".join(info.get("signals") or []) or "-",
        )
        deadline = time.time() + grace
        interval = max(1, min(3, _poll_interval()))
        while time.time() < deadline:
            time.sleep(interval)
            info = detect(driver)
            if not info.get("detected"):
                logger.info(
                    "%s Cloudflare %s 已自行放行（静默等待上限 %ss），继续流程：url=%s",
                    _log_prefix(label), _kind_cn(kind), grace, str(info.get("url") or "")[:200],
                )
                return True
        kind = str(info.get("kind") or kind)
        logger.warning(
            "%s Cloudflare %s 在 %ss 内未自行放行，转为人工处理：url=%s title=%s",
            _log_prefix(label), _kind_cn(kind), grace,
            str(info.get("url") or "")[:200], info.get("title") or "-",
        )

    key = _key(job_id, email)
    if not _begin(key):
        return False

    page_url = str(info.get("url") or "")
    reason = "%s 命中特征：%s" % (label or "页面", ", ".join(info.get("signals") or []) or "-")
    logger.warning(
        "%s 检测到 Cloudflare 风控页（%s）：url=%s title=%s 特征=%s，等待用户手动处理",
        _log_prefix(label), _kind_cn(kind), page_url[:200], info.get("title") or "-",
        ", ".join(info.get("signals") or []) or "-",
    )
    try:
        result = manual_intervention.request_manual_intervention(
            kind=KIND,
            title="需要你手动完成 Cloudflare 验证",
            guide=_HINTS.get(kind, _HINTS["interstitial"]),
            status_text="已检测到 Cloudflare %s，请在浏览器窗口里手动处理…" % _kind_cn(kind),
            email=email,
            job_id=job_id,
            task=task or "注册",
            url=page_url,
            timeout=_timeout(),
            poll_interval=_poll_interval(),
            extra={
                "kind": kind,
                "page_title": str(info.get("title") or ""),
                "signals": list(info.get("signals") or []),
                "label": label,
            },
            probe=lambda: probe(driver),
        )
    except manual_intervention.ManualInterventionTimeout as exc:
        # 用户一直没处理：超时后自动结束本次任务（不再空等，也不再反复弹框）。
        logger.error(
            "%s 等待人工处理 Cloudflare 风控超时（%ss），自动结束本次任务：%s",
            _log_prefix(label), _timeout(), exc,
        )
        raise
    finally:
        _end(key)
    logger.info(
        "%s Cloudflare 风控已解除，继续流程：复检次数=%s 等待约 %ss（%s）",
        _log_prefix(label), result.get("checks"), result.get("waiting_seconds"), reason,
    )
    return True


def maybe_handle(driver, *, label: str = "") -> bool:
    """共享页面函数里的守卫：读任务上下文自动补齐 email/job_id。

    任何意外异常都被吞掉（只留 debug 日志）——风控提醒功能本身绝不能让注册流程变脆；
    但"用户取消/等待超时"是要让任务失败的，必须原样抛出。
    """
    try:
        ctx = current_context()
        return handle(
            driver,
            email=ctx.get("email"),
            job_id=ctx.get("job_id"),
            task=ctx.get("task") or "注册",
            label=label,
        )
    except (manual_intervention.ManualInterventionCancelled, manual_intervention.ManualInterventionTimeout):
        raise
    except Exception:
        logger.debug("Cloudflare 风控检测/等待失败（忽略）label=%s", label, exc_info=True)
        return False


def _log_prefix(label: str) -> str:
    return ("[Cloudflare]" + (" " + label if label else "")).strip()


def _enabled() -> bool:
    return bool(getattr(_cfg, "CF_CHALLENGE_MANUAL_WAIT", True))


def _timeout() -> int:
    """等待人工处理的上限：与「人工介入超时时间」(MANUAL_OTP_TIMEOUT) 共用同一个配置。"""
    try:
        return max(30, int(getattr(_email_cfg, "MANUAL_OTP_TIMEOUT", 600) or 600))
    except (TypeError, ValueError):
        return 600


def _poll_interval() -> int:
    try:
        return max(1, int(getattr(_cfg, "CF_CHALLENGE_POLL_INTERVAL", 5) or 5))
    except (TypeError, ValueError):
        return 5


def _grace_seconds() -> int:
    """检测到风控后先静默等待挑战自行放行的秒数（0 表示立刻弹框）。"""
    try:
        return max(0, int(getattr(_cfg, "CF_CHALLENGE_GRACE_SECONDS", 50) or 0))
    except (TypeError, ValueError):
        return 50
