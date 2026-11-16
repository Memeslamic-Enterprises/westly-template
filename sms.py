#!/usr/bin/env python3
"""Westly SMS channel — outbound texts to the owner via a carrier email-to-SMS gateway.

Optional last-resort alert channel for bin/westly-ping: it depends on neither the bot
nor a data connection. Sends through any SMTP account (MAIL_* in westly.env; use an
app password). Outbound only: carriers drop the handset->email return leg, so
two-way chat lives on Telegram (tg.py). US gateways only.

Self-check: python sms.py --selftest
"""
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import config

REPO = config.REPO
SENT_STATE = REPO / ".sms_sent.json"
DEDUPE_WINDOW = 4 * 3600   # identical-text mute; an hourly flapper slips a 1h window
RATE_WINDOW = 3600         # the MAX_PER_HOUR cap counts sends inside this
MAX_CHARS = 300     # gateways drop or fragment past ~160; alerts stay one-liners
REPLY_CHARS = 600   # a chat answer gets more rope than an alert
MAX_PER_HOUR = 6    # hard ceiling on unforced texts: a flapping monitor must never own the phone

# Shorthands map to the gateway of the network the MVNO actually rides.
GATEWAYS = {
    "verizon": "vtext.com", "visible": "vtext.com", "xfinity": "vtext.com",
    # MMS gateways: longer messages, and the handset->email reply path actually returns
    "verizonmms": "vzwpix.com", "visiblemms": "vzwpix.com",
    "attmms": "mms.att.net", "tmobilemms": "tmomail.net",
    "att": "txt.att.net", "cricket": "txt.att.net",
    "tmobile": "tmomail.net", "mint": "tmomail.net", "metro": "mymetropcs.com",
    "googlefi": "msg.fi.google.com", "uscellular": "email.uscc.net",
    "boost": "sms.myboostmobile.com", "spectrum": "vtext.com",
}


def resolve(spec):
    """'(212) 555-0123@verizon' -> '2125550123@vtext.com'. Raises on junk."""
    spec = spec.strip().lower()
    if spec.count("@") != 1:
        raise ValueError(f"SMS_TO entry needs exactly one '@': {spec!r}")
    local, domain = spec.split("@")
    digits = "".join(c for c in local if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"not a 10-digit US number: {local!r}")
    domain = GATEWAYS.get(domain, domain)
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError(f"unknown carrier shorthand / bad domain: {domain!r}")
    return f"{digits}@{domain}"


def recipients():
    raw = os.environ.get("SMS_TO", "").strip()
    if not raw:
        raise RuntimeError("SMS_TO not set in westly.env (e.g. SMS_TO=2125550123@verizon)")
    return [resolve(p) for p in raw.split(",") if p.strip()]


def truncate(text, limit=MAX_CHARS):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def throttle(body, now, state=None):
    """(allowed, reason) for an unforced text. Records the send when it allows one.

    Two gates, because they stop different failures: the exact-repeat mute stops one
    monitor flapping, the hourly cap stops several of them (or one with a varying
    message) between them turning a phone into a pager. Prunes as it reads.
    """
    state = state or SENT_STATE
    try:
        seen = json.loads(state.read_text())
    except Exception:
        seen = {}
    seen = {k: v for k, v in seen.items() if now - v < DEDUPE_WINDOW}
    if body in seen:
        return False, "duplicate within the mute window"
    if sum(1 for v in seen.values() if now - v < RATE_WINDOW) >= MAX_PER_HOUR:
        return False, f"hourly cap of {MAX_PER_HOUR} texts reached"
    seen[body] = now
    try:
        state.write_text(json.dumps(seen))
    except OSError:
        pass  # throttling is a courtesy; never block a real alert on a write failure
    return True, ""


def send(text, force=False, limit=MAX_CHARS):
    """Mail `text` to every SMS_TO gateway. Returns the body sent, or None if deduped."""
    body = truncate(text, limit)
    if not body:
        raise ValueError("refusing to send an empty text")
    to = recipients()
    if not force:
        allowed, reason = throttle(body, time.time())
        if not allowed:
            print(f"sms: not sent ({reason}): {body[:60]}", file=sys.stderr)
            return None
    user, password = smtp_account()
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg["Subject"] = ""  # carriers prepend the subject to the body; keep it out of the way
    msg.set_content(body)
    host = config.get("MAIL_SMTP_HOST")
    if not host:
        raise RuntimeError("MAIL_SMTP_HOST not set in westly.env")
    try:
        port = int(config.get("MAIL_SMTP_PORT", "465"))
    except ValueError:
        raise RuntimeError("MAIL_SMTP_PORT must be a number") from None
    if port == 465:
        conn = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        conn.starttls()
    with conn as s:
        s.login(user, password)
        s.send_message(msg)
    return body


def smtp_account():
    user, password = config.get("MAIL_USER"), config.get("MAIL_PASSWORD")
    if not user or not password:
        raise RuntimeError("MAIL_USER / MAIL_PASSWORD not set in westly.env")
    return user, password


def selftest():
    assert resolve("2125550123@verizon") == "2125550123@vtext.com"
    assert resolve(" (212) 555-0123@Verizon ") == "2125550123@vtext.com"
    assert resolve("12125550123@txt.att.net") == "2125550123@txt.att.net"
    assert resolve("212-555-0123@mint") == "2125550123@tmomail.net"
    for bad in ["2125550123", "212555@verizon", "2125550123@nope",
                "a@b@verizon", "21255501235@verizon"]:
        try:
            resolve(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted junk: {bad}")

    prior = os.environ.get("SMS_TO")
    os.environ["SMS_TO"] = "2125550123@verizon, 212-555-0199@mint"
    try:
        assert recipients() == ["2125550123@vtext.com", "2125550199@tmomail.net"]
    finally:
        if prior is None:
            os.environ.pop("SMS_TO", None)
        else:
            os.environ["SMS_TO"] = prior

    assert truncate("a  b\n c") == "a b c"
    assert truncate("x" * 400).endswith("…") and len(truncate("x" * 400)) == MAX_CHARS
    assert len(truncate("x" * 900, REPLY_CHARS)) == REPLY_CHARS

    tmp = Path("/tmp/.westly-sms-selftest.json")
    tmp.unlink(missing_ok=True)
    assert throttle("hi", 1000, tmp) == (True, "")
    assert throttle("hi", 1100, tmp)[0] is False              # exact repeat muted
    assert throttle("other", 1100, tmp)[0] is True
    assert throttle("hi", 1000 + DEDUPE_WINDOW + 1, tmp)[0] is True   # window expired
    tmp.unlink(missing_ok=True)
    # a flapping monitor with a *varying* message defeats the dupe mute; the cap catches it
    for i in range(MAX_PER_HOUR):
        assert throttle(f"alert {i}", 2000 + i, tmp)[0] is True, i
    ok, why = throttle("alert overflow", 2000 + MAX_PER_HOUR, tmp)
    assert ok is False and "hourly cap" in why, why
    # ...and the cap lifts as the window rolls forward, rather than wedging shut
    assert throttle("alert later", 2000 + DEDUPE_WINDOW + 1, tmp)[0] is True
    tmp.unlink(missing_ok=True)
    # an hourly flapper: repeats ~63min apart stay muted for the full 4h
    assert throttle("storm", 5000, tmp)[0] is True
    assert throttle("storm", 5000 + 3800, tmp)[0] is False
    assert throttle("storm", 5000 + DEDUPE_WINDOW + 1, tmp)[0] is True
    tmp.unlink(missing_ok=True)
    # 4h dedupe retention must not tighten the hourly cap for distinct texts
    for i in range(MAX_PER_HOUR):
        assert throttle(f"burst {i}", 9000 + i, tmp)[0] is True, i
    assert throttle("burst 7th", 9000 + MAX_PER_HOUR, tmp)[0] is False
    assert throttle("burst late", 9000 + RATE_WINDOW + MAX_PER_HOUR, tmp)[0] is True
    tmp.unlink(missing_ok=True)

    print("sms selftest ok")


if __name__ == "__main__":
    selftest()
