# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class RemailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        # /api/jobs 提交前会预检「本地指纹浏览器客户端」（REGISTRATION_DRIVER=bit/roxy）：
        # 单测环境没有本地客户端，这里统一放行，避免掩盖这些用例真正要验证的邮箱源校验。
        patcher = patch("core.browser_client_ready.check_registration_driver", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_remail_without_project_id(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 0, create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Remail 项目 ID", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_remail_config_does_not_check_local_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "remail"
        ), patch.object(email_config, "REMAIL_API_BASE", "https://remail.aishop6.com", create=True), patch.object(
            email_config, "REMAIL_API_KEY", "rk-test-key", create=True
        ), patch.object(email_config, "REMAIL_PROJECT_ID", 1001, create=True), patch.object(
            email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1, proxy_group="")


if __name__ == "__main__":
    unittest.main()
