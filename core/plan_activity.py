# -*- coding: utf-8 -*-
"""ChatGPT Plus 活动（eligible_promo_campaigns.plus）识别与展示标签。

同一个 free 账号用不同地区出口查套餐，服务端给的活动可能不一样：

    plus-1-month-free         1 个月免费
    plus-1-month-50-pct-off   1 个月半价
    plus-2-month-50-pct-off   2 个月半价
    plus-3-month-50-pct-off   3 个月半价

账号列表的「套餐」列要按代理分组区分这些活动，所以这里集中维护
「活动 ID → 短标签 / 完整说明」的映射：短标签只做区分（``JP-1月免费``、
``PH-2月半价``），完整说明放在悬浮提示里。

短标签刻意不写成 ``free``：列表里「所有分组都没查到活动」的兜底展示就是一个
光秃秃的 ``free``，如果活动标签也叫 free，筛选下拉里 ``JP-free`` 会同时表示
「有1个月免费活动」和「该分组无活动」两种意思。

未知活动 ID 不会丢失：优先按接口返回的时长与折扣百分比推导标签，
推导不出时退化成 ID 去掉 ``plus-`` 前缀的形态。
"""
from __future__ import annotations

# 已知活动 ID → (短标签, 完整说明)
_KNOWN_ACTIVITIES: dict[str, tuple[str, str]] = {
    "plus-1-month-free": ("1月免费", "Plus 1个月免费"),
    "plus-1-month-50-pct-off": ("1月半价", "Plus 1个月半价"),
    "plus-2-month-50-pct-off": ("2月半价", "Plus 2个月半价"),
    "plus-3-month-50-pct-off": ("3月半价", "Plus 3个月半价"),
}

_PERIOD_LABELS = {
    "day": "天",
    "week": "周",
    "month": "月",
    "year": "年",
}


def _period_label(period: str | None) -> str:
    value = str(period or "").strip().lower()
    if not value:
        return ""
    return _PERIOD_LABELS.get(value, value)


def _to_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def describe_activity(
    campaign_id: str | None,
    *,
    discount_percentage=None,
    num_periods=None,
    period: str | None = None,
) -> dict | None:
    """把活动 ID 和接口返回的时长/折扣归一成展示用结构。

    返回 ``{"id", "short", "title"}``；没有活动 ID 时返回 ``None``。
    """
    cid = str(campaign_id or "").strip()
    if not cid:
        return None

    known = _KNOWN_ACTIVITIES.get(cid)
    if known:
        return {"id": cid, "short": known[0], "title": known[1]}

    percentage = _to_int(discount_percentage)
    periods = _to_int(num_periods)
    unit = _period_label(period)
    duration_text = f"{periods}{unit}" if (periods and unit) else (unit or "")

    if percentage is not None and duration_text:
        if percentage >= 100:
            short = f"{duration_text}免费"
            title = f"Plus {duration_text}免费"
        else:
            off = max(0, 100 - percentage)
            short = f"{duration_text}{off}%off"
            title = f"Plus {duration_text}{off}%折扣"
        return {"id": cid, "short": short, "title": title}

    # 信息不足：退化成去掉 plus- 前缀的 ID，保证不同活动仍能区分。
    fallback = cid[5:] if cid.lower().startswith("plus-") else cid
    return {"id": cid, "short": fallback, "title": f"Plus 活动 {cid}"}


def activity_of_result(result: dict | None) -> dict | None:
    """从一次套餐查询结果里取出活动信息。

    只认 Plus 试用活动（eligible_promo_campaigns.plus）：付费账号 entitlement
    里的 ``discount_promo_campaign_id`` 是已生效的折扣，不是「可领的活动」，
    混进来会让列表标签误导。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    activity = describe_activity(
        result.get("plus_trial_campaign_id"),
        discount_percentage=result.get("plus_trial_discount_percentage"),
        num_periods=result.get("plus_trial_duration_num_periods"),
        period=result.get("plus_trial_duration_period"),
    )
    if activity:
        return activity
    # 兼容只透传了 activity 字段的旧记录。
    short = str(result.get("plan_activity_short") or "").strip()
    if short:
        return {
            "id": str(result.get("plan_activity_id") or "").strip(),
            "short": short,
            "title": str(result.get("plan_activity_title") or "").strip() or short,
        }
    return None


def activity_short(record: dict | None) -> str:
    """从「按分组保存的记录」里取短标签。"""
    if not isinstance(record, dict):
        return ""
    return str(record.get("activity_short") or "").strip()


def activity_id(record: dict | None) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("activity_id") or "").strip()
