# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth


class _Session:
    def __init__(self, cf_challenge: bool = False, proxy: str = ""):
        self.calls = 0
        self.reset_count = 0
        self.proxy = proxy
        self.cf_challenge = cf_challenge
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            error = RuntimeError("HTTP Error 403")
            headers = {"cf-mitigated": "challenge"} if self.cf_challenge else {}
            error.response = SimpleNamespace(status_code=403, headers=headers)
            raise error
        return SimpleNamespace(
            status_code=200,
            url="https://auth.openai.com/email-verification",
            raise_for_status=lambda: None,
        )

    def reset_circuit_breaker(self):
        self.reset_count += 1


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_stops_immediately_without_replaying_oauth_state(self):
        session = _Session()
        cookie_jar_identity = session.cookie_jar_identity

        with patch.object(openai_auth.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "403"):
                openai_auth.follow_authorize(
                    session,
                    "https://auth.openai.com/api/accounts/authorize?state=test",
                )

        self.assertEqual(session.calls, 1)
        self.assertEqual(session.reset_count, 0)
        self.assertIs(session.cookie_jar_identity, cookie_jar_identity)
        sleep.assert_not_called()

    def test_authorize_cf_challenge_puts_exit_into_cooldown(self):
        """authorize 这一跳在 preflight 之外，被 CF 挑战必须自己上报坏出口。"""
        session = _Session(cf_challenge=True, proxy="http://bad-exit:9521")

        with patch("config.proxy.report_proxy_result") as report:
            with self.assertRaisesRegex(RuntimeError, "403"):
                openai_auth.follow_authorize(
                    session,
                    "https://auth.openai.com/api/accounts/authorize?state=test",
                )

        report.assert_called_once()
        self.assertEqual(report.call_args.args[0], "http://bad-exit:9521")
        self.assertEqual(report.call_args.kwargs["status"], 403)

    def test_authorize_plain_403_does_not_report_exit(self):
        """普通业务 403（无 cf-mitigated=challenge）不是出口信誉问题，不上报。"""
        session = _Session(cf_challenge=False, proxy="http://fine-exit:9521")

        with patch("config.proxy.report_proxy_result") as report:
            with self.assertRaisesRegex(RuntimeError, "403"):
                openai_auth.follow_authorize(
                    session,
                    "https://auth.openai.com/api/accounts/authorize?state=test",
                )

        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
