# -*- coding: utf-8 -*-
"""``core.icloud_hme_client`` 的契约测试（纯离线，网络层全 mock）。

覆盖点：
1. API 根地址容忍误填（尾部 /、``/api``、``/api/v1``、``/docs``、缺 scheme）；
2. 取邮箱＝预留：``available/next`` 的 ``item`` 为 null 时必须给出可操作报错，
   而不是抛 KeyError / 返回半成品上下文；
3. 取码优先读服务端解析好的 ``verification_code``，缺失才回退本地 ``extract_code``；
4. 回收只在 ``status == "available"`` 时通知平台，且平台 200 + ``ok:false`` 也算失败；
5. 错误映射：401 → API Key 失效，400 → 项目 code 不存在。
"""
import unittest
from unittest.mock import Mock, patch

from core import icloud_hme_client as client


def _ok(payload):
    response = Mock(status_code=200, text="")
    response.json.return_value = payload
    return response


def _err(status, payload=None, text=""):
    response = Mock(status_code=status, text=text)
    if payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    return response


class IcloudHmeClientTests(unittest.TestCase):
    def setUp(self):
        client._CONTEXT_CACHE.clear()

    # ---------- 配置与地址 ----------

    def test_base_url_tolerates_pasted_paths(self):
        cases = {
            "http://127.0.0.1:5050": "http://127.0.0.1:5050",
            "http://127.0.0.1:5050/": "http://127.0.0.1:5050",
            "http://127.0.0.1:5050/api": "http://127.0.0.1:5050",
            "http://127.0.0.1:5050/api/v1": "http://127.0.0.1:5050",
            "http://127.0.0.1:5050/api/v1/": "http://127.0.0.1:5050",
            "http://127.0.0.1:5050/docs": "http://127.0.0.1:5050",
            "127.0.0.1:5050": "http://127.0.0.1:5050",
            "https://hme.example.com/api/v1": "https://hme.example.com",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(client._base_url(raw), expected)

    def test_base_url_rejects_empty_or_invalid(self):
        with patch.object(client._email_cfg, "ICLOUD_HME_API_BASE", "", create=True):
            self.assertEqual(client._base_url(), client.DEFAULT_API_BASE)

    def test_missing_api_key_raises_actionable_error(self):
        with patch.object(client._email_cfg, "ICLOUD_HME_API_KEY", "", create=True):
            with self.assertRaisesRegex(client.IcloudHmeError, "ICLOUD_HME_API_KEY"):
                client.list_projects()

    def test_missing_project_code_raises_actionable_error(self):
        with patch.object(client._email_cfg, "ICLOUD_HME_API_KEY", "hme_x", create=True), patch.object(
            client._email_cfg, "ICLOUD_HME_PROJECT_CODE", "", create=True
        ):
            with self.assertRaisesRegex(client.IcloudHmeError, "ICLOUD_HME_PROJECT_CODE"):
                client.pick_account()

    # ---------- 取邮箱（预留） ----------

    def test_pick_account_reserves_and_caches_context(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok(
                {
                    "ok": True,
                    "project_code": "chatgpt",
                    "item": {
                        "alias_email": "Fiercer.Radian_5g@icloud.com",
                        "account_id": "acc_e338d8a1",
                        "account_name": "16673153781",
                        "label": "16673153781 10030917",
                    },
                }
            )
            account = client.pick_account()

        self.assertEqual(account.email, "Fiercer.Radian_5g@icloud.com")
        self.assertEqual(account.project_code, "chatgpt")
        self.assertEqual(account.account_id, "acc_e338d8a1")

        method, url = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://127.0.0.1:5050/api/v1/hme/available/next")
        self.assertEqual(kwargs["params"], {"project_code": "chatgpt"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer hme_test_key")
        # 上下文按小写邮箱缓存，取码/回收才能按原样传回大写地址
        self.assertIs(client.get_account_context("fiercer.radian_5g@icloud.com"), account)

    def test_pick_account_raises_when_pool_is_empty(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": True, "project_code": "chatgpt", "item": None})
            with self.assertRaisesRegex(client.IcloudHmeError, "没有可用邮箱"):
                client.pick_account()

    def test_pick_account_rejects_malformed_email(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": True, "item": {"alias_email": "not-an-email"}})
            with self.assertRaisesRegex(client.IcloudHmeError, "邮箱地址无效"):
                client.pick_account()

    def test_get_email_compat_entry_returns_address(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": True, "item": {"alias_email": "a@icloud.com"}})
            self.assertEqual(client.get_email(), "a@icloud.com")

    # ---------- 取验证码 ----------

    def test_payload_code_prefers_server_side_code(self):
        payload = {
            "ok": True,
            "messages": [
                {
                    "subject": "कोड",
                    "body": "जारी रखने के लिए ये कोड डालें:\n255224",
                    "date": "2026-10-03T04:00:00Z",
                    "verification_code": "255224",
                }
            ],
            "verification_code": "255224",
        }
        code, timestamp = client._payload_code(payload, None)
        self.assertEqual(code, "255224")
        self.assertIsNotNone(timestamp)

    def test_payload_code_falls_back_to_local_extract_on_hindi_body(self):
        """服务端没给出 verification_code 时，本地兜底解析要能吃印地语正文。"""
        payload = {
            "ok": True,
            "messages": [
                {
                    "subject": "आपका अस्थायी ChatGPT वेरिफिकेशन कोड",
                    "body": "जारी रखने के लिए ये टेम्पररी वेरिफ़िकेशन कोड डालें:\n255224\nसादर,",
                    "date": "2026-10-03T04:00:00Z",
                }
            ],
        }
        code, _ = client._payload_code(payload, None)
        self.assertEqual(code, "255224")

    def test_payload_code_skips_messages_older_than_after_ts(self):
        payload = {
            "messages": [
                {"body": "old code 111111", "date": "2026-10-01T00:00:00Z", "verification_code": "111111"},
                {"body": "new code 222222", "date": "2026-10-03T04:00:00Z", "verification_code": "222222"},
            ]
        }
        after = client._parse_timestamp("2026-10-02T00:00:00Z")
        code, _ = client._payload_code(payload, after)
        self.assertEqual(code, "222222")

    def test_payload_code_reads_root_field_when_no_messages(self):
        code, _ = client._payload_code({"messages": [], "verification_code": "998877"}, None)
        self.assertEqual(code, "998877")

    def test_payload_code_ignores_empty_root_field(self):
        code, _ = client._payload_code({"messages": [], "verification_code": "", "code": ""}, None)
        self.assertIsNone(code)

    def test_payload_code_never_falls_back_to_root_field_when_messages_exist(self):
        """根级字段是 messages[0] 的投影，不能绕过 after_ts。

        2026-10-05 账号 #58 换绑实测：新验证码邮件还没到，邮箱里最新一封是 20 小时
        前的「MFA 设置已变更」通知，根级 ``verification_code`` 里的 "3243"（跟踪
        链接编号）被当作登录验证码提交 → 400 string_below_min_length。
        """
        stale_message = {
            "subject": "Your multi-factor authentication settings have been changed",
            "body": "Settings changed. Security tab (http://url3243.email.openai.com/ls/click?upn=abc)",
            "date": "2026-10-04T09:09:20+00:00",
            "verification_code": "3243",
        }
        payload = {"messages": [stale_message], "verification_code": "3243", "otp_code": "3243"}
        after = client._parse_timestamp("2026-10-05T12:00:00Z")
        code, timestamp = client._payload_code(payload, after)
        self.assertIsNone(code)
        self.assertIsNone(timestamp)
        # after_ts 阙如（调用方不关心时间）时同样不该把 4 位噪声当成验证码
        code, _ = client._payload_code(payload, None)
        self.assertIsNone(code)

    def test_payload_code_rejects_stale_root_field_even_when_six_digits(self):
        """时间过滤失败时，6 位旧码同样不能靠根级字段漏回来。"""
        payload = {
            "messages": [
                {"body": "old login code", "date": "2026-10-04T09:07:54+00:00", "verification_code": "111111"}
            ],
            "verification_code": "111111",
        }
        after = client._parse_timestamp("2026-10-05T12:00:00Z")
        self.assertEqual(client._payload_code(payload, after), (None, None))

    def test_fetch_latest_otp_returns_on_first_hit_without_sleeping(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, patch(
            "core.icloud_hme_client.time.sleep"
        ) as sleep_mock, self._config():
            request_mock.return_value = _ok(
                {
                    "ok": True,
                    "messages": [{"verification_code": "255224", "date": "2026-10-03T04:00:00Z"}],
                    "verification_code": "255224",
                }
            )
            code = client.fetch_latest_otp("a@icloud.com", max_wait=5, poll_interval=1, settle_seconds=0)

        self.assertEqual(code, "255224")
        sleep_mock.assert_not_called()
        _, url = request_mock.call_args.args
        self.assertEqual(url, "http://127.0.0.1:5050/api/v1/mailboxes/a%40icloud.com/messages")
        self.assertEqual(
            request_mock.call_args.kwargs["params"],
            {"project_code": "chatgpt", "limit": 1, "force": 1},
        )

    def test_fetch_latest_otp_times_out_when_inbox_has_no_code(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, patch(
            "core.icloud_hme_client.time.sleep"
        ), self._config():
            request_mock.return_value = _ok({"ok": True, "messages": [], "verification_code": ""})
            with self.assertRaisesRegex(client.IcloudHmeError, "等待 iCloud HME 验证码超时"):
                client.fetch_latest_otp("a@icloud.com", max_wait=0, poll_interval=1, settle_seconds=0)

    def test_fetch_latest_otp_requires_email(self):
        with self.assertRaisesRegex(client.IcloudHmeError, "缺少邮箱地址"):
            client.fetch_latest_otp("   ")

    # ---------- 回收 ----------

    def test_release_account_calls_platform_when_available(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": True, "released": True, "available": True})
            result = client.release_account("a@icloud.com", "available", note="注册失败")

        self.assertTrue(result)
        method, url = request_mock.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://127.0.0.1:5050/api/v1/hme/a%40icloud.com/release")
        self.assertEqual(request_mock.call_args.kwargs["params"], {"project_code": "chatgpt"})

    def test_release_account_treats_platform_error_as_failure(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": False, "error": "项目 code 不存在"})
            self.assertFalse(client.release_account("a@icloud.com", "available"))

    def test_release_account_skips_platform_for_consumed_mailbox(self):
        """邮箱已被 OpenAI 消耗（failed/used），放回池里只会让下次注册必然失败。"""
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            self.assertFalse(client.release_account("a@icloud.com", "failed"))
            self.assertFalse(client.release_account("a@icloud.com", "used"))
            request_mock.assert_not_called()

    def test_release_account_respects_release_switch(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config(
            ICLOUD_HME_RELEASE_ON_FAILED=False
        ):
            self.assertFalse(client.release_account("a@icloud.com", "available"))
            request_mock.assert_not_called()

    def test_release_account_returns_false_on_network_error(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.side_effect = client.requests.RequestException("boom")
            self.assertFalse(client.release_account("a@icloud.com", "available"))

    def test_release_account_drops_cached_context(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": True, "item": {"alias_email": "a@icloud.com"}})
            client.pick_account()
            self.assertIsNotNone(client.get_account_context("a@icloud.com"))

            request_mock.return_value = _ok({"ok": True, "released": True})
            client.release_account("a@icloud.com", "available")

        self.assertIsNone(client.get_account_context("a@icloud.com"))

    # ---------- 错误映射 ----------

    def test_request_maps_401_to_key_error(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _err(401, {"ok": False, "error": "invalid API key"})
            with self.assertRaisesRegex(client.IcloudHmeError, "API Key 无效或已吊销"):
                client.list_projects()

    def test_request_maps_400_to_server_message(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _err(400, {"ok": False, "error": "项目 code 不存在"})
            with self.assertRaisesRegex(client.IcloudHmeError, "项目 code 不存在"):
                client.list_projects()

    def test_request_raises_on_http_200_with_ok_false(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok({"ok": False, "error": "池已耗尽"})
            with self.assertRaisesRegex(client.IcloudHmeError, "池已耗尽"):
                client.list_projects()

    def test_list_projects_filters_non_dict_items(self):
        with patch("core.icloud_hme_client.requests.request") as request_mock, self._config():
            request_mock.return_value = _ok(
                {"ok": True, "projects": [{"code": "chatgpt", "name": "ChatGPT"}, "junk", None]}
            )
            self.assertEqual(client.list_projects(), [{"code": "chatgpt", "name": "ChatGPT"}])

    # ---------- 工具函数 ----------

    def test_parse_timestamp_variants(self):
        expected = 1791000000.0  # 2026-10-03T04:00:00Z
        for raw in ("2026-10-03T04:00:00Z", "2026-10-03T04:00:00+00:00", 1791000000000, "1791000000000"):
            with self.subTest(raw=raw):
                self.assertEqual(client._parse_timestamp(raw), expected)
        for raw in (None, "", "not-a-date"):
            with self.subTest(raw=raw):
                self.assertIsNone(client._parse_timestamp(raw))

    def test_message_code_prefers_direct_field(self):
        self.assertEqual(
            client._message_code({"verification_code": " 123456 ", "body": "code is 999999"}),
            "123456",
        )

    def test_message_code_ignores_css_hex_color_in_html(self):
        """CSS 颜色 #202123 是 6 位纯数字，不能当成验证码。"""
        self.assertIsNone(
            client._message_code(
                {"subject": "hi", "body": '<div style="color:#202123;">Hello</div>'}
            )
        )

    def test_message_code_rejects_notification_mail_noise(self):
        """通知邮件不是验证码邮件：跟踪链接编号/页脚年份都不能当验证码。"""
        mfa_mail = {
            "subject": "Your multi-factor authentication settings have been changed",
            "body_preview": "Your multi factor authentication settings have been changed\r\n"
            "To see the latest status, please navigate to the ChatGPT settings and select the "
            "Security tab (http://url3243.email.openai.com/ls/click?upn=u001.abc)",
            "verification_code": "3243",
        }
        signin_mail = {
            "subject": "New sign-in to your OpenAI account",
            "body_preview": "A new sign-in was detected.\r\n© 2026 OpenAI",
            "verification_code": "2026",
        }
        for message in (mfa_mail, signin_mail):
            with self.subTest(subject=message["subject"]):
                self.assertIsNone(client._message_code(message))

    def test_message_code_keeps_real_six_digit_code(self):
        self.assertEqual(
            client._message_code({"subject": "Your temporary ChatGPT login code", "body_preview": "Enter this code: 919067"}),
            "919067",
        )

    # ---------- helper ----------

    def _config(self, **overrides):
        base = {
            "ICLOUD_HME_API_BASE": "http://127.0.0.1:5050",
            "ICLOUD_HME_API_KEY": "hme_test_key",
            "ICLOUD_HME_PROJECT_CODE": "chatgpt",
            "ICLOUD_HME_RELEASE_ON_FAILED": True,
            "ICLOUD_HME_REQUEST_TIMEOUT": 5,
        }
        base.update(overrides)
        return _MultiPatch(client._email_cfg, base)


class _MultiPatch:
    """小型 patch 组合器，避免每个用例堆十几层 with。"""

    def __init__(self, target, values):
        self._patchers = [patch.object(target, key, value, create=True) for key, value in values.items()]

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc_info):
        for patcher in reversed(self._patchers):
            patcher.stop()
        return False


if __name__ == "__main__":
    unittest.main()
