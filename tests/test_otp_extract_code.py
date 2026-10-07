# -*- coding: utf-8 -*-
"""``core.otp_utils.extract_code`` 的取码测试。

覆盖点：
1. 结构锚点（灰底块 / 等宽字体）在**无任何英文关键字**的印地语邮件上必须命中；
2. 返回约定：命中返回字符串、未命中返回空串（不是 None）；
3. inline style 里的 CSS 颜色值（``#202123`` / ``#353740``，本身就是 6 位纯数字）
   不能被当成验证码；
4. 验证码紧贴 Unicode 字母时不能漏。
"""
import unittest

from core.otp_utils import extract_code

#: 真实 OpenAI 印地语模板的精简版。保留全部结构性特征：
#: style 块、inline 的 CSS 颜色陷阱、mso 注释里的 Lucida Console、灰底等宽块。
INDIA_HTML = """<html><head>
<title>आपका अस्थायी ChatGPT वेरिफिकेशन कोड</title>
<style type="text/css">
  #bodyCell { padding: 20px; }
  #bodyTable { width: 560px; }
  @media only screen and (max-width: 480px) { #signIn { max-width: 280px !important; } }
</style>
</head><body>
<div class="top" style="background-color: #ffffff;color:#202123; padding: 56px 0 32px 0;">
  <img src="https://cdn.example.net/d4ef4a73d51fe844/206x51.png" width="206" height="51" alt="ChatGPT">
</div>
<table class="main"><tr>
  <td style="background-color: #ffffff;color:#353740; text-align: left; line-height:1.5;">
    <p style="font-size: 16px; line-height: 24px; margin: 0;">
      जारी रखने के लिए ये टेम्पररी वेरिफ़िकेशन कोड डालें:
    </p>
    <p style="font-family: Menlo, Monaco, Lucida Console, Arial; font-size: 24px; line-height: 28px; background-color: #F3F3F3; color: #5D5D5D; border-radius: 16px; padding: 28px 24px; margin: 24px 0;">
      <!--[if mso]>
          <span style="font-family: Lucida Console, Arial, sans-serif;">
      <![endif]-->
      255224
      <!--[if mso]>
          </span>
      <![endif]-->
    </p>
    <p style="margin: 0; color: #8F8F8F;"><br/>सादर,<br/>ChatGPT टीम</p>
  </td>
</tr></table>
</body></html>"""


class ExtractCodeTests(unittest.TestCase):
    def test_india_html_hits_structural_anchor(self):
        """印地语邮件全文无 code/verification，只能靠 HTML 结构锚点。"""
        self.assertEqual(extract_code(INDIA_HTML), "255224")

    def test_india_html_bare_keywords_are_absent(self):
        lowered = INDIA_HTML.lower()
        for kw in ("verification", "verify", "security", "code"):
            self.assertNotIn(kw, lowered)

    def test_css_color_values_never_returned(self):
        """inline style 的 #202123 / #353740 恰好是 6 位纯数字，不能当验证码。"""
        for wrong in ("202123", "353740"):
            self.assertNotEqual(extract_code(INDIA_HTML), wrong)

    def test_style_only_html_returns_empty(self):
        """只有 CSS 颜色、没有结构锚点和关键字时，必须返回空串。"""
        self.assertEqual(
            extract_code('<div style="color:#202123;background:#353740;">Hello</div>'),
            "",
        )

    def test_plain_text_english(self):
        self.assertEqual(
            extract_code(
                "Your ChatGPT temporary verification code is 255224. "
                "If you weren't trying to create an account, ignore this email."
            ),
            "255224",
        )

    def test_plain_text_multilingual_keywords(self):
        cases = {
            "simplified": "您的验证码是 255224",
            "traditional": "您的驗證碼是 255224",
            "japanese": "認証コードは 255224 です",
            "korean": "인증 코드는 255224",
        }
        for name, text in cases.items():
            with self.subTest(lang=name):
                self.assertEqual(extract_code(text), "255224")

    def test_code_glued_to_unicode_letters(self):
        """字母与数字同属 \\w，验证码紧贴印地语字母时 \\b 会漏，前瞻后顾不会。"""
        self.assertEqual(extract_code("ये वेरिफ़िकेशन कोड255224"), "255224")

    def test_fallback_first_number_after_stripping_tags(self):
        """无关键字、无结构锚点时，退化为剥标签后的第一个 4-8 位数字。"""
        self.assertEqual(
            extract_code("<div><span>255224</span></div>"),
            "255224",
        )

    def test_dict_input_prefers_html_field(self):
        self.assertEqual(
            extract_code({"subject": "आपका कोड", "content": INDIA_HTML}),
            "255224",
        )

    def test_dict_input_falls_back_to_plain_field(self):
        self.assertEqual(
            extract_code({"subject": "Your code", "bodyPreview": "Your code is 654321"}),
            "654321",
        )

    def test_empty_and_missing_input_return_empty_string(self):
        for value in ("", None, {}, "hello world", "<p>no digits here</p>"):
            with self.subTest(value=value):
                self.assertEqual(extract_code(value), "")

    def test_returns_str_never_none(self):
        for value in ("", None, INDIA_HTML, "hello"):
            with self.subTest(value=value):
                self.assertIsInstance(extract_code(value), str)


if __name__ == "__main__":
    unittest.main()
