# -*- coding: utf-8 -*-
"""authorize（步骤4）遇到 HTTP 403 时的重试策略。

2026-10-13 合并 main 后，authorize 的 403 采用 main 分支的策略：
保留当前指纹与 Cookie、清本地熔断后按指数退避重试；重试耗尽才抛出。
因此这里断言「会重试」「耗尽才报错」「authorize 这一跳不上报代理出口」。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth


class _Session:
    def __init__(self, cf_challenge: bool = False, proxy: str = "", fail_times: int = 1):
        self.calls = 0
        self.reset_count = 0
        self.proxy = proxy
        self.cf_challenge = cf_challenge
        #: 前 N 次请求返回 403，之后返回 200。
        self.fail_times = fail_times
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
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


def _max_attempts() -> int:
    return openai_auth._proxy_retry_config()[0]


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_retries_same_session_and_then_succeeds(self):
        """403 后保留同一个 session / Cookie Jar，只清熔断并退避重试。"""
        session = _Session(fail_times=1)
        cookie_jar_identity = session.cookie_jar_identity

        with patch.object(openai_auth.time, "sleep") as sleep:
            final_url = openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/api/accounts/authorize?state=test",
            )

        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.reset_count, 1)
        self.assertIs(session.cookie_jar_identity, cookie_jar_identity)
        sleep.assert_called_once()

    def test_403_exhausting_attempts_raises(self):
        """持续 403 时重试到上限才抛出，每次重试都清一次本地熔断。"""
        max_attempts = _max_attempts()
        session = _Session(fail_times=max_attempts + 1)

        with patch.object(openai_auth.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "403"):
                openai_auth.follow_authorize(
                    session,
                    "https://auth.openai.com/api/accounts/authorize?state=test",
                )

        self.assertEqual(session.calls, max_attempts)
        self.assertEqual(session.reset_count, max_attempts - 1)
        self.assertEqual(sleep.call_count, max_attempts - 1)

    def test_authorize_403_does_not_report_proxy_exit(self):
        """authorize 这一跳不再上报代理出口（重试策略下出口冷却由上层负责）。"""
        session = _Session(cf_challenge=True, proxy="http://bad-exit:9521", fail_times=1)

        with patch("config.proxy.report_proxy_result") as report:
            openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/api/accounts/authorize?state=test",
            )

        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
