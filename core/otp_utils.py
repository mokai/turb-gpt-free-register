# -*- coding: utf-8 -*-
"""
OTP 检测与抽取通用工具，被 outlook_client（Outlook 邮箱）使用。

要求：
    - 多语言关键字识别（英 / 中 / 日 / 韩）
    - 字段名容错（不同邮件 API 用不同的字段命名约定）
    - 上下文优先：在多个 6 位数中，选择离"验证码"等关键字最近的那个
"""
import re

_OPENAI_SENDER_HINT = "openai"

# 多语言关键字（用于判断是否是 OpenAI 邮件）
_OPENAI_KEYWORDS = (
    "chatgpt", "openai",
    # 英文
    "verification code", "code is", "your code", "verify your email",
    # 中文
    "代码", "验证码", "确认码",
    # 日文
    "認証コード", "検証コード", "確認コード", "一時検証", "認証",
    # 韩文
    "인증 코드", "확인 코드",
)

# OTP 上下文关键字（用于在多个 6 位数中挑出真正的验证码）
_OTP_CONTEXT_KEYWORDS = (
    "code", "verify", "verification",
    "代码", "验证", "确认",
    "コード", "認証", "検証", "確認",
    "코드", "인증",
)

_OTP_REGEX = re.compile(r"\b(\d{6})\b")


def _get_field(item: dict, *names: str) -> str:
    """
    从邮件 dict 中按顺序尝试多个可能的字段名，返回第一个非空字符串。
    用于兼容不同邮件 API 的字段命名约定（例如 sendEmail / from / fromEmail / from.address）。
    """
    for name in names:
        if "." in name:
            # 支持 "from.emailAddress.address" 这种点路径
            value = item
            for part in name.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, str) and value:
                return value
        else:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def looks_like_openai_email(item: dict) -> bool:
    """
    判断邮件是否来自 OpenAI / ChatGPT。多语言、多字段名兼容。

    字段名容错（不同 API 返回风格不一）：
        发件人:  sendEmail / from / fromEmail / from.emailAddress.address
        发件人名:sendName / fromName / from.emailAddress.name
        纯文本:  text / bodyPreview / bodyText
        HTML:    content / body / html / body.content / bodyHtml
    """
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _get_field(item, "subject").lower()
    text = _get_field(item, "text", "bodyPreview", "bodyText").lower()
    content = _get_field(item, "content", "body", "html", "body.content", "bodyHtml").lower()

    if _OPENAI_SENDER_HINT in sender or _OPENAI_SENDER_HINT in sender_name:
        return True

    return any(k in s for s in (subject, text, content) for k in _OPENAI_KEYWORDS)


def extract_otp(item: dict) -> str | None:
    """
    从邮件中抽出 6 位 OTP。

    抽取顺序：
        1. subject（OpenAI 部分邮件直接把 6 位数放在主题里，例 "Your OpenAI code is 525210"）
        2. 纯文本字段（text / bodyPreview / bodyText）
        3. HTML 字段（content / html / body / body.content / bodyHtml，去标签后）

    若 body 中含多个 6 位数，优先选择离 "验证码 / code / 認証" 等关键字最近的那个。
    """
    # 1. 主题里如果直接有 6 位数，最可信
    subject = _get_field(item, "subject")
    if subject:
        codes_in_subject = _OTP_REGEX.findall(subject)
        if len(codes_in_subject) == 1:
            # 主题里恰好只有一个 6 位数，几乎肯定就是 OTP
            return codes_in_subject[0]

    # 2. body 字段
    candidates = [
        ("text", _get_field(item, "text", "bodyPreview", "bodyText")),
        ("html", _get_field(item, "content", "html", "body", "body.content", "bodyHtml")),
    ]

    for kind, body in candidates:
        if not body:
            continue
        # 无论 text 还是 html，都先去 HTML 标签和 style 属性
        # （QQ 邮箱转发的 OpenAI 邮件，text 字段也可能含 HTML）
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        all_codes = _OTP_REGEX.findall(body)
        if not all_codes:
            continue
        body_lower = body.lower()
        # 优先选离上下文关键字最近的 6 位数
        for code in all_codes:
            idx = body_lower.find(code)
            if idx < 0:
                continue
            window = body_lower[max(0, idx - 60): idx + 6 + 60]
            if any(k.lower() in window for k in _OTP_CONTEXT_KEYWORDS):
                return code
        return all_codes[0]
    return None


# =============================================================================
# extract_code —— 结构优先 + 文案兜底的取码入口
#
# 与上面的 extract_otp 是并存关系：extract_otp 的入参是邮件 dict、未命中返回
# None，且**完全依赖文案关键字**（_OTP_CONTEXT_KEYWORDS 只覆盖英/中/日/韩）。
# 下面这组遵循「命中返回验证码字符串，未命中返回空串」的约定，并优先锚定与
# 邮件语言无关的 HTML 结构。
# =============================================================================

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_STYLE_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

#: 等宽字体系列。验证码在邮件模板里通常单独占一个等宽字体块，该特征与语言无关。
_MONO_FAMILY = (
    r"(?:Menlo|Monaco|Lucida\s*Console|Consolas|Courier|monospace"
    r"|Roboto\s*Mono|IBM\s*Plex\s*Mono|Source\s*Code)"
)

#: 结构锚点：灰底代码块（background-color:#F3F3F3）或等宽字体块。
_HTML_BLOCK_RE = re.compile(
    r"<(?:p|div|td|span|h[1-6])\b[^>]*style\s*=\s*[\"'][^\"']*(?:"
    r"background-color\s*:\s*#F3F3F3"
    r"|font-family\s*:[^;\"']*" + _MONO_FAMILY + r")"
    r"[^\"']*[\"'][^>]*>(.*?)</(?:p|div|td|span|h[1-6])>",
    re.DOTALL | re.IGNORECASE,
)

#: 取码。用前后瞻而不用 \b：验证码紧贴 Unicode 字母（如印地语 "कोड255224"）时，
#: 字母与数字同属 \w，\b 不成立会整段漏掉。
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

#: 文案关键字。仅作兜底：这只覆盖英/中/日/韩，印地语、俄语、西语等邮件里
#: 一个关键字都不会出现，只靠文案会直接取不到码。
_CODE_KEYWORDS = (
    "verification", "verify", "security", "one-time", "otp",
    "验证码", "驗證碼", "校验码",
    "認証コード", "検証コード", "確認コード",
    "인증 코드", "확인 코드",
    "code",
)
_KEYWORD_RE = re.compile(
    r"(?:" + "|".join(_CODE_KEYWORDS) + r")[^0-9]{0,30}(\d{4,8})",
    re.IGNORECASE,
)


def _coerce_mail_text(email_content) -> str:
    """把 str / 邮件 dict 统一成可检索文本；HTML 原样保留，供结构锚点使用。"""
    if isinstance(email_content, str):
        return email_content
    if not isinstance(email_content, dict):
        return str(email_content or "")

    # 优先取富文本字段（结构锚点需要标签本身），再退回纯文本字段。
    for key in ("content", "html", "bodyHtml", "body.content", "body"):
        value = _get_field(email_content, key)
        if value:
            return str(value)
    return str(_get_field(email_content, "text", "bodyPreview", "bodyText") or "")


def extract_code(email_content) -> str:
    """从邮件内容提取验证码；提取不到返回空串。

    返回约定：命中返回验证码字符串，未命中返回 ``""``（不返回 ``None``、
    不抛异常），便于直接 ``if not code:`` 判空。

    提取顺序：

    1. **HTML 结构锚点** —— 灰底代码块 ``background-color:#F3F3F3`` 或等宽字体
       （``Menlo`` / ``Monaco`` / ``Lucida Console`` / ``Consolas`` / ``Courier``）
       的 ``<p>/<div>/<td>/<span>/<h1-6>`` 块。该特征与邮件语言无关，是主路径。
    2. **文案关键字 + 30 字符窗口** —— ``verification`` / ``verify`` /
       ``security`` / ``otp`` / ``code`` / ``验证码`` / ``認証コード`` / ``인증 코드``
       等，覆盖英/中/日/韩。
    3. **兜底** —— 剥标签后的可见文本里第一个 4-8 位数字。

    注意：**不要**把它直接指向未剥标签的原始 HTML 却指望第 2/3 层兜底 ——
    OpenAI 邮件 inline style 里的 ``color:#202123`` / ``color:#353740`` 本身就是
    6 位纯十进制数字，裸 ``\\d{4,8}`` 会先命中它们而取错码。本函数内部始终先删
    HTML 注释、再剥 ``<style>``/标签，所以那几个颜色值不会进入候选。
    """
    raw = _coerce_mail_text(email_content)
    if not raw:
        return ""

    # 结构锚点前必须先删注释：`<!--[if mso]>` 注释里同样会出现
    # `<span style="font-family: Lucida Console, Arial, sans-serif;">`。
    cleaned = _COMMENT_RE.sub(" ", raw)

    # 第 1 层：HTML 结构锚点（与语言无关）
    for block in _HTML_BLOCK_RE.findall(cleaned):
        hit = _CODE_RE.search(block)
        if hit:
            return hit.group(1)

    # 第 2/3 层都在剥离 style/script 与标签后的可见文本上进行
    text = _STYLE_TAG_RE.sub(" ", cleaned)
    text = _TAG_RE.sub(" ", text)

    # 第 2 层：文案关键字窗口
    hit = _KEYWORD_RE.search(text)
    if hit:
        return hit.group(1)

    # 第 3 层：兜底取第一个 4-8 位数字
    hit = _CODE_RE.search(text)
    return hit.group(1) if hit else ""
