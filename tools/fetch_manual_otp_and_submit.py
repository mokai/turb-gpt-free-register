# -*- coding: utf-8 -*-
"""为处于人工输入等待的换绑任务从 IMAP 拉取 OTP 并提交。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core import imap_mail_client as imc

API = "http://127.0.0.1:5001"
HEADERS = {"X-Auth-Code": "admin123456", "Content-Type": "application/json", "Accept-Encoding": "identity"}


def api_get(path: str):
    req = urllib.request.Request(API + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(path: str, payload: dict):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pending_requests() -> list[dict]:
    data = api_get("/api/manual-otp/waiting")
    return data.get("waiting") or []


def fetch_code(email: str, after_ts: float) -> str | None:
    account = imc.get_account_context(email)
    if account is None:
        print(f"[!] 邮箱池中找不到 {email} 的 IMAP 凭据", flush=True)
        return None
    mail = None
    try:
        mail = imc._connect(account)
        messages = imc._search_messages(mail, datetime.fromtimestamp(after_ts - 30, tz=timezone.utc))
    except Exception as exc:
        print(f"[!] {email} IMAP 读取失败: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    messages.sort(key=lambda item: item.get("date") or "", reverse=True)
    for item in messages:
        recipient = " ".join(str(item.get(k) or "") for k in ("to", "deliveredTo", "xOriginalTo")).lower()
        if email.lower() not in recipient or not imc.looks_like_openai_email(item):
            continue
        otp = imc.extract_otp(item)
        if not otp:
            continue
        raw_ts = item.get("date") or ""
        try:
            ts = parsedate_to_datetime(raw_ts).timestamp()
        except Exception:
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0
        if ts and ts < after_ts - 30:
            continue
        return otp
    return None


def main() -> None:
    print("[*] 开始监控人工输入等待队列…", flush=True)
    deadline = time.time() + 560
    submitted: set[str] = set()
    while time.time() < deadline:
        waiting = pending_requests()
        targets = [w for w in waiting if w.get("purpose", "").startswith("换绑") and not w.get("expired")]
        if not targets:
            if waiting:
                print(f"[*] 等待队列还有 {len(waiting)} 项（非换绑），继续", flush=True)
            else:
                print("[*] 人工输入队列已清空", flush=True)
            time.sleep(8)
            continue
        for w in targets:
            email = str(w.get("email") or "")
            rid = str(w.get("request_id") or "")
            if rid in submitted:
                continue
            after_ts = float(w.get("since") or time.time())
            code = fetch_code(email, after_ts)
            if code:
                print(f"[*] {email} 取到验证码（不打印），提交 request_id={rid[:8]}…", flush=True)
                res = api_post("/api/manual-otp", {"email": email, "code": code, "request_id": rid})
                print(f"    提交结果: {res}", flush=True)
                submitted.add(rid)
            else:
                print(f"[*] {email} 暂无新验证码邮件", flush=True)
        time.sleep(10)
    print("[*] 脚本结束", flush=True)


if __name__ == "__main__":
    main()
