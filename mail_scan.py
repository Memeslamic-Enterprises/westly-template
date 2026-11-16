#!/usr/bin/env python3
"""Westly mail scan — digest the inbox mail that arrived since the last scan (optional).

Deterministic fetch + format only; the classification happens in prompts/mail.md.
Reads INBOX over IMAP by UID (read-only), writes the newest messages (headers + a
text snippet) to memory/mail/scan.txt, and records where it stopped in
.mail-scan.json so nothing is scanned twice. Senders matching MAIL_SKIP_FROM
(comma-separated substrings) are left out of the digest.

No-ops gracefully rather than failing:
  no credentials -> prints NO_CREDENTIAL, exit 0
  nothing new    -> prints NO_NEW_MAIL,   exit 0

Run modes:
  mail_scan.py            real run
  mail_scan.py --selftest assert-based checks of the pure logic (offline)
"""
import email
import imaplib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from html.parser import HTMLParser

import config

REPO = config.REPO
STATE_FILE = REPO / ".mail-scan.json"
SCAN = REPO / "memory" / "mail" / "scan.txt"
TZ = config.TZ
IMAP_HOST = config.get("MAIL_IMAP_HOST")
IMAP_TIMEOUT = 30
SKIP_FROM = tuple(h.strip().lower() for h in config.get("MAIL_SKIP_FROM").split(",") if h.strip())

HEADER_FIELDS = ("From", "To", "Subject", "Date")
MAX_MESSAGES = 25          # messages digested per scan; older new mail is only counted
HEADERS_ONLY_MAX = 200     # past the cap, this many more get a From/Subject/Date line (10/22: 176 went unread)
MAX_FETCH_BYTES = 200_000  # over this, fetch headers instead of the whole message
SNIPPET_CHARS = 2000
HEADER_CHARS = 300
NO_BODY = "(no text body)"


def collapse(text, limit):
    """Whitespace runs squashed to single spaces, truncated to `limit` chars."""
    return " ".join((text or "").split())[:limit]


def parse_uids(data):
    """Flatten an IMAP SEARCH response (bytes or str) into int uids."""
    uids = []
    for item in data or []:
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("ascii", "replace")
        if not isinstance(item, str):
            continue
        uids += [int(tok) for tok in item.split() if tok.isdigit()]
    return uids


def parse_uidvalidity(items):
    """UIDVALIDITY int out of a SELECT/response payload, or 0 if absent."""
    for item in items or []:
        if isinstance(item, tuple):
            item = b"".join(p for p in item if isinstance(p, (bytes, bytearray)))
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("ascii", "replace")
        if not isinstance(item, str):
            continue
        m = re.search(r"UIDVALIDITY\s+(\d+)", item, re.IGNORECASE)
        if m:
            return int(m.group(1))
        if item.strip().isdigit():
            return int(item.strip())
    return 0


def parse_size(data):
    """RFC822.SIZE out of a FETCH response, or 0 if it wasn't reported."""
    for item in data or []:
        if isinstance(item, tuple):
            item = b"".join(p for p in item if isinstance(p, (bytes, bytearray)))
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("ascii", "replace")
        if not isinstance(item, str):
            continue
        m = re.search(r"RFC822\.SIZE\s+(\d+)", item, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def needs_reset(state, uidvalidity):
    """True when stored state can't be trusted for this mailbox generation.

    Covers the first run ever (no/garbage state) and a UIDVALIDITY change,
    which invalidates every uid the mailbox previously handed out.
    """
    try:
        stored = int(state["uidvalidity"])
        int(state["last_uid"])
    except (KeyError, TypeError, ValueError):
        return True
    return stored != int(uidvalidity)


def new_uids(uids, last_uid):
    """Uids strictly newer than last_uid, newest first.

    A `n:*` range search always returns the highest-uid message even when it
    is older than n, so the numeric filter is required, not defensive.
    """
    return sorted({u for u in uids if u > last_uid}, reverse=True)


def next_last_uid(last_uid, fresh):
    """Advance past everything seen this run, capped or not — never re-scan a backlog."""
    return max([last_uid, *fresh])


def is_skipped(from_addr, hints=None):
    """True when the sender matches a MAIL_SKIP_FROM entry (mail some other tool owns)."""
    fa = (from_addr or "").lower()
    return any(h in fa for h in (SKIP_FROM if hints is None else hints))


def has_pdf(msg):
    """True if any part is a PDF, by content type or filename."""
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        fname = (part.get_filename() or "").lower()
        if ctype == "application/pdf" or fname.endswith(".pdf"):
            return True
    return False


class _TextExtractor(HTMLParser):
    """Collects visible text from HTML, dropping script/style content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def strip_tags(html):
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return " ".join(parser.chunks)


def _part_text(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def body_snippet(msg):
    """First text/plain part, else a de-tagged text/html part, else NO_BODY."""
    html_text = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain":
            snippet = collapse(_part_text(part), SNIPPET_CHARS)
            if snippet:
                return snippet
        elif ctype == "text/html" and not html_text:
            html_text = _part_text(part)
    return collapse(strip_tags(html_text), SNIPPET_CHARS) or NO_BODY


def header_text(msg, name):
    """Decoded, single-line header value — empty string when the header is absent."""
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        value = str(make_header(decode_header(raw)))
    except (ValueError, LookupError, TypeError):
        value = str(raw)
    return collapse(value, HEADER_CHARS)


def digest_block(uid, msg, snippet):
    """One digest entry: uid line, the headers on one line, then the snippet."""
    fields = " | ".join(f"{name}: {header_text(msg, name)}" for name in HEADER_FIELDS)
    return f"=== uid {uid}\n{fields}\n{snippet}"


def render_digest(now, blocks, n_skipped, n_sched, headers_only=()):
    head = (f"Scanned: {now.isoformat(timespec='seconds')} | new: {len(blocks)}"
            f" | skipped over cap: {n_skipped} | skip-list skipped: {n_sched}")
    tail = ([f"=== over the cap, headers only ({len(headers_only)}): triage from sender + subject",
             *headers_only] if headers_only else [])
    return "\n".join([head, *blocks, *tail]) + "\n"


def parse_header_fetch(data):
    """A multi-uid header FETCH response -> {uid: email.message}."""
    out = {}
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            m = re.search(rb"UID (\d+)", bytes(item[0]))
            if m:
                out[int(m.group(1))] = email.message_from_bytes(bytes(item[1]))
    return out


def header_lines(uids, msgs):
    """One line per over-cap message in `uids` order; skip-list senders dropped.
    Returns (lines, n_skipped_by_list)."""
    lines, n_sched = [], 0
    for uid in uids:
        msg = msgs.get(uid)
        if msg is None:
            continue
        if is_skipped(msg.get("From", "")):
            n_sched += 1
            continue
        lines.append(f"uid {uid} | " + " | ".join(
            f"{name}: {header_text(msg, name)}" for name in ("From", "Subject", "Date")))
    return lines, n_sched


def fetch_headers(imap, uids):
    if not uids:
        return {}
    spec = "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
    typ, data = imap.uid("FETCH", ",".join(map(str, uids)), spec)
    if typ != "OK":
        raise RuntimeError(f"UID FETCH headers failed: {typ}")
    return parse_header_fetch(data)


def plan_messages(uids, fetch, cap=MAX_MESSAGES):
    """Digest blocks for the newest `cap` messages in `uids` (already newest-first).

    `fetch(uid)` returns (email.message, has_pdf). Skip-list mail is dropped for
    free — it never consumes a cap slot. Everything past the cap is counted but
    never fetched.

    Returns (blocks, n_skipped_over_cap, n_skipped_by_list).
    """
    blocks, n_sched = [], 0
    for i, uid in enumerate(uids):
        if len(blocks) >= cap:
            return blocks, len(uids) - i, n_sched
        msg, _ = fetch(uid)
        if is_skipped(msg.get("From", "")):
            n_sched += 1
            continue
        blocks.append(digest_block(uid, msg, body_snippet(msg)))
    return blocks, 0, n_sched


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_state(uidvalidity, last_uid):
    STATE_FILE.write_text(json.dumps(
        {"uidvalidity": int(uidvalidity), "last_uid": int(last_uid)},
        indent=2, sort_keys=True))


def _literals(data):
    """Concatenate the literal payloads of an IMAP FETCH response."""
    out = b""
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 \
                and isinstance(item[1], (bytes, bytearray)):
            out += bytes(item[1])
    return out


def _uid_search(imap, criterion):
    typ, data = imap.uid("SEARCH", None, criterion)
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH {criterion} failed: {typ}")
    return parse_uids(data)


def fetch_message(imap, uid):
    """(email.message, has_pdf) for one uid; oversized mail comes back headers-only."""
    typ, data = imap.uid("FETCH", str(uid), "(RFC822.SIZE)")
    if typ != "OK":
        raise RuntimeError(f"UID FETCH {uid} RFC822.SIZE failed: {typ}")
    if parse_size(data) > MAX_FETCH_BYTES:
        fields = " ".join(f.upper() for f in HEADER_FIELDS)
        spec = f"(BODY.PEEK[HEADER.FIELDS ({fields})])"
    else:
        spec = "(RFC822)"
    typ, data = imap.uid("FETCH", str(uid), spec)
    if typ != "OK":
        raise RuntimeError(f"UID FETCH {uid} {spec} failed: {typ}")
    msg = email.message_from_bytes(_literals(data))
    return msg, has_pdf(msg)


def scan(user, password, now):
    """Digest everything newer than the last scan; return scan.txt or None.

    None means nothing to read: first run / new UIDVALIDITY (state is seeded at
    the top of the mailbox instead of dumping history), or no new uids.
    """
    imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)  # EXAMINE: an RFC822 fetch can't mark mail read
        uidvalidity = parse_uidvalidity(imap.response("UIDVALIDITY")[1])
        if not uidvalidity:
            raise RuntimeError("SELECT INBOX returned no UIDVALIDITY")

        state = load_state()
        if needs_reset(state, uidvalidity):
            save_state(uidvalidity, max(_uid_search(imap, "ALL"), default=0))
            return None

        last_uid = int(state["last_uid"])
        fresh = new_uids(_uid_search(imap, f"UID {last_uid + 1}:*"), last_uid)
        if not fresh:
            return None
        blocks, n_skipped, n_sched = plan_messages(
            fresh, lambda uid: fetch_message(imap, uid))
        rest = fresh[len(fresh) - n_skipped:] if n_skipped else []
        listed = rest[:HEADERS_ONLY_MAX]
        got = fetch_headers(imap, listed)
        hdrs, n_sched_hdr = header_lines(listed, got)
        missing = sum(1 for u in listed if u not in got)  # a FETCH can skip uids; still count them
        n_skipped, n_sched = len(rest) - len(listed) + missing, n_sched + n_sched_hdr
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    SCAN.parent.mkdir(parents=True, exist_ok=True)
    SCAN.write_text(render_digest(now, blocks, n_skipped, n_sched, hdrs),
                    encoding="utf-8", errors="replace")
    save_state(uidvalidity, next_last_uid(last_uid, fresh))
    return SCAN


def main():
    user, password = config.get("MAIL_USER"), config.get("MAIL_PASSWORD")
    if not user or not password or not IMAP_HOST:
        print("NO_CREDENTIAL")  # MAIL_USER, MAIL_PASSWORD and MAIL_IMAP_HOST are all needed
        return 0
    try:
        result = scan(user, password, datetime.now(TZ))
    except Exception as e:
        print(f"mail_scan: error: {e}", file=sys.stderr)
        return 1
    if result is None:
        print("NO_NEW_MAIL")
        return 0
    print(f"OK {result}")
    return 0


def selftest():
    def mail(uid):
        return email.message_from_string(
            f"From: sender{uid} <s{uid}@example.com>\nTo: owner@example.com\n"
            f"Subject: note {uid}\nDate: Mon, 17 Aug 2026 01:00:00 -0700\n\n"
            f"body of {uid}\n"), False

    # 1) response parsing: bytes in, ints out
    assert parse_uids([b"1 2 30"]) == [1, 2, 30]
    assert parse_uids([b""]) == [] and parse_uids([None]) == [] and parse_uids(None) == []
    assert parse_uidvalidity([b"1234567"]) == 1234567
    assert parse_uidvalidity([b"* OK [UIDVALIDITY 42] ready"]) == 42
    assert parse_uidvalidity([None]) == 0 and parse_uidvalidity([]) == 0
    assert parse_size([b"3 (UID 9 RFC822.SIZE 250000)"]) == 250000
    assert parse_size([b"3 (UID 9)"]) == 0

    # 1a) new-uid filtering: 'n:*' hands back the highest uid even when it's old
    assert new_uids([100], 100) == []
    assert new_uids([100, 103, 101, 102], 100) == [103, 102, 101]
    assert new_uids([5, 5, 7], 4) == [7, 5]  # dedupes
    assert new_uids([], 0) == []

    # 1b) reset decision: first run, garbage state, or a re-generated mailbox
    assert needs_reset({}, 5) is True
    assert needs_reset({"uidvalidity": 5}, 5) is True
    assert needs_reset({"uidvalidity": "x", "last_uid": 1}, 5) is True
    assert needs_reset({"uidvalidity": 4, "last_uid": 10}, 5) is True
    assert needs_reset({"uidvalidity": 5, "last_uid": 10}, 5) is False
    assert needs_reset({"uidvalidity": "5", "last_uid": "10"}, 5) is False

    # 2) cap: newest MAX_MESSAGES digested, the rest counted and left unfetched
    fetched = []

    def counting_fetch(uid):
        fetched.append(uid)
        return mail(uid)

    fresh = new_uids(list(range(1, 31)), 0)
    blocks, n_skipped, n_sched = plan_messages(fresh, counting_fetch)
    assert len(blocks) == MAX_MESSAGES and (n_skipped, n_sched) == (5, 0)
    assert blocks[0].startswith("=== uid 30\n"), blocks[0]
    assert blocks[-1].startswith("=== uid 6\n"), blocks[-1]
    assert fetched == list(range(30, 5, -1)), fetched
    assert next_last_uid(0, fresh) == 30  # advances past the 5 skipped, not just the 25
    assert next_last_uid(99, []) == 99

    # 2a) skip-list mail is dropped for free — it never eats a cap slot
    global SKIP_FROM
    real_skip, SKIP_FROM = SKIP_FROM, ("@tool.example.org",)

    def sched_fetch(uid):
        if uid % 2 == 0:
            return email.message_from_string(
                "From: Robot <bot@tool.example.org>\nSubject: Weekly export\n"
                "\nsee attached\n"), True
        return mail(uid)

    blocks, n_skipped, n_sched = plan_messages(new_uids(list(range(1, 61)), 0), sched_fetch)
    assert len(blocks) == MAX_MESSAGES and (n_skipped, n_sched) == (10, 25)
    assert all(int(b.split()[2]) % 2 for b in blocks), blocks[0]

    # 2b) the skip matches listed senders ONLY: customer PDFs and "weekly" newsletters stay in
    assert is_skipped("Bot <BOT@Tool.Example.org>") is True
    assert is_skipped("customer@example.com") is False and is_skipped(None) is False
    assert is_skipped("x@tool.example.org", ()) is False  # an empty list skips nothing
    pdf_mail = EmailMessage()
    pdf_mail["From"] = "Customer <customer@example.com>"
    pdf_mail["Subject"] = "invoice attached"
    pdf_mail.set_content("hi")
    pdf_mail.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                            filename="invoice.pdf")
    assert has_pdf(pdf_mail) is True
    plain = EmailMessage()
    plain.set_content("hi")
    assert has_pdf(plain) is False
    blocks, _, n_sched = plan_messages([7], lambda uid: (pdf_mail, True))
    assert len(blocks) == 1 and n_sched == 0  # a customer PDF reaches the digest
    newsletter = email.message_from_string(
        "From: news@list.example.com\nSubject: Your weekly digest\n\nhi\n")
    blocks, _, n_sched = plan_messages([8], lambda uid: (newsletter, False))
    assert len(blocks) == 1 and n_sched == 0  # "weekly" subject alone is not boss mail

    # 3) snippet: plain part wins over html
    both = EmailMessage()
    both.set_content("plain   body\n\nsecond line")
    both.add_alternative("<html><body><p>html body</p></body></html>", subtype="html")
    assert body_snippet(both) == "plain body second line", body_snippet(both)

    # 3a) html-only falls back with tags/style stripped and entities decoded
    html_only = EmailMessage()
    html_only.set_content("<p>Hi   <b>Sam</b></p><style>p{color:red}</style>&amp; done",
                          subtype="html")
    assert body_snippet(html_only) == "Hi Sam & done", body_snippet(html_only)

    # 3b) whitespace collapse + truncation, unknown charset, and no body at all
    long_mail = EmailMessage()
    long_mail.set_content("x " * 3000)
    snip = body_snippet(long_mail)
    assert len(snip) == SNIPPET_CHARS and "  " not in snip, len(snip)
    assert collapse(" a\t\nb  c ", 100) == "a b c"
    assert collapse(None, 10) == "" and collapse("abcdef", 3) == "abc"
    weird = email.message_from_string(
        'Content-Type: text/plain; charset="unknown-8bit"\n\nhi  there\n')
    assert body_snippet(weird) == "hi there", body_snippet(weird)
    assert body_snippet(email.message_from_string("From: a@b.c\n\n")) == NO_BODY
    headers_only = email.message_from_string(
        "From: a@b.c\r\nTo: n@example.com\r\nSubject: huge\r\nDate: x\r\n\r\n")
    assert body_snippet(headers_only) == NO_BODY

    # 4) block formatting: missing header -> empty value, folded header stays on one line
    msg = email.message_from_string(
        "From: Boss <b@example.com>\nSubject: =?utf-8?q?caf=C3=A9?=\n"
        "Date: Mon, 17 Aug 2026 01:00:00 -0700\n\nhello\n")
    block = digest_block(4321, msg, body_snippet(msg))
    assert block.splitlines() == [
        "=== uid 4321",
        "From: Boss <b@example.com> | To:  | Subject: café | "
        "Date: Mon, 17 Aug 2026 01:00:00 -0700",
        "hello",
    ], block
    folded = email.message_from_string(
        "From: a@b.c\r\nSubject: line one\r\n  line two\r\n\r\nx\r\n")
    assert digest_block(1, folded, "x").splitlines()[1] == \
        "From: a@b.c | To:  | Subject: line one line two | Date: ", folded

    # 4a) digest header line + newest-first body
    pdt = timezone(timedelta(hours=-7))
    doc = render_digest(datetime(2026, 8, 17, 6, 30, tzinfo=pdt), [block], 3, 2)
    assert doc.splitlines()[0] == ("Scanned: 2026-08-17T06:30:00-07:00 | new: 1 | "
                                  "skipped over cap: 3 | skip-list skipped: 2"), doc
    assert doc.splitlines()[1] == "=== uid 4321" and doc.endswith("hello\n"), doc
    empty = render_digest(datetime(2026, 8, 17, 6, 30, tzinfo=pdt), [], 0, 0)
    assert empty == ("Scanned: 2026-08-17T06:30:00-07:00 | new: 0 | "
                     "skipped over cap: 0 | skip-list skipped: 0\n"), empty

    # 4b) over the cap: a multi-uid header FETCH becomes one line each, skip-list mail dropped
    data = [(b"7 (UID 107 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {60}",
             b"From: City Power <bill@power.example>\r\nSubject: Your bill is ready\r\n\r\n"), b")",
            (b"8 (UID 108 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {70}",
             b"From: Robot <bot@tool.example.org>\r\nSubject: Weekly export\r\n\r\n"), b")"]
    msgs = parse_header_fetch(data)
    assert sorted(msgs) == [107, 108]
    lines, n = header_lines([108, 107, 999], msgs)
    assert n == 1 and lines == ["uid 107 | From: City Power <bill@power.example> | Subject: Your bill is ready | Date: "], lines
    doc = render_digest(datetime(2026, 8, 17, 6, 30, tzinfo=pdt), [], 0, 1, lines)
    assert doc.splitlines()[1].startswith("=== over the cap, headers only (1)") and doc.endswith("Date: \n"), doc

    SKIP_FROM = real_skip
    print("PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    sys.exit(main())
