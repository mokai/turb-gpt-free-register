# -*- coding: utf-8 -*-
"""换绑成功后原邮箱回池：来源区分与回池方式。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, email_provider


class EmailPoolRecycleTests(unittest.TestCase):
    @staticmethod
    def storage(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json", "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json", "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db", "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "agent", "_LEGACY_CODEX_EXPORT_STATE": root / "state.json",
            "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
        }

    def _register_and_rebind(self, source: str, old_email: str) -> int:
        """建一个 old_email 账号并走完换绑落库（换成 new@example.com / imap）。"""
        account_id = db.insert_account(
            email=old_email, access_token="old-token", email_source=source,
        )
        self.assertTrue(db.claim_account_email_change(account_id, "imap"))
        self.assertTrue(db.mark_account_email_change_running(account_id, "new@example.com"))
        self.assertTrue(db.finish_account_email_change(
            account_id, ok=True, new_email="new@example.com", source="imap",
            material_line="new@example.com----imap-pass",
        ))
        return account_id

    def test_outlook_old_email_is_reset_to_available(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_outlook_accounts([{
                "email": "old@example.com", "password": "pw", "client_id": "cid",
                "refresh_token": "rt",
            }])
            db.claim_next_outlook()
            self.assertEqual(db.get_outlook_by_email("old@example.com")["status"], "used")

            account_id = self._register_and_rebind("outlook", "old@example.com")
            result = email_provider.recycle_released_email(
                "old@example.com", "outlook", account_id=account_id,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "outlook")
            self.assertEqual(result["mode"], "reset")
            row = db.get_outlook_by_email("old@example.com")
            self.assertEqual(row["status"], "available")
            self.assertIsNone(row["used_at"])
            self.assertIn("换绑释放", row["note"])
            # 复位后可以再次被领取，且素材没有被清空。
            self.assertEqual(db.claim_next_outlook()["email"], "old@example.com")
            self.assertEqual(db.get_outlook_by_email("old@example.com")["password"], "pw")

    def test_old_source_is_used_instead_of_new_source(self):
        """finish 会把账号来源改写成新邮箱来源，回池必须用换绑前的来源。"""
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_outlook_accounts([{
                "email": "old@example.com", "password": "pw", "client_id": "cid",
                "refresh_token": "rt",
            }])
            db.claim_next_outlook()
            account_id = self._register_and_rebind("outlook", "old@example.com")
            self.assertEqual(db.get_account(account_id)["email_source"], "imap")
            self.assertEqual(db.get_account(account_id)["email_history"], ["old@example.com"])

            # 不显式传来源时按地址反查，同一地址当时在 Outlook 池里 → 仍落 Outlook。
            result = email_provider.recycle_released_email("old@example.com", "outlook")

            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "outlook")
            self.assertEqual(db.get_outlook_by_email("old@example.com")["status"], "available")

    def test_generic_api_and_imap_pool_rows_are_reset(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_generic_api_emails([{
                "email": "old@example.com", "code_url": "https://mail.example/code",
            }])
            db.claim_next_generic_api_email()
            account_id = self._register_and_rebind("generic_api", "old@example.com")

            result = email_provider.recycle_released_email("old@example.com", "generic_api")

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "reset")
            row = db.get_generic_api_email_by_email("old@example.com")
            self.assertEqual(row["status"], "available")
            self.assertIn("换绑释放", row["note"])
            self.assertIsNotNone(account_id)

    def test_domain_email_is_reset(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.claim_next_domain_email("old@example.com")
            db.release_domain_email("old@example.com", status="used")
            self._register_and_rebind("cloudflare_domain", "old@example.com")

            result = email_provider.recycle_released_email("old@example.com", "cloudflare_domain")

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "reset")
            self.assertEqual(db.list_domain_email_pool(limit=10)[0]["status"], "available")

    def test_temp_mail_sources_never_enter_local_pool(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            for source in ("remail", "gptmail", "cloudflare", "mailnest", "cloudmail"):
                result = email_provider.recycle_released_email("temp@example.com", source)
                self.assertFalse(result["ok"])
                self.assertEqual(result["mode"], "skipped")
                self.assertIn("临时邮箱", result["message"])
            self.assertEqual(db.list_email_pool_page(source="all", limit=10)["items"], [])

    def test_rebuild_outlook_row_from_account_credentials(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_outlook_accounts([{
                "email": "old@example.com", "password": "pw", "client_id": "cid",
                "refresh_token": "rt",
            }])
            db.claim_next_outlook()
            account_id = db.insert_account(
                email="old@example.com", access_token="old-token", email_source="outlook",
            )
            # 换绑前读取的账号记录里才有旧 Outlook 凭证；finish_account_email_change
            # 会在换绑成功后清空它们，所以调用方必须先取好账号记录。
            before = db.get_account(account_id)
            self.assertEqual(before["refresh_token"], "rt")
            self.assertTrue(db.claim_account_email_change(account_id, "imap"))
            self.assertTrue(db.mark_account_email_change_running(account_id, "new@example.com"))
            self.assertTrue(db.finish_account_email_change(
                account_id, ok=True, new_email="new@example.com", source="imap",
                material_line="new@example.com----imap-pass",
            ))
            self.assertEqual(db.get_account(account_id)["refresh_token"], "")
            # 池记录被用户删过：只能靠换绑前保存的素材重建。
            db.delete_email_pool("old@example.com", source="outlook")
            self.assertIsNone(db.get_outlook_by_email("old@example.com"))

            result = email_provider.recycle_released_email(
                "old@example.com", "outlook", account_id=account_id,
                credentials={
                    "password": before["password"], "client_id": before["client_id"],
                    "refresh_token": before["refresh_token"],
                },
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "insert")
            row = db.get_outlook_by_email("old@example.com")
            self.assertEqual(row["status"], "available")
            self.assertEqual(row["refresh_token"], "rt")
            self.assertIn("账号 #", row["note"])

    def test_rebuild_skipped_when_source_config_only_lives_in_pool(self):
        """通用 API / IMAP 的连接配置只存在池记录里，删掉就无法重建。"""
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_imap_emails([{
                "email": "old@example.com", "imap_password": "pw", "imap_server": "imap.example.com",
            }])
            db.claim_next_imap_email()
            self._register_and_rebind("imap", "old@example.com")
            db.delete_email_pool("old@example.com", source="imap")

            result = email_provider.recycle_released_email("old@example.com", "imap")

            self.assertFalse(result["ok"])
            self.assertEqual(result["mode"], "skipped")
            self.assertIn("无法重建", result["message"])

    def test_rebuild_outlook_without_credentials_is_skipped(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            result = email_provider.recycle_released_email("ghost@example.com", "outlook")
            self.assertFalse(result["ok"])
            self.assertEqual(result["mode"], "skipped")
            self.assertIn("无法重建", result["message"])

    def test_email_still_used_as_main_email_is_not_recycled(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db.import_outlook_accounts([{
                "email": "busy@example.com", "password": "pw", "client_id": "cid",
                "refresh_token": "rt",
            }])
            db.claim_next_outlook()
            db.insert_account(email="busy@example.com", access_token="t", email_source="outlook")

            result = email_provider.recycle_released_email("busy@example.com", "outlook")

            self.assertFalse(result["ok"])
            self.assertIn("在用主邮箱", result["message"])

    def test_empty_email_is_skipped(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            result = email_provider.recycle_released_email("", "outlook")
            self.assertFalse(result["ok"])
            self.assertEqual(result["mode"], "skipped")


if __name__ == "__main__":
    unittest.main()
