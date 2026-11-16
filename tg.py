#!/usr/bin/env python3
"""Westly Telegram channel — reliable pings out, conversation back.

Both halves (pings out, chat back) ride one HTTP API with no carrier and no cost.
Inbound uses Telegram's native long poll, so a reply lands in seconds rather than
waiting out a poll interval. Nothing here is exposed to the internet: Westly calls
out to Telegram, there is no webhook.

Self-check: python tg.py --selftest
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import config

REPO = config.REPO
STATE = REPO / ".tg_offset.json"
API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096      # Telegram's hard cap on one message
POLL = 50         # long-poll seconds; the request parks until a message arrives


def creds():
    """(token, chat_id) when configured, else None. chat_id may be None pre-discovery."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        return None
    return token, os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None


def api(method, token, params=None, http_timeout=30):
    """Call the Bot API. `params` is a dict, not kwargs — Telegram has its own
    `timeout` parameter and merging the two namespaces collides with this call's."""
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise RuntimeError(f"telegram {method} failed ({e.code}): {detail}") from None
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method} rejected: {body.get('description')}")
    return body["result"]


def send(text, token=None, chat=None):
    """Send one message, split if it exceeds Telegram's cap. Returns messages sent."""
    if token is None or chat is None:
        got = creds()
        if not got:
            raise RuntimeError("TELEGRAM_TOKEN not set in westly.env")
        token, chat = got[0], chat or got[1]
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID not set — message the bot once so it can be discovered")
    text = text.strip() or "(empty)"
    sent = 0
    for part in split(text):
        api("sendMessage", token, {"chat_id": chat, "text": part,
                                   "disable_web_page_preview": "true"})
        sent += 1
    return sent


def typing(token=None, chat=None):
    """Show 'typing…' in the owner's chat. Telegram expires it after ~5s, so it needs repeating."""
    if token is None or chat is None:
        got = creds()
        if not got:
            return
        token, chat = got[0], chat or got[1]
    if chat:
        api("sendChatAction", token, {"chat_id": chat, "action": "typing"})


def split(text, limit=LIMIT):
    """Break text into Telegram-sized pieces, preferring line boundaries."""
    out = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


def offset():
    try:
        return int(json.loads(STATE.read_text())["offset"])
    except Exception:
        return None


def set_offset(value):
    STATE.write_text(json.dumps({"offset": int(value)}))


def select_updates(updates, chat, mark):
    """(next_offset, [texts]) from the owner's chat only. Pure.

    next_offset always advances past everything seen — a message from a stranger or
    a non-text update must still be acknowledged, or it is replayed forever.
    """
    texts, top = [], None  # stays None on an empty poll: advancing past an id we
    for u in updates:      # never saw would skip the next message Telegram assigns
        uid = u.get("update_id")
        if uid is None:
            continue
        top = uid if top is None else max(top, uid)
        msg = u.get("message") or u.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue
        body = (msg.get("text") or "").strip()
        if body:
            texts.append(body)
    return (mark if top is None else top + 1), texts


def poll(token=None, chat=None, timeout=POLL, log=None):
    """Long-poll for the owner's messages. Blocking up to `timeout` — call in a thread."""
    if token is None or chat is None:
        got = creds()
        if not got:
            return []
        token, chat = got[0], chat or got[1]
    if not chat:
        return []
    mark = offset()
    params = {"timeout": timeout}
    if mark is not None:
        params["offset"] = mark
    updates = api("getUpdates", token, params, http_timeout=timeout + 15)
    nxt, texts = select_updates(updates, chat, mark)
    if nxt is not None:
        set_offset(nxt)
    if log and texts:
        log.info("telegram: %d message(s) from the owner", len(texts))
    return texts


def discover_chat(token):
    """Chat ids that have messaged the bot — used once, to fill TELEGRAM_CHAT_ID."""
    found = {}
    for u in api("getUpdates", token):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or chat.get("type")
            found[str(chat["id"])] = who
    return found


def selftest():
    assert split("hi") == ["hi"]
    text = "\n".join(["x" * 80] * 200)
    parts = split(text)
    assert all(len(p) <= LIMIT for p in parts) and "\n".join(parts) == text
    blob = "y" * 9000  # nothing to split on -> hard cuts at the cap
    parts = split(blob)
    assert [len(p) for p in parts] == [4096, 4096, 808] and "".join(parts) == blob
    assert split("a\nb", 2) == ["a", "b"]

    def upd(uid, chat, text=None, key="message"):
        m = {"chat": {"id": chat}}
        if text is not None:
            m["text"] = text
        return {"update_id": uid, key: m}

    nxt, texts = select_updates([upd(5, 111, "hey"), upd(6, 111, "  again  ")], 111, None)
    assert (nxt, texts) == (7, ["hey", "again"]), (nxt, texts)
    # a stranger is ignored but still acknowledged, else getUpdates replays it forever
    nxt, texts = select_updates([upd(8, 999, "who am i"), upd(9, 111, "ok")], 111, 8)
    assert (nxt, texts) == (10, ["ok"]), (nxt, texts)
    nxt, texts = select_updates([upd(11, 999, "spam")], 111, 11)
    assert (nxt, texts) == (12, []), (nxt, texts)
    # non-text updates (photo, sticker, join) advance the offset without producing a turn
    nxt, texts = select_updates([upd(12, 111)], 111, 12)
    assert (nxt, texts) == (13, []), (nxt, texts)
    assert select_updates([], 111, 4) == (4, [])
    assert select_updates([], 111, None) == (None, [])
    # chat id compared as text, so an int from the API matches a string from env
    assert select_updates([upd(1, 111, "x")], "111", None)[1] == ["x"]
    assert select_updates([upd(2, 111, "y")], 111, None)[1] == ["y"]
    # an edited message is still a message
    assert select_updates([upd(3, 111, "fixed", "edited_message")], 111, None)[1] == ["fixed"]
    import inspect
    sig = inspect.signature(api).parameters
    assert "params" in sig and "http_timeout" in sig and "timeout" not in sig, sig
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.values()), sig
    print("tg selftest ok")


if __name__ == "__main__":
    selftest()
