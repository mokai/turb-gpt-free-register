# -*- coding: utf-8 -*-
"""dasaobi 公益提链后台队列（独立渠道，不侵入官方提链服务）。

接入 dasaobi 公益站（https://dasaobi.online/upi/）的异步提链 API：

    1. POST api/tasks        提交 Access Token + 印度随机出口代理 -> task_id
    2. 轮询 GET api/tasks/{task_id}
    3. status=succeeded 取 result.upi_url；failed 取 error；cancelled 表示已取消

与官方提链（core/extract_link_service.py，CDK + SSE）完全独立，本文件不 import 它，
也不改动它。账号提链状态复用 db 的 extract_link_* 字段族，因此前端展示层零改动。

Cookie 语义：公益站首次访问任务接口下发 upi_paid_session（Secure/HttpOnly，路径
/upi/，24h）。同一 Cookie 下同一 AT 的未结束任务可能去重。按账号 email 各自保存
Cookie 文件，避免不同账号共用一个 Cookie 造成串号。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None

from config import upi_public as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """每次读取时重新加载 .env，避免“页面已保存但当前进程仍读到空值”。"""
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _api_base() -> str:
    base = str(_runtime_setting("UPI_PUBLIC_API_BASE", "https://dasaobi.online/upi/") or "").strip()
    if not base:
        raise ValueError("UPI_PUBLIC_API_BASE 为空")
    return base.rstrip("/") + "/"


_WORKERS = _int_setting("UPI_PUBLIC_WORKERS", 2, 1, 16)
_QUEUE_LIMIT = _int_setting("UPI_PUBLIC_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="upi-public")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _cookie_dir() -> Path:
    raw = str(_runtime_setting("UPI_PUBLIC_COOKIE_DIR", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    # 默认落在项目 data 下，避免污染用户主目录
    return Path(__file__).resolve().parent.parent / "run" / "upi-public-cookies"


def _cookie_path(email: str) -> Path:
    digest = hashlib.sha1(str(email or "").strip().lower().encode("utf-8")).hexdigest()[:24]
    return _cookie_dir() / f"{digest}.cookies"


def _load_cookie(email: str) -> str | None:
    path = _cookie_path(email)
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None


def _save_cookie(email: str, session) -> None:
    if session is None:
        return
    raw = session.cookies.get("upi_paid_session")
    if not raw:
        return
    try:
        path = _cookie_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(raw).strip(), encoding="utf-8")
    except Exception:
        logger.exception("[dasaobi] 保存 Cookie 失败: %s", email)


def _session(email: str):
    """构造带 Cookie 的 requests Session；requests 不可用则抛错（公益站仅支持 HTTPS JSON）。"""
    if _requests is None:
        raise RuntimeError("当前环境未安装 requests，无法调用 dasaobi 公益提链")
    s = _requests.Session()
    cookie = _load_cookie(email)
    if cookie:
        s.cookies.set("upi_paid_session", cookie, path="/upi/")
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s


def _validate_proxy(value: str) -> str:
    """校验代理 URL 基础格式；只允许公网 host:port。"""
    proxy = str(value or "").strip()
    if not proxy:
        raise ValueError("缺少代理")
    from urllib.parse import urlparse
    parsed = urlparse(proxy)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        raise ValueError(f"代理协议不支持: {parsed.scheme or '(空)'}")
    host = (parsed.hostname or "").strip()
    if not host or not parsed.port:
        raise ValueError("代理缺少公网 host 或 port")
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        raise ValueError("代理地址不可为本地/内网地址")
    return proxy


def _pick_proxy() -> str:
    """从公益提链专用代理池选一个印度出口；为空则回退通用默认分组随机出口。

    兼容 .env 里该字段的三种写法：多行字符串（每行一个代理）、JSON 数组字面量、
    以及 WebUI 写回的空列表占位 ``[]``。
    """
    raw = str(_runtime_setting("UPI_PUBLIC_PROXY_POOL", "") or "").strip()
    pool: list[str] = []
    if raw:
        try:
            import ast
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, (list, tuple)):
                pool = [str(x).strip() for x in parsed if str(x).strip()]
            else:
                pool = [line.strip() for line in raw.splitlines() if line.strip()]
        except Exception:
            pool = [line.strip() for line in raw.splitlines() if line.strip()]
    if pool:
        import random
        proxy = random.choice(pool)
    else:
        try:
            from config import proxy as proxy_cfg
            proxy = proxy_cfg.pick_proxy()
        except Exception:
            proxy = ""
    return _validate_proxy(proxy)


def _extract_error(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _post(session, url, payload: dict, *, timeout: int) -> dict:
    resp = session.post(url, json=payload, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {"error": (resp.text or "")[:300]}
    if not isinstance(data, dict):
        data = {"error": str(data)[:300]}
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(_extract_error(data) or f"HTTP {resp.status_code}")
    return data


def _get(session, url, *, timeout: int) -> dict:
    resp = session.get(url, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {"error": (resp.text or "")[:300]}
    if not isinstance(data, dict):
        data = {"error": str(data)[:300]}
    if resp.status_code == 404:
        raise RuntimeError(_extract_error(data) or "任务不存在或已清理")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(_extract_error(data) or f"HTTP {resp.status_code}")
    return data


def _submit_task(session, *, access_token: str, proxy: str, timeout: int) -> dict:
    base = _api_base()
    payload = {
        "access_token": access_token,
        "checkout_proxy": proxy,
        "update_proxy": proxy,
    }
    data = _post(session, urljoin(base, "api/tasks"), payload, timeout=timeout)
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"公益站未返回 task_id: {data}")
    return data


def _poll_task(session, *, task_id: str, timeout: int) -> dict:
    base = _api_base()
    return _get(session, urljoin(base, f"api/tasks/{task_id}"), timeout=timeout)


def _run_public(*, account_id: int, email: str, access_token: str, trigger: str) -> dict:
    req_timeout = _int_setting("UPI_PUBLIC_REQUEST_TIMEOUT", 30, 5, 300)
    poll_interval = _float_setting("UPI_PUBLIC_POLL_INTERVAL", 2.0, 0.5, 60.0)
    poll_timeout = _int_setting("UPI_PUBLIC_POLL_TIMEOUT", 600, 10, 7200)
    session = None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}

        proxy = _pick_proxy()
        session = _session(email)

        submit = _submit_task(session, access_token=access_token, proxy=proxy, timeout=req_timeout)
        task_id = str(submit.get("task_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": task_id,
            "link_type": "upi_public",
            "message": "公益提链任务已提交，等待公益站处理",
        })

        deadline = time.time() + poll_timeout
        while True:
            if time.time() >= deadline:
                raise RuntimeError(f"公益提链轮询超时（>{poll_timeout}s）")
            data = _poll_task(session, task_id=task_id, timeout=req_timeout)
            status = str(data.get("status") or "")
            if status == "succeeded":
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                upi_url = str(result.get("upi_url") or "").strip()
                if not upi_url:
                    raise RuntimeError("公益站返回 succeeded 但 result.upi_url 为空")
                final = {
                    "ok": True,
                    "status": "success",
                    "job_id": task_id,
                    "link_type": "upi_public",
                    "result": {
                        "long_url": upi_url,
                        "copy_paste": upi_url,
                        "upi_url": upi_url,
                        "payment_method": result.get("payment_method") or "upi",
                        "amount_due_minor": result.get("amount_due_minor"),
                        "currency": result.get("currency") or "INR",
                        "billing_country": result.get("billing_country") or "IN",
                    },
                    "logs": [],
                }
                db.update_account_extract(account_id, final)
                logger.info("[dasaobi] 成功: %s task=%s", email, task_id)
                return final
            if status == "failed":
                raise RuntimeError(_extract_error(data) or "公益提链任务失败")
            if status == "cancelled":
                raise RuntimeError("公益提链任务已取消")
            # queued / running / cancel_requested：继续轮询
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "job_id": task_id,
                "link_type": "upi_public",
                "message": f"公益站处理中（{status}）",
            })
            time.sleep(poll_interval)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)}".strip()[:500]
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[dasaobi] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[dasaobi] 失败: %s", email)
        return result
    finally:
        _save_cookie(email, session)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        _QUEUE_SLOTS.release()


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual") -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "公益提链队列已满"}
    try:
        if not db.claim_account_extract(account_id, trigger=trigger, link_type="upi_public"):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(
            _run_public,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=trigger,
        )
        return {"accepted": True, "busy": False, "future": fut, "link_type": "upi_public"}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
