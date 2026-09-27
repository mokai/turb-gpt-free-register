# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan

logger = logging.getLogger(__name__)


def log_path(email: str) -> Path:
    """某邮箱最近一次套餐查询的独立日志文件。"""
    safe = str(email or "unknown").strip().replace("/", "_").replace("\\", "_") or "unknown"
    return Path("logs") / f"plan-check-{safe}.log"


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    try:
        p = log_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        mode = "w" if clear else "a"
        with p.open(mode, encoding="utf-8") as f:
            f.write(f"{stamp} [INFO] {line}\n")
    except Exception:
        logger.debug("[Plan] 写查询日志失败: email=%s", email, exc_info=True)


def is_checking(email: str) -> bool:
    """该账号当前是否有套餐查询在排队/执行（读数据库状态）。"""
    try:
        acc = db.get_account_by_email(str(email or "").strip())
    except Exception:
        return False
    if not acc:
        return False
    return str(acc.get("plan_check_status") or "") in {"queued", "running"}


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    groups: list[str] | None = None,
) -> dict:
    """执行套餐查询。

    groups 为空表示走全局网络配置，结果记到默认分组；
    否则按顺序对每个选中的代理分组各查一次，分别记录结果
    （不同地区出口看到的 Plus 试用资格可能不同，因此按分组分开保存）。
    """
    try:
        if not db.mark_account_plan_check_running(account_id):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}

        from config.proxy import DEFAULT_PROXY_GROUP, resolve_group_pool

        group_names = [str(g).strip() for g in (groups or []) if str(g or "").strip()]
        if not group_names:
            group_names = [DEFAULT_PROXY_GROUP]
        # 本轮只查这几个分组：先把账号上其它分组的旧缓存清掉，否则换过分组的账号
        # 会一直挂着上一轮分组的标签（用户看到的就是"查询还在用缓存"）。
        # 清理失败只记日志，不能因此让本次查询失败。
        try:
            db.scope_account_plan_groups(account_id, group_names)
        except Exception:
            logger.warning("[Plan] 收敛旧分组缓存失败: account_id=%s", account_id, exc_info=True)

        last_result: dict = {"ok": False, "error": "未执行任何查询"}
        summary: list[dict] = []
        for group_name in group_names:
            group_pool = None if group_name == DEFAULT_PROXY_GROUP and not groups else resolve_group_pool(group_name)
            if group_pool is None:
                # 分组不存在（可能已被删除）：回退全局配置，避免整条查询直接失败。
                group_pool = resolve_group_pool(DEFAULT_PROXY_GROUP)
            _wait_for_rate_slot()
            result = _check_once(
                access_token=access_token,
                proxy=proxy,
                pool=group_pool,
                timezone_offset_min=timezone_offset_min,
                trigger=trigger,
                email=email,
                group_name=group_name,
            )
            db.update_account_plan_check(acc_id=account_id, result=result, group=group_name)
            if result.get("ok"):
                _append_log(
                    email,
                    f"[套餐] 分组「{group_name}」查询成功：plan={result.get('current_plan_type') or '-'} "
                    f"plus_trial={'是' if result.get('plus_trial_eligible') else '否'}",
                )
            else:
                _append_log(
                    email,
                    f"[套餐] 分组「{group_name}」查询失败：{result.get('error') or '未知错误'}",
                )
            summary.append({
                "group": group_name,
                "ok": bool(result.get("ok")),
                "plan": result.get("current_plan_type"),
                "plus_trial_eligible": bool(result.get("plus_trial_eligible")),
                "error": result.get("error"),
            })
            last_result = result
            if len(group_names) > 1:
                logger.info(
                    "[Plan] 分组查询完成: %s group=%s ok=%s plan=%s trial=%s",
                    email, group_name, result.get("ok"),
                    result.get("current_plan_type") or "-", bool(result.get("plus_trial_eligible")),
                )
        last_result = dict(last_result)
        last_result["groups_checked"] = summary
        return last_result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _check_once(
    *,
    access_token: str,
    proxy: str | None,
    pool: list[str] | None,
    timezone_offset_min: str,
    trigger: str,
    email: str,
    group_name: str,
) -> dict:
    """单次查询（含新账号资格复查）。"""
    result = check_account_plan(
        access_token,
        proxy=proxy,
        pool=pool,
        timezone_offset_min=timezone_offset_min,
    )

    recheck_delay = _registration_recheck_delay()
    should_recheck = (
        trigger == "registration_auto"
        and recheck_delay > 0
        and bool(result.get("ok"))
        and str(result.get("current_plan_type") or "").lower() == "free"
        and not bool(result.get("plus_trial_eligible"))
    )
    if should_recheck:
        logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
        time.sleep(recheck_delay)
        _wait_for_rate_slot()
        recheck_result = check_account_plan(
            access_token,
            proxy=proxy,
            pool=pool,
            timezone_offset_min=timezone_offset_min,
            max_attempts=1,
        )
        if recheck_result.get("ok"):
            result = recheck_result
        else:
            logger.warning(
                "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                email,
                recheck_result.get("error") or "未知错误",
            )
    if result.get("ok"):
        logger.info(
            "[Plan] 后台查询成功: %s, group=%s, plan=%s, plus_trial=%s, trigger=%s",
            email, group_name,
            result.get("current_plan_type") or "unknown",
            bool(result.get("plus_trial_eligible")),
            trigger,
        )
    else:
        logger.warning(
            "[Plan] 后台查询失败: %s, group=%s, trigger=%s, error=%s",
            email, group_name, trigger, result.get("error") or "未知错误",
        )
    return result


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
    proxy_groups: list[str] | None = None,
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    if not db.claim_account_plan_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    groups = [str(g).strip() for g in (proxy_groups or []) if str(g or "").strip()]
    try:
        _EXECUTOR.submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
            groups=groups,
        )
        _append_log(
            email,
            f"[套餐] 已入队 trigger={trigger or 'manual'} groups={groups if groups else '默认'}",
            clear=True,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
        "proxy_groups": groups,
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
