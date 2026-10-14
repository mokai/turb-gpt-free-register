# -*- coding: utf-8 -*-
"""账号列表 special 快捷筛选（unchanged=未改绑且 Free / abnormal=操作异常）的 SQL 下推逻辑。"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import db


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class AccountSpecialFilterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        now = datetime.now()
        long_ago_running = _iso(now - timedelta(seconds=db._PLAN_CHECK_STALE_SECONDS + 60))
        fresh_running = _iso(now - timedelta(seconds=10))
        long_ago_queued = _iso(now - timedelta(seconds=db._PLAN_CHECK_QUEUE_STALE_SECONDS + 60))
        rows = [
            # 1：从未换绑、套餐 free → 未改绑
            {"id": 1, "email": "never@example.com", "current_plan_type": "free"},
            # 2：换绑失败、套餐 free → 未改绑 + 操作异常
            {"id": 2, "email": "change-failed@example.com", "current_plan_type": "free",
             "email_change_status": "failed"},
            # 3：换绑成功过（有 original_email + history）→ 已改绑
            {"id": 3, "email": "changed@example.com", "current_plan_type": "free",
             "original_email": "old@example.com", "email_change_status": "success",
             "email_history": ["old@example.com"]},
            # 4：换绑成功过但最近一次失败 → 已改绑 + 操作异常
            {"id": 4, "email": "changed-then-failed@example.com", "current_plan_type": "free",
             "original_email": "old@example.com", "email_change_status": "failed",
             "email_history": ["old@example.com"]},
            # 5：套餐不是 free（plus）→ 不算未改绑
            {"id": 5, "email": "plus@example.com", "current_plan_type": "plus"},
            # 6：查活失败 → 操作异常
            {"id": 6, "email": "live-failed@example.com", "current_plan_type": "free",
             "live_check_status": "failed"},
            # 7：套餐查询失败 → 操作异常
            {"id": 7, "email": "plan-failed@example.com", "current_plan_type": "free",
             "plan_check_status": "failed"},
            # 8：套餐查询 running 且已超时 → 列表口径视为查询失败 → 操作异常
            {"id": 8, "email": "plan-stale-running@example.com", "current_plan_type": "free",
             "plan_check_status": "running", "plan_check_started_at": long_ago_running},
            # 9：套餐查询 queued 且已超时 → 操作异常
            {"id": 9, "email": "plan-stale-queued@example.com", "current_plan_type": "free",
             "plan_check_status": "queued", "plan_check_queued_at": long_ago_queued},
            # 10：套餐查询刚发起（未超时）→ 不算操作异常
            {"id": 10, "email": "plan-running@example.com", "current_plan_type": "free",
             "plan_check_status": "running", "plan_check_started_at": fresh_running},
            # 11：邮箱历史为空数组、状态缺失 → 仍属未改绑
            {"id": 11, "email": "empty-history@example.com", "current_plan_type": "free",
             "email_history": []},
        ]
        (root / "accounts.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        missing = root / "missing.json"
        patcher = patch.multiple(
            db,
            _ACCOUNTS_JSON=root / "accounts.json",
            _LEGACY_ACCOUNTS_JSON=missing,
            _OUTLOOK_JSON=missing,
            _GENERIC_API_EMAIL_JSON=missing,
            _JOBS_JSON=missing,
            _DOMAIN_EMAIL_JSON=missing,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = patch.multiple(db, _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        ready.start()
        self.addCleanup(ready.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unchanged_filter(self):
        result = db.list_accounts_page(limit=50, special_filter="unchanged")
        # 未改绑 = 从未成功换绑（无 original_email / email_history 非空）且套餐为 free。
        # 命中：1、2、6~10（6~10 只是有别的异常，但同样从未换绑成功）；排除 3、4、5。
        self.assertEqual([item["id"] for item in result["items"]], [11, 10, 9, 8, 7, 6, 2, 1])
        snapshot = db.list_account_plan_check_statuses(limit=50, special_filter="unchanged")
        self.assertEqual([item["id"] for item in snapshot["items"]], [11, 10, 9, 8, 7, 6, 2, 1])

    def test_abnormal_filter(self):
        result = db.list_accounts_page(limit=50, special_filter="abnormal")
        # 操作异常 = 换绑失败 / 查活失败 / 查套餐失败（含超时未结束）任一命中。
        self.assertEqual([item["id"] for item in result["items"]], [9, 8, 7, 6, 4, 2])
        snapshot = db.list_account_plan_check_statuses(limit=50, special_filter="abnormal")
        self.assertEqual([item["id"] for item in snapshot["items"]], [9, 8, 7, 6, 4, 2])

    def test_special_and_plan_filter_can_stack(self):
        # 后端不做互斥（互斥由前端负责）：两个条件同时给时应按 AND 叠加。
        result = db.list_accounts_page(limit=50, special_filter="abnormal", plan_filter="free")
        self.assertEqual([item["id"] for item in result["items"]], [9, 8, 7, 6, 4, 2])

    def test_special_all_is_noop(self):
        self.assertEqual(
            db.list_accounts_page(limit=50, special_filter="all")["total"],
            db.list_accounts_page(limit=50)["total"],
        )
        self.assertEqual(
            db.list_accounts_page(limit=50, special_filter="")["total"],
            db.list_accounts_page(limit=50)["total"],
        )


if __name__ == "__main__":
    unittest.main()
