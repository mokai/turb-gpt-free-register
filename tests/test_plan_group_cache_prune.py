# -*- coding: utf-8 -*-
"""plan_check_groups 只反映「最近一轮」查询：旧分组缓存必须被收敛掉。

背景：换了代理分组去查套餐时，旧分组的记录会一直留在 plan_check_groups 里，
列表套餐列把所有分组都渲染成标签，看起来就像"查询命中了缓存"（实测残留在
已删除的 JP 分组上）。这里覆盖收敛规则本身，不碰真实库。
"""
import unittest
from unittest.mock import patch

import core.db as db
from core.db import prune_stale_plan_groups

_KNOWN = {"IN", "VN"}


class _PatchGroups(unittest.TestCase):
    """固定住"代理池里有哪些分组"，避免依赖本机 .env。"""

    def setUp(self):
        patcher = patch("core.db._configured_plan_group_names", return_value=set(_KNOWN))
        patcher.start()
        self.addCleanup(patcher.stop)


class PruneStalePlanGroupsTests(_PatchGroups):
    def test_keeps_same_run_groups(self):
        groups = {
            "VN": {"checked_at": "2026-09-23T21:09:38"},
            "IN": {"checked_at": "2026-09-23T21:10:17"},
        }
        self.assertEqual(sorted(prune_stale_plan_groups(groups)), ["IN", "VN"])

    def test_drops_previous_run(self):
        groups = {
            "VN": {"checked_at": "2026-09-23T21:10:30", "ok": True},
            "IN": {"checked_at": "2026-09-23T21:11:10", "ok": True},
            "JP": {"checked_at": "2026-09-22T15:19:20", "ok": True},
        }
        kept = prune_stale_plan_groups(groups)
        self.assertEqual(sorted(kept), ["IN", "VN"])
        self.assertNotIn("JP", kept)

    def test_drops_group_removed_from_config(self):
        """分组已从代理池删掉时，即使它属于最近一轮也不再展示。"""
        groups = {
            "VN": {"checked_at": "2026-09-22T10:46:20"},
            "IN": {"checked_at": "2026-09-22T10:46:35"},
            "JP": {"checked_at": "2026-09-22T10:47:10"},
        }
        self.assertEqual(sorted(prune_stale_plan_groups(groups)), ["IN", "VN"])

    def test_keeps_records_without_timestamp(self):
        groups = {
            "VN": {"checked_at": "2026-09-23T21:10:30"},
            "IN": {},  # 老数据没有 checked_at，保守保留
        }
        self.assertEqual(sorted(prune_stale_plan_groups(groups)), ["IN", "VN"])

    def test_single_group_untouched(self):
        groups = {"JP": {"checked_at": "2020-01-01T00:00:00"}}
        self.assertEqual(list(prune_stale_plan_groups(groups)), ["JP"])

    def test_never_returns_empty(self):
        """分组名全都不在当前配置里（例如配置读失败）时不能把标签清空。"""
        groups = {
            "JP": {"checked_at": "2026-09-22T10:47:10"},
            "KN": {"checked_at": "2026-09-22T10:47:30"},
        }
        self.assertEqual(sorted(prune_stale_plan_groups(groups)), ["JP", "KN"])

    def test_bad_input(self):
        self.assertEqual(prune_stale_plan_groups(None), {})
        self.assertEqual(prune_stale_plan_groups([]), {})

    def test_unknown_config_keeps_window_result(self):
        with patch("core.db._configured_plan_group_names", return_value=set()):
            groups = {
                "VN": {"checked_at": "2026-09-23T21:10:30"},
                "JP": {"checked_at": "2026-09-22T15:19:20"},
            }
            self.assertEqual(list(prune_stale_plan_groups(groups)), ["VN"])


class ScopeAccountPlanGroupsTests(unittest.TestCase):
    """每轮查询开始时把分组缓存收敛到本轮名单。"""

    def _rows(self):
        return [{
            "id": 1,
            "plan_check_groups": {
                "VN": {"checked_at": "2026-09-22T10:46:20"},
                "IN": {"checked_at": "2026-09-22T10:46:35"},
                "JP": {"checked_at": "2026-09-22T10:47:10"},
            },
        }]

    def test_drops_groups_outside_run(self):
        rows = self._rows()
        with patch.object(db, "_load_accounts", return_value=rows), \
                patch.object(db, "_save_accounts") as save:
            changed = db.scope_account_plan_groups(1, ["VN", "IN"])
        self.assertTrue(changed)
        self.assertEqual(sorted(rows[0]["plan_check_groups"]), ["IN", "VN"])
        save.assert_called_once()

    def test_keeps_previous_value_of_in_scope_group(self):
        """本轮要查的分组先保留上一轮的值，查询进行中仍能看到旧结果。"""
        rows = self._rows()
        with patch.object(db, "_load_accounts", return_value=rows), \
                patch.object(db, "_save_accounts"):
            db.scope_account_plan_groups(1, ["VN", "IN"])
        self.assertEqual(rows[0]["plan_check_groups"]["VN"]["checked_at"], "2026-09-22T10:46:20")

    def test_no_change_skips_write(self):
        rows = [{"id": 1, "plan_check_groups": {"VN": {}, "IN": {}}}]
        with patch.object(db, "_load_accounts", return_value=rows), \
                patch.object(db, "_save_accounts") as save:
            changed = db.scope_account_plan_groups(1, ["IN", "VN"])
        self.assertFalse(changed)
        save.assert_not_called()

    def test_empty_names_is_noop(self):
        rows = self._rows()
        with patch.object(db, "_load_accounts", return_value=rows), \
                patch.object(db, "_save_accounts") as save:
            changed = db.scope_account_plan_groups(1, [])
        self.assertFalse(changed)
        save.assert_not_called()

    def test_missing_account(self):
        with patch.object(db, "_load_accounts", return_value=[{"id": 2}]), \
                patch.object(db, "_save_accounts") as save:
            changed = db.scope_account_plan_groups(1, ["VN"])
        self.assertFalse(changed)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
