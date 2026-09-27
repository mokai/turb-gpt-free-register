# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core import db


class EmailPoolSourceTests(unittest.TestCase):
    def _storage_patches(self, root: Path) -> dict:
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

    def test_generic_api_import_keeps_source_for_all_and_filtered_lists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                    ]),
                    (1, 0),
                )

                all_items = db.list_email_pool_page(source="all", limit=10)["items"]
                generic_items = db.list_email_pool_page(source="generic_api", limit=10)["items"]
                self.assertEqual(all_items[0]["source"], "generic_api")
                self.assertEqual(generic_items[0]["source"], "generic_api")

    def test_repairs_source_for_legacy_generic_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db._ensure_sqlite()
                payload = {
                    "id": 1,
                    "email": "legacy-generic@example.com",
                    "code_url": "https://mail.example/legacy-code",
                    "status": "available",
                }
                with closing(db._sqlite_conn()) as conn:
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (1, payload["email"], "", "available", 0, "", "", json.dumps(payload)),
                    )
                    conn.commit()

                db._SQLITE_READY = False
                db._SQLITE_READY_PATH = None
                items = db.list_email_pool_page(source="generic_api", limit=10)["items"]
                self.assertEqual(items[0]["source"], "generic_api")

    def test_repairs_source_for_legacy_domain_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db._ensure_sqlite()
                payload = {
                    "id": 1,
                    "email": "legacy-domain@example.com",
                    "status": "available",
                    "used_at": None,
                }
                with closing(db._sqlite_conn()) as conn:
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (1, payload["email"], "", "available", 0, "", "", json.dumps(payload)),
                    )
                    conn.commit()

                db._SQLITE_READY = False
                db._SQLITE_READY_PATH = None
                items = db.list_email_pool_page(source="cloudflare_domain", limit=10)["items"]
                self.assertEqual(items[0]["source"], "cloudflare_domain")

    def test_delete_pool_supports_all_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                ])
                db.import_outlook_accounts([
                    {
                        "email": "outlook@example.com",
                        "password": "password",
                        "client_id": "client",
                        "refresh_token": "refresh",
                    },
                ])
                db.claim_next_domain_email("domain@example.com")

                self.assertTrue(db.delete_email_pool("generic@example.com", source="all"))
                self.assertTrue(db.delete_email_pool("outlook@example.com", source="outlook"))
                self.assertTrue(db.delete_email_pool("domain@example.com", source="cloudflare_domain"))
                self.assertFalse(db.list_email_pool_page(source="all", limit=10)["items"])

    def _rebound_account(self, old_email: str, new_email: str) -> int:
        """建一个 old_email 账号并换绑到 new_email（新邮箱进域名池）。"""
        account_id = db.insert_account(
            email=old_email, access_token="token-old@example.com", email_source="generic_api",
        )
        self.assertTrue(db.claim_account_email_change(account_id, "cloudflare_domain"))
        self.assertTrue(db.mark_account_email_change_running(account_id, new_email))
        self.assertTrue(db.finish_account_email_change(
            account_id, ok=True, new_email=new_email, source="cloudflare_domain",
            material_line=new_email,
        ))
        db.claim_next_domain_email(new_email)
        return account_id

    def test_search_does_not_leak_rebound_account_old_email_into_current_row(self):
        """搜旧邮箱不能把「同一账号换绑后的当前邮箱」那条池记录带出来。"""
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            db.import_generic_api_emails([
                {"email": "old@example.com", "code_url": "https://mail.example/code"},
            ])
            self._rebound_account("old@example.com", "new@gkkdev.com")

            emails = [
                item["email"]
                for item in db.list_email_pool_page(source="all", q="old@example.com", limit=10)["items"]
            ]
            # old@example.com 自己仍在池里 → 命中它；但换绑后的 new@gkkdev.com 不该被搜出来。
            self.assertEqual(emails, ["old@example.com"])

    def test_search_returns_nothing_when_email_is_only_in_rebind_history(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            db.import_outlook_accounts([{
                "email": "gone@example.com", "password": "pw", "client_id": "cid",
                "refresh_token": "rt",
            }])
            db.claim_next_outlook()
            account_id = db.insert_account(
                email="gone@example.com", access_token="token-gone@example.com", email_source="outlook",
            )
            self.assertTrue(db.claim_account_email_change(account_id, "cloudflare_domain"))
            self.assertTrue(db.mark_account_email_change_running(account_id, "kept@gkkdev.com"))
            self.assertTrue(db.finish_account_email_change(
                account_id, ok=True, new_email="kept@gkkdev.com", source="cloudflare_domain",
                material_line="kept@gkkdev.com",
            ))
            db.claim_next_domain_email("kept@gkkdev.com")
            # 池记录被删（旧邮箱只剩在账号的换绑历史里）。
            db.delete_email_pool("gone@example.com", source="outlook")

            result = db.list_email_pool_page(source="all", q="gone@example.com", limit=10)
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["items"], [])

    def test_search_still_matches_account_token_and_email_directly(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage_patches(Path(td))):
            db.import_generic_api_emails([
                {"email": "kept@example.com", "code_url": "https://mail.example/code"},
                {"email": "moved@example.com", "code_url": "https://mail.example/code"},
            ])
            # 未换绑的账号：token 仍在 payload 顶层 → 仍能按 token 搜到池记录。
            db.insert_account(
                email="kept@example.com", access_token="tok-sentinel-9001", email_source="generic_api",
            )
            by_token = db.list_email_pool_page(source="all", q="tok-sentinel-9001", limit=10)
            self.assertEqual([i["email"] for i in by_token["items"]], ["kept@example.com"])

            # 换绑后：新旧两个地址都能按地址各搜到自己的池记录。
            self._rebound_account("moved@example.com", "new@gkkdev.com")
            by_old = db.list_email_pool_page(source="all", q="moved@example.com", limit=10)
            self.assertEqual([i["email"] for i in by_old["items"]], ["moved@example.com"])
            by_new = db.list_email_pool_page(source="all", q="new@gkkdev.com", limit=10)
            self.assertEqual([i["email"] for i in by_new["items"]], ["new@gkkdev.com"])


if __name__ == "__main__":
    unittest.main()
