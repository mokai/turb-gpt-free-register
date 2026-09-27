# -*- coding: utf-8 -*-
"""Remail 注册失败邮箱回收（写回通用 API 邮箱池）测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import db, email_provider, remail_client


def _storage_patches(root: Path) -> dict:
    return {
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_DOMAIN_EMAIL_JSON": root / "domain.json",
        "_JOBS_JSON": root / "jobs.json",
        "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
        "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
        "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
        "_LEGACY_SQLITE": root / "legacy.db",
        "_CODEX_DIR": root / "codex_accounts",
        "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
        "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
        "_SQLITE_READY": False,
        "_SQLITE_READY_PATH": None,
    }


class RemailRecycleTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()

    def tearDown(self):
        remail_client._CONTEXT_CACHE.clear()

    def _cache_context(self, email="fresh@outlook.test", token="st-test-token", order="R1"):
        account = remail_client.RemailAccount(
            email=email,
            service_token=token,
            order_no=order,
            project_id=2,
            email_suffix="outlook.com",
        )
        return remail_client._cache_context(account)

    # ---------- pickup_url ----------
    def test_pickup_url_uses_cached_context_and_normalizes_docs_base(self):
        self._cache_context()
        with patch.object(email_config, "REMAIL_API_BASE", "https://remail.aishop6.com/docs", create=True):
            self.assertEqual(
                remail_client.pickup_url("fresh@outlook.test"),
                "https://remail.aishop6.com/pickup?email=fresh%40outlook.test&token=st-test-token",
            )

    def test_pickup_url_empty_without_token_or_context(self):
        self.assertEqual(remail_client.pickup_url("fresh@outlook.test"), "")

    # ---------- db.upsert_generic_api_email ----------
    def test_upsert_inserts_then_updates_same_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)):
                self.assertEqual(
                    db.upsert_generic_api_email("a@x.test", "https://r.test/pickup?email=a@x.test&token=st-1"),
                    "inserted",
                )
                self.assertEqual(
                    db.upsert_generic_api_email(
                        "A@X.test", "https://r.test/pickup?email=a@x.test&token=st-2",
                        status="available", note="回收",
                    ),
                    "updated",
                )
                rows = db.list_generic_api_email_pool()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["email"], "a@x.test")
                self.assertEqual(row["code_url"], "https://r.test/pickup?email=a@x.test&token=st-2")
                self.assertEqual(row["status"], "available")
                self.assertEqual(row["note"], "回收")
                self.assertIsNone(row["used_at"])

    # ---------- recycle_failed_email / release_account ----------
    def test_release_account_available_recycles_email_into_pool(self):
        account = self._cache_context()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                email_config, "REMAIL_API_BASE", "https://remail.aishop6.com", create=True
            ), patch.object(email_config, "REMAIL_RECYCLE_FAILED_EMAIL", True, create=True):
                remail_client.release_account("fresh@outlook.test", status="available", note="上次失败")

                rows = db.list_generic_api_email_pool()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["email"], "fresh@outlook.test")
                self.assertEqual(row["status"], "available")
                self.assertIn("token=st-test-token", row["code_url"])
                self.assertIn("order=R1", row["note"])
                # 取件上下文已释放
                self.assertIsNone(remail_client.get_account_context("fresh@outlook.test"))
                # 回收后的行可被通用 API 邮箱领取流程直接选中
                claimed = db.claim_next_generic_api_email()
                self.assertEqual(claimed["email"], "fresh@outlook.test")
        self.assertIsNotNone(account)

    def test_release_account_failed_status_does_not_recycle(self):
        self._cache_context()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)):
                remail_client.release_account("fresh@outlook.test", status="failed", note="账号已废弃")
                self.assertEqual(db.list_generic_api_email_pool(), [])

    def test_recycle_skips_registered_email(self):
        self._cache_context()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                db, "get_account_by_email", return_value={"id": 1, "email": "fresh@outlook.test"}
            ):
                self.assertFalse(remail_client.recycle_failed_email("fresh@outlook.test"))
                self.assertEqual(db.list_generic_api_email_pool(), [])

    def test_recycle_disabled_by_config(self):
        self._cache_context()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                email_config, "REMAIL_RECYCLE_FAILED_EMAIL", False, create=True
            ):
                self.assertFalse(remail_client.recycle_failed_email("fresh@outlook.test"))
                self.assertEqual(db.list_generic_api_email_pool(), [])

    def test_recycle_without_context_credentials_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                email_config, "REMAIL_RECYCLE_FAILED_EMAIL", True, create=True
            ):
                self.assertFalse(remail_client.recycle_failed_email("no-context@outlook.test"))
                self.assertEqual(db.list_generic_api_email_pool(), [])

    # ---------- email_provider 端到端 ----------
    def test_release_email_if_unconsumed_recycles_remail_email(self):
        self._cache_context(email="pool@outlook.test", token="st-pool-token", order="R2")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                email_config, "REMAIL_API_BASE", "https://remail.aishop6.com", create=True
            ), patch.object(email_config, "REMAIL_RECYCLE_FAILED_EMAIL", True, create=True):
                self.assertTrue(
                    email_provider.release_email_if_unconsumed("pool@outlook.test", note="任务未消耗")
                )
                rows = db.list_generic_api_email_pool()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["email"], "pool@outlook.test")
                self.assertEqual(row["status"], "available")
                self.assertIn("token=st-pool-token", row["code_url"])
                self.assertIn("任务未消耗", row["note"])

    def test_release_email_if_unconsumed_keeps_registered_remail_email(self):
        self._cache_context(email="used@outlook.test")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **_storage_patches(root)), patch.object(
                db, "get_account_by_email", return_value={"id": 9, "email": "used@outlook.test"}
            ):
                self.assertFalse(email_provider.release_email_if_unconsumed("used@outlook.test"))
                self.assertEqual(db.list_generic_api_email_pool(), [])

    # ---------- 通用 API 客户端识别自定义 Remail 主机 ----------
    def test_parse_remail_pickup_url_accepts_configured_custom_host(self):
        from core import generic_api_mail_client as client

        with patch.object(
            email_config, "REMAIL_API_BASE", "https://mail.example-selfhosted.com", create=True
        ):
            parsed = client._parse_remail_pickup_url(
                "https://mail.example-selfhosted.com/pickup?email=a@x.test&token=st-1"
            )
        self.assertEqual(parsed, ("a@x.test", "st-1"))

    def test_parse_remail_pickup_url_rejects_unknown_host(self):
        from core import generic_api_mail_client as client

        with patch.object(email_config, "REMAIL_API_BASE", "https://remail.aishop6.com", create=True):
            self.assertIsNone(
                client._parse_remail_pickup_url("https://other.test/pickup?email=a@x.test&token=st-1")
            )


if __name__ == "__main__":
    unittest.main()
