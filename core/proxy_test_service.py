# -*- coding: utf-8 -*-
"""代理池连通性测试：随机抽取一个代理，复用账号查活的 curl_cffi 指纹环境请求 chatgpt.com。

与账号查活（``core/live_check_service.py`` + ``core/account_liveness.py``）保持同一套请求方式：

    - curl_cffi ``Session(impersonate=config.IMPERSONATE)`` 负责 TLS/JA3 指纹；
    - ``BrowserSession`` 负责 UA / Accept-Language / sec-ch-ua / 时区 / 屏幕等画像，
      并先按代理出口 IP 探测 Geo，让语言与时区跟随真实出口；
    - ``oai-did`` / ``oai-locale`` Cookie 与请求头保持同一设备上下文；
    - 顶层文档导航（无 Referer，``Sec-Fetch-Site: none``）+ 12s 超时，
      与查活预热 `/auth/login` 用的是同一个 ``get_chatgpt_navigate_headers``；
    - 文档返回后同步 ChatGPT 前端 build/seq 元数据。

探测两个地址：
    1. ``https://chatgpt.com/``：首页。冷会话经常直接被 Cloudflare 挑战（403 +
       cf-mitigated=challenge），单独看它会误判成"代理不可用"；
    2. ``https://chatgpt.com/auth/login``：查活网络预检的第一个顶层导航，
       它返回 2xx/3xx 才说明这个出口能真正走通查活链路。

综合两者给出结论，并把完整指纹环境写入实时日志。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import IMPERSONATE, USER_AGENT
from core.chatgpt_plan import _mask_proxy
from core.session import BrowserSession

try:  # curl_cffi 是项目硬依赖，这里保留防御式导入便于在无依赖环境下做导入检查
    from curl_cffi.requests import Session
except Exception:  # pragma: no cover
    Session = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CHATGPT_HOME_URL = "https://chatgpt.com/"
# 与查活网络预检的第一步一致：登录页顶层导航。
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
# 与查活登录页预热一致：代理端口可连接不代表其上游 TLS 可用，避免坏节点长期占住请求。
TEST_TIMEOUT = 12
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"

_RUN_LOCK = threading.Lock()
_RUNNING = False
_LAST_RESULT: dict | None = None
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def log_path() -> Path:
    """代理测试实时日志文件（WebUI 弹框轮询展示）。"""
    return _LOG_DIR / "proxy-test.log"


def is_running() -> bool:
    with _RUN_LOCK:
        return _RUNNING


def last_result() -> dict | None:
    return dict(_LAST_RESULT) if isinstance(_LAST_RESULT, dict) else None


def _log(line: str, *, clear: bool = False) -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    with p.open("w" if clear else "a", encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _pick_from_pool(pool) -> tuple[str, int, str]:
    """随机抽取本次要测试的代理出口。

    优先使用请求带入的代理列表（可能是配置页里尚未保存的文本），
    否则使用已保存的 ``config.PROXY_POOL``。
    返回 (代理, 池大小, 来源)。
    """
    from config import proxy as proxy_cfg

    if pool:
        candidates = proxy_cfg.normalize_proxy_list(pool)
        if candidates:
            return random.choice(candidates), len(candidates), "request"

    try:
        configured = [str(item).strip() for item in (proxy_cfg.PROXY_POOL or []) if str(item).strip()]
    except Exception:
        configured = []
    if configured:
        # 与查活/查套餐一致：走 pick_proxy() 的自适应健康选路。
        return str(proxy_cfg.pick_proxy() or "").strip(), len(configured), "config"
    return "", 0, "config"


def _extract_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120]


def _log_fingerprint(session: BrowserSession) -> None:
    """把与查活一致的完整请求指纹环境写入实时日志。"""
    profile = dict(getattr(session, "browser_profile", {}) or {})
    geo = getattr(session, "exit_geo", {}) or {}
    _log(f"[指纹] TLS/JA3：curl_cffi impersonate={IMPERSONATE}（TLS 指纹由 curl_cffi 自动匹配）")
    _log(
        "[指纹] 设备/会话标识："
        f"device_id(oai-did)={session.device_id} "
        f"oai_session_id={session.oai_session_id} "
        f"auth_session_logging_id={session.auth_session_logging_id} "
        f"sentinel_sid={session.sentinel_sid} "
        f"sentinel_iframe_sid={session.sentinel_iframe_sid} "
        f"document_navigation_id={session.document_navigation_id}"
    )
    _log(
        "[指纹] RUM/trace："
        f"datadog_trace_id={session.datadog_trace_id} "
        f"datadog_parent_id={session.datadog_parent_id} origin={session.datadog_origin}"
    )
    _log(
        "[指纹] 浏览器画像："
        f"family={profile.get('browser_family')} os={profile.get('browser_os')} "
        f"client_version={session.client_version} client_build_number={session.client_build_number}"
    )
    _log(f"[指纹] User-Agent：{profile.get('user_agent') or USER_AGENT}")
    _log(
        "[指纹] 语言："
        f"accept_language={profile.get('accept_language')} "
        f"navigator.language={profile.get('navigator_language')} "
        f"navigator.languages={profile.get('navigator_languages')}"
    )
    _log(
        "[指纹] 时区："
        f"{profile.get('timezone_iana')} "
        f"(offset={profile.get('timezone_offset_minutes')}min, "
        f"JS getTimezoneOffset={session.js_timezone_offset_min()}min, name={profile.get('timezone_name')})"
    )
    _log(
        "[指纹] 屏幕/硬件："
        f"{profile.get('screen_width')}x{profile.get('screen_height')} "
        f"@dpr={profile.get('device_pixel_ratio')} "
        f"cpu={profile.get('hardware_concurrency')} mem={profile.get('device_memory')} "
        f"jsHeap={profile.get('js_heap_size_limit')}"
    )
    _log(
        "[指纹] Client Hints："
        f"sec-ch-ua={profile.get('sec_ch_ua')} "
        f"platform={profile.get('sec_ch_ua_platform')} "
        f"platform-version={profile.get('sec_ch_ua_platform_version')} "
        f"mobile={profile.get('sec_ch_ua_mobile')} "
        f"arch={profile.get('sec_ch_ua_arch')} "
        f"bitness={profile.get('sec_ch_ua_bitness')} "
        f"model={profile.get('sec_ch_ua_model')}"
    )
    if profile.get("sec_ch_ua_full_version_list"):
        _log(f"[指纹] Client Hints (high-entropy)：{profile.get('sec_ch_ua_full_version_list')}")
    _log(
        "[指纹] React/DOM key："
        f"{profile.get('react_listening_key')} "
        f"{profile.get('react_container_key')} "
        f"{profile.get('react_resources_key')}"
    )
    _log(
        "[指纹] 出口 Geo："
        f"ip={geo.get('ip') or '?'} country={geo.get('country') or '?'} "
        f"region={geo.get('region') or '?'} city={geo.get('city') or '?'} "
        f"timezone={geo.get('timezone') or '?'} org={geo.get('org') or '?'}"
    )
    _log(
        "[指纹] Cookie Jar：oai-did / oai-locale 已按画像写入 chatgpt.com、auth.openai.com；"
        f"Cloudflare Cookie={session.cf_cookie_snapshot() or '（暂无）'}"
    )
    try:
        _log("[指纹] 完整画像 JSON：" + json.dumps(profile, ensure_ascii=False, sort_keys=True))
    except Exception as exc:  # pragma: no cover - 仅防御不可序列化画像
        logger.debug("[代理测试] 画像序列化失败：%s", exc)


def _log_request_headers(headers: dict) -> None:
    safe = {k: v for k, v in (headers or {}).items() if k.lower() not in {"cookie", "authorization"}}
    try:
        _log("[请求] 请求头：" + json.dumps(safe, ensure_ascii=False, sort_keys=True))
    except Exception:
        _log(f"[请求] 请求头：{safe}")


def _probe_document(session: BrowserSession, url: str, label: str) -> dict:
    """按查活的顶层文档导航方式请求一个 URL，并把过程写入实时日志。

    这里直接使用 ``session.session.get``（裸 curl_cffi），不走 BrowserSession 的
    会话熔断与代理健康上报：代理测试属于只读诊断，首页被 CF 挑战是常态，
    不应该把该出口在自适应代理池里打成冷却。
    """
    headers = session.get_chatgpt_navigate_headers(referer="", user_initiated=True)
    _log(
        f"[请求] {label}：顶层文档导航 GET {url}"
        f"（无 Referer、Sec-Fetch-Site=none、allow_redirects=True、timeout={TEST_TIMEOUT}s）"
    )
    _log_request_headers(headers)
    t0 = time.time()
    resp = session.session.get(url, headers=headers, allow_redirects=True, timeout=TEST_TIMEOUT)
    elapsed_ms = int((time.time() - t0) * 1000)

    status_code = int(getattr(resp, "status_code", 0) or 0)
    resp_headers = getattr(resp, "headers", {}) or {}
    text = str(getattr(resp, "text", "") or "")
    title = _extract_title(text)
    cf_mitigated = str(resp_headers.get("cf-mitigated") or "")
    challenge = (
        bool(cf_mitigated)
        or "just a moment" in title.lower()
        or "challenges.cloudflare.com" in text[:200000]
    )
    final_url = str(getattr(resp, "url", "") or url)

    _log(
        f"[响应] {label}：HTTP {status_code} {getattr(resp, 'reason', '') or ''} "
        f"耗时 {elapsed_ms} ms 最终 URL={final_url}"
    )
    _log(
        f"[响应] {label}：server={resp_headers.get('server') or '?'} "
        f"cf-ray={resp_headers.get('cf-ray') or '?'} cf-mitigated={cf_mitigated or '-'} "
        f"content-type={resp_headers.get('content-type') or '?'} "
        f"title={title or '（无）'} 正文长度={len(text)} 字符"
    )
    _log(f"[响应] {label}：Cloudflare Cookie={session.cf_cookie_snapshot() or '（暂无）'}")

    # 与查活一致：从真实文档同步 ChatGPT 前端 build/seq，供后续请求复用同一版本上下文。
    try:
        session.observe_chatgpt_document(resp)
    except Exception:
        pass

    return {
        "label": label,
        "url": url,
        "status_code": status_code,
        "final_url": final_url,
        "elapsed_ms": elapsed_ms,
        "title": title,
        "content_length": len(text),
        "server": str(resp_headers.get("server") or ""),
        "cf_ray": str(resp_headers.get("cf-ray") or ""),
        "cf_mitigated": cf_mitigated,
        "challenge": challenge,
        "ok": 200 <= status_code < 400,
    }


def _run_test(selected: str, pool_size: int, pool_source: str) -> dict:
    """在工作线程中执行一次代理测试，全程写实时日志。"""
    result = {
        "ok": False,
        "status": "failed",
        "proxy": _mask_proxy(selected) or "直连",
        "proxy_mode": "proxy" if selected else "direct",
        "pool_size": pool_size,
        "pool_source": pool_source,
        "url": CHATGPT_HOME_URL,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": 0,
        "status_code": None,
    }
    session: BrowserSession | None = None
    started = time.time()
    try:
        _log(
            f"[代理测试] 代理来源={pool_source} 池大小={pool_size} "
            f"本次随机出口={_mask_proxy(selected) or '直连'}"
        )
        _log("[代理测试] 创建 BrowserSession 指纹环境（与账号查活同一套画像 + 出口 Geo 探测）…")
        session = BrowserSession(
            proxy=selected,
            detect_exit_geo=True,
            fingerprint_seed=f"proxy-test:{uuid.uuid4()}",
        )
        _log("[代理测试] 指纹环境已就绪，浏览器画像与设备标识如下：")
        _log_fingerprint(session)

        home = _probe_document(session, CHATGPT_HOME_URL, "首页")
        login = _probe_document(session, CHATGPT_LOGIN_URL, "查活入口页 /auth/login")

        elapsed_ms = int((time.time() - started) * 1000)
        _log(
            f"[指纹] 文档同步后的前端版本：client_version={session.client_version} "
            f"client_build_number={session.client_build_number}"
        )

        home_ok = bool(home["ok"])
        login_ok = bool(login["ok"])
        ok = home_ok or login_ok
        if login_ok:
            status = "ok"
            if home_ok:
                message = f"代理可用：首页 HTTP {home['status_code']}，查活入口页 HTTP {login['status_code']}，耗时 {elapsed_ms} ms"
            else:
                message = (
                    f"代理可用：查活入口页 HTTP {login['status_code']} 正常"
                    f"（首页 HTTP {home['status_code']} 被 Cloudflare 挑战，冷会话属正常），耗时 {elapsed_ms} ms"
                )
        elif home["challenge"] or login["challenge"] or home["status_code"] in (403, 429) or login["status_code"] in (403, 429):
            status = "blocked"
            message = (
                f"代理可连通，但被 Cloudflare 拦截：首页 HTTP {home['status_code']}、"
                f"查活入口页 HTTP {login['status_code']}，耗时 {elapsed_ms} ms"
            )
        else:
            status = "failed"
            message = f"代理请求未成功：首页 HTTP {home['status_code']}、查活入口页 HTTP {login['status_code']}，耗时 {elapsed_ms} ms"

        result.update({
            "ok": ok,
            "status": status,
            "message": message,
            "status_code": home["status_code"],
            "final_url": home["final_url"],
            "elapsed_ms": elapsed_ms,
            "title": home["title"],
            "server": home["server"],
            "cf_ray": home["cf_ray"],
            "cf_mitigated": home["cf_mitigated"],
            "challenge": bool(home["challenge"] or login["challenge"]),
            "home": home,
            "login": login,
            "geo": dict(getattr(session, "exit_geo", {}) or {}),
            "fingerprint_text": session.fingerprint_summary_text(),
        })
        _log(f"[代理测试] 结论：{message}")
        _log("[代理测试] ========== 测试结束 ==========")
        return result
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        err = f"{type(exc).__name__}: {str(exc)[:400]}"
        # curl_cffi 的异常里可能回显完整代理串；统一替换成脱敏形式，避免凭据落进日志/接口结果。
        if selected and selected in err:
            err = err.replace(selected, _mask_proxy(selected) or "***")
        result.update({
            "ok": False,
            "status": "failed",
            "elapsed_ms": elapsed_ms,
            "error": err,
            "message": f"代理测试失败：{err}",
        })
        if session is not None:
            try:
                result["geo"] = dict(getattr(session, "exit_geo", {}) or {})
                result["fingerprint_text"] = session.fingerprint_summary_text()
            except Exception:
                pass
        logger.warning("[代理测试] 失败：%s", err)
        _log(f"[代理测试] 失败：{err}")
        _log("[代理测试] ========== 测试结束 ==========")
        return result
    finally:
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass


def _worker(selected: str, pool_size: int, pool_source: str) -> None:
    global _RUNNING, _LAST_RESULT
    try:
        _LAST_RESULT = _run_test(selected, pool_size, pool_source)
    except Exception as exc:  # pragma: no cover - 兜底，避免线程静默退出
        logger.exception("[代理测试] 后台异常")
        err = f"{type(exc).__name__}: {exc}"
        if selected and selected in err:
            err = err.replace(selected, _mask_proxy(selected) or "***")
        _LAST_RESULT = {
            "ok": False,
            "status": "failed",
            "proxy": _mask_proxy(selected) or "直连",
            "error": err,
            "message": "代理测试后台异常",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            _log(f"[代理测试] 后台异常：{err}")
        except Exception:
            pass
    finally:
        with _RUN_LOCK:
            _RUNNING = False


def start_test(pool=None) -> dict:
    """启动一次后台代理测试。``pool`` 可传入尚未保存的代理列表（每行一个）。"""
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            return {"accepted": False, "running": True, "error": "已有代理测试正在执行，请等待结束"}
        _RUNNING = True

    selected, pool_size, pool_source = _pick_from_pool(pool)
    if not selected:
        with _RUN_LOCK:
            _RUNNING = False
        _log("[代理测试] 代理池为空：请先在「代理池」里填写代理 URL，再点击测试", clear=True)
        return {"accepted": False, "running": False, "error": "代理池为空，请先填写代理"}

    _log("[代理测试] ========== 代理连通性测试开始 ==========", clear=True)
    _log(
        f"[代理测试] 本次随机抽取：代理来源={pool_source} 池大小={pool_size} "
        f"出口={_mask_proxy(selected)}"
    )
    _log(f"[代理测试] 探测地址：{CHATGPT_HOME_URL} 与 {CHATGPT_LOGIN_URL}")
    try:
        threading.Thread(
            target=_worker,
            args=(selected, pool_size, pool_source),
            name="proxy-test",
            daemon=True,
        ).start()
    except Exception as exc:
        with _RUN_LOCK:
            _RUNNING = False
        _log(f"[代理测试] 启动线程失败：{type(exc).__name__}: {exc}")
        return {"accepted": False, "running": False, "error": f"启动失败：{exc}"}

    return {
        "accepted": True,
        "running": True,
        "proxy": _mask_proxy(selected) or "直连",
        "pool_size": pool_size,
        "pool_source": pool_source,
    }


# ============================================================
# 出口信息快速测试（IP2Location）
#
# 与上面的「深度指纹测试」不同，这里只关心"这个代理能不能用、出口在哪"，
# 因此直接用 curl_cffi 会话请求出口信息接口，1~3 秒即可出结果，
# 适合分组代理池里逐个 / 一键批量测试。
# ============================================================

# 默认接口（可被 config.proxy.PROXY_TEST_GEO_URLS 覆盖）
_DEFAULT_GEO_URLS = [
    "https://api.ip2location.io/?format=json",
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
]
_BATCH_LOG_NAME = "proxy-batch-test.log"

_BATCH_LOCK = threading.Lock()
_BATCH_RUNNING = False
_BATCH_STATE: dict = {"total": 0, "done": 0, "results": [], "started_at": ""}


def batch_log_path() -> Path:
    return _LOG_DIR / _BATCH_LOG_NAME


def _batch_log(line: str, *, clear: bool = False) -> None:
    p = batch_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    with p.open("w" if clear else "a", encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _geo_urls() -> list[str]:
    try:
        from config import proxy as proxy_cfg

        urls = [str(u).strip() for u in (getattr(proxy_cfg, "PROXY_TEST_GEO_URLS", []) or []) if str(u).strip()]
    except Exception:
        urls = []
    return urls or list(_DEFAULT_GEO_URLS)


def _geo_timeout() -> float:
    try:
        from config import proxy as proxy_cfg

        value = float(getattr(proxy_cfg, "PROXY_TEST_TIMEOUT", 12.0) or 12.0)
    except Exception:
        value = 12.0
    return max(3.0, min(60.0, value))


def _first(data: dict, *keys) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = value.get("name") or value.get("id")
            if nested:
                return str(nested)
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_geo_payload(data: dict) -> dict:
    """把不同出口信息接口的响应归一成统一结构。"""
    if not isinstance(data, dict):
        return {}
    # ip2location: {"ip","country_code","country_name","region_name","city_name","time_zone","as","is_proxy"}
    country = _first(data, "country_code", "countryCode", "country", "country_code2")
    return {
        "ip": _first(data, "ip", "query", "ipAddress"),
        "country": country,
        "country_name": _first(data, "country_name", "countryName", "country"),
        "region": _first(data, "region_name", "region", "regionName", "region_name"),
        "city": _first(data, "city_name", "city"),
        "timezone": _first(data, "time_zone", "timezone", "timeZone"),
        "org": _first(data, "as", "org", "asn", "connection", "isp", "organization"),
        "is_proxy": data.get("is_proxy") if isinstance(data.get("is_proxy"), bool) else None,
    }


def test_proxy_geo(proxy: str, *, timeout: float | None = None) -> dict:
    """测试单个代理出口：请求 IP2Location 等出口信息接口，返回出口 IP / 国家 / 耗时。

    同步执行，供前端「测试」按钮逐个调用。
    """
    from config import IMPERSONATE

    value = str(proxy or "").strip()
    result: dict = {
        "ok": False,
        "proxy": _mask_proxy(value) or "直连",
        "proxy_raw": value,
        "elapsed_ms": 0,
        "channel": "",
        "geo": {},
        "status_code": None,
        "error": "",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    urls = _geo_urls()
    wait = float(timeout if timeout is not None else _geo_timeout())
    session = None
    started = time.time()
    errors: list[str] = []
    try:
        session = Session(impersonate=IMPERSONATE)
        session.timeout = wait
        if value:
            session.proxies = {"http": value, "https": value}
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        for url in urls:
            try:
                resp = session.get(url, headers=headers, timeout=wait)
                status = int(getattr(resp, "status_code", 0) or 0)
                if status != 200:
                    errors.append(f"{url} -> HTTP {status}")
                    continue
                data = resp.json()
                geo = normalize_geo_payload(data if isinstance(data, dict) else {})
                if not geo.get("ip"):
                    errors.append(f"{url} -> 未返回 IP")
                    continue
                result.update({
                    "ok": True,
                    "status_code": status,
                    "channel": url,
                    "geo": geo,
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                return result
            except Exception as exc:  # 单个接口失败继续尝试下一个
                errors.append(f"{url} -> {type(exc).__name__}: {str(exc)[:160]}")
        result["error"] = "；".join(errors)[:400] or "所有出口信息接口均不可用"
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def batch_state() -> dict:
    with _BATCH_LOCK:
        return {
            "running": _BATCH_RUNNING,
            "total": int(_BATCH_STATE.get("total") or 0),
            "done": int(_BATCH_STATE.get("done") or 0),
            "started_at": str(_BATCH_STATE.get("started_at") or ""),
            "results": [dict(item) for item in (_BATCH_STATE.get("results") or [])],
        }


def _batch_worker(proxies: list[str], label: str) -> None:
    global _BATCH_RUNNING
    from concurrent.futures import ThreadPoolExecutor

    try:
        _batch_log(
            f"开始批量测试：分组={label or '-'} 共 {len(proxies)} 个代理，"
            f"接口={_geo_urls()[0]} 超时={_geo_timeout():.0f}s",
        )
        with ThreadPoolExecutor(max_workers=min(5, max(1, len(proxies))), thread_name_prefix="proxy-batch") as pool:
            futures = {pool.submit(test_proxy_geo, item): item for item in proxies}
            for future in futures:
                item = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "ok": False,
                        "proxy": _mask_proxy(item),
                        "proxy_raw": item,
                        "error": f"{type(exc).__name__}: {exc}",
                        "geo": {},
                    }
                with _BATCH_LOCK:
                    _BATCH_STATE["results"].append(result)
                    _BATCH_STATE["done"] = int(_BATCH_STATE.get("done") or 0) + 1
                geo = result.get("geo") or {}
                if result.get("ok"):
                    _batch_log(
                        f"[{result.get('proxy')}] 可用 出口={geo.get('ip')} "
                        f"地区={geo.get('country') or '?'} {geo.get('city') or ''} "
                        f"运营商={geo.get('org') or '?'} 耗时={result.get('elapsed_ms')}ms"
                    )
                else:
                    _batch_log(f"[{result.get('proxy')}] 失败 {result.get('error') or '未知原因'}")
        _batch_log("批量测试结束")
    finally:
        with _BATCH_LOCK:
            _BATCH_RUNNING = False


def start_batch_test(proxies, label: str = "") -> dict:
    """一键测试一组代理（后台执行，前端轮询 batch_state）。"""
    global _BATCH_RUNNING
    items: list[str] = []
    seen: set[str] = set()
    for raw in (proxies or []):
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    if not items:
        _batch_log("批量测试未执行：代理列表为空", clear=True)
        return {"accepted": False, "error": "代理列表为空"}
    with _BATCH_LOCK:
        if _BATCH_RUNNING:
            return {"accepted": False, "running": True, "error": "已有批量测试正在执行"}
        _BATCH_RUNNING = True
        _BATCH_STATE.update({
            "total": len(items),
            "done": 0,
            "results": [],
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })
    _batch_log("", clear=True)
    try:
        threading.Thread(
            target=_batch_worker,
            args=(items, label),
            name="proxy-batch-test",
            daemon=True,
        ).start()
    except Exception as exc:
        with _BATCH_LOCK:
            _BATCH_RUNNING = False
        _batch_log(f"启动批量测试失败：{type(exc).__name__}: {exc}")
        return {"accepted": False, "error": f"启动失败：{exc}"}
    return {"accepted": True, "running": True, "total": len(items)}
