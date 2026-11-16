#!/usr/bin/env python3
"""Westly gcal — the assistant's Google calendars, written through a service account.

Stdlib + the already-installed `cryptography` (RS256 for the service-account JWT);
no google client library. Every event Westly writes carries a private
`westly_key`, so re-running anything updates in place instead of duplicating.

Calendars (IDs in westly.env): main = GCAL_MAIN (appointments, deadlines, anything the
owner asks to add) · shared = GCAL_SHARED (optional, shared with a household member).
Reminders are per-user in Google Calendar: the owner's per-calendar notification defaults
decide what their phone does, so events use useDefault and a deadline that needs a long
lead gets a separate all-day heads-up event (--heads-up DAYS).

  gcal.py add --title T --start "2026-10-15 21:00" [--end …] [--all-day] [--cal main]
              [--desc …] [--key …] [--heads-up DAYS]
  gcal.py list [--days 7] [--cal main|shared|all]
  gcal.py rm --key K [--cal main]
  gcal.py check                      credentials + access to each calendar
  gcal.py --selftest
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
import config

REPO = config.REPO
TZ = config.TZ
TOKEN_CACHE = REPO / ".gcal-token.json"
API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
CALS = {"main": "GCAL_MAIN", "shared": "GCAL_SHARED"}
NOT_CONFIGURED = "GCAL_NOT_CONFIGURED"


class NotConfigured(RuntimeError):
    pass


def key_file():
    p = Path(os.path.expanduser(os.environ.get("GCAL_KEY", "").strip() or "gcal-sa.json"))
    return p if p.is_absolute() else REPO / p


def cal_id(name):
    cid = os.environ.get(CALS.get(name, ""), "").strip()
    if not cid:
        raise NotConfigured(f"{CALS.get(name, name)} not set in westly.env")
    return cid


def configured(name="main"):
    return key_file().exists() and bool(os.environ.get(CALS.get(name, ""), "").strip())


# ---------- auth ----------

def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt_claims(email, now):
    return {"iss": email, "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}


def sign_jwt(sa, now):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(jwt_claims(sa["client_email"], now)).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def token():
    now = int(time.time())
    try:
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached["exp"] - 120 > now:
            return cached["token"]
    except (OSError, ValueError, KeyError):
        pass
    kf = key_file()
    if not kf.exists():
        raise NotConfigured(f"service-account key missing: {kf}")
    sa = json.loads(kf.read_text())
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": sign_jwt(sa, now)}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=20) as r:
        got = json.loads(r.read())
    TOKEN_CACHE.touch(mode=0o600, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps({"token": got["access_token"],
                                       "exp": now + int(got.get("expires_in", 3600))}))
    return got["access_token"]


def call(method, path, params=None, body=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token()}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Google Calendar {method} {path}: HTTP {e.code} {detail}") from None
    return json.loads(raw) if raw else {}


def _events_path(cal):
    return f"/calendars/{urllib.parse.quote(cal_id(cal), safe='')}/events"


# ---------- event building (pure) ----------

def parse_when(text):
    """'2026-10-15', '2026-10-15 21:00', or full ISO -> date | aware datetime."""
    text = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    dt = datetime.fromisoformat(text.replace(" ", "T", 1))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def default_key(title, start):
    day = start.isoformat()[:10]
    return f"{slug(title)}:{day}"


def build_event(title, start, end=None, desc="", key=None):
    """Google event body. A date start means all-day (end exclusive); a datetime
    start without an end lasts an hour."""
    if isinstance(start, datetime):
        end = end or start + timedelta(hours=1)
        when = {"start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
                "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)}}
    else:
        end = end if isinstance(end, date) and not isinstance(end, datetime) else None
        end = end or start + timedelta(days=1)
        when = {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}
    return {"summary": title, "description": desc, **when,
            "reminders": {"useDefault": True},
            "extendedProperties": {"private": {"westly_key": key or default_key(title, start)}}}


def event_from_input(d):
    """App form -> Google event body, or ValueError with a plain message.
    d: title, date YYYY-MM-DD, all_day, start/end HH:MM (timed), end_date (optional), desc."""
    title = " ".join(str(d.get("title") or "").split())
    if not title or len(title) > 200:
        raise ValueError("Give it a title (200 characters max).")
    desc = str(d.get("desc") or "").strip()
    if len(desc) > 2000:
        raise ValueError("Notes are too long (2000 characters max).")
    try:
        day = date.fromisoformat(str(d.get("date") or ""))
        last = date.fromisoformat(d["end_date"]) if d.get("end_date") else None
    except ValueError:
        raise ValueError("Pick a date.") from None
    if last and not day <= last <= day + timedelta(days=60):
        raise ValueError("The end date has to be on or after the start, within 60 days.")
    if d.get("all_day"):
        return build_event(title, day, (last or day) + timedelta(days=1), desc)
    try:
        t0 = datetime.strptime(str(d.get("start") or ""), "%H:%M").time()
        t1 = datetime.strptime(str(d["end"]), "%H:%M").time() if d.get("end") else None
    except ValueError:
        raise ValueError("Times look like 18:30.") from None
    start = datetime.combine(day, t0, TZ)
    end = datetime.combine(last or day, t1, TZ) if t1 else start + timedelta(hours=1)
    if end <= start:
        if last:
            raise ValueError("It has to end after it starts.")
        end += timedelta(days=1)  # 22:00-01:00 runs past midnight
    return build_event(title, start, end, desc)


def same_event(existing, wanted):
    """True when an update would change nothing the owner can see."""
    def pick(e):
        return (e.get("summary", ""), e.get("description", "") or "",
                e.get("start", {}).get("date") or _norm_dt(e.get("start", {}).get("dateTime")),
                e.get("end", {}).get("date") or _norm_dt(e.get("end", {}).get("dateTime")))
    return pick(existing) == pick(wanted)


def _norm_dt(text):
    return datetime.fromisoformat(text).astimezone(TZ).isoformat() if text else None


# ---------- operations ----------

def find(cal, key):
    got = call("GET", _events_path(cal), {"privateExtendedProperty": f"westly_key={key}",
                                         "maxResults": 5})
    return got.get("items", [])


def upsert(cal, body):
    """'created' | 'updated' | 'unchanged'. Extra copies of the same key are removed."""
    key = body["extendedProperties"]["private"]["westly_key"]
    hits = find(cal, key)
    for extra in hits[1:]:
        call("DELETE", f"{_events_path(cal)}/{extra['id']}")
    if not hits:
        call("POST", _events_path(cal), body=body)
        return "created"
    if same_event(hits[0], body):
        return "unchanged"
    call("PATCH", f"{_events_path(cal)}/{hits[0]['id']}", body=body)
    return "updated"


def remove(cal, key):
    hits = find(cal, key)
    for ev in hits:
        call("DELETE", f"{_events_path(cal)}/{ev['id']}")
    return len(hits)


def get_event(cal, eid):
    return call("GET", f"{_events_path(cal)}/{urllib.parse.quote(eid, safe='')}")


def save_event(cal, body, eid=None):
    """Create (no eid) or replace the visible fields of one event by its Google id. Returns the event."""
    if eid is None:
        return call("POST", _events_path(cal), body=body)
    return call("PATCH", f"{_events_path(cal)}/{urllib.parse.quote(eid, safe='')}", body=body)


def delete_event(cal, eid):
    call("DELETE", f"{_events_path(cal)}/{urllib.parse.quote(eid, safe='')}")


EVENT_ID = re.compile(r"[A-Za-z0-9_@.-]{1,1024}")


def save_input(cal, data):
    """Create or edit (data['id']) one event from app-form input. Returns {id, title}; ValueError for bad input."""
    body = event_from_input(data)
    eid = data.get("id")
    if eid is not None:
        if not EVENT_ID.fullmatch(str(eid)):
            raise ValueError("bad id")
        get_event(cal, str(eid))  # 404s unless the event is on this calendar
        body.pop("extendedProperties")  # an edit keeps the key Westly may know it by
        for k in ("start", "end"):  # PATCH merges: clear the other form or Google rejects timed<->all-day
            body[k] = {**{"date": None, "dateTime": None, "timeZone": None}, **body[k]}
    got = save_event(cal, body, None if eid is None else str(eid))
    return {"id": got.get("id"), "title": got.get("summary")}


def delete_input(cal, eid):
    if not EVENT_ID.fullmatch(str(eid or "")):
        raise ValueError("bad id")
    get_event(cal, str(eid))
    delete_event(cal, str(eid))
    return {"id": eid}


def list_events(cal, days=7, start=None):
    start = start or datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    got = call("GET", _events_path(cal), {
        "timeMin": start.isoformat(), "timeMax": (start + timedelta(days=days)).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 250})
    out = []
    for e in got.get("items", []):
        s, en = e.get("start", {}), e.get("end", {})
        out.append({"id": e.get("id"), "title": e.get("summary", "(no title)"), "desc": e.get("description") or "",
                    "start": s.get("dateTime") or s.get("date"),
                    "end": en.get("dateTime") or en.get("date"),
                    "all_day": "date" in s, "calendar": cal,
                    "key": e.get("extendedProperties", {}).get("private", {}).get("westly_key")})
    return out


# ---------- CLI ----------

def main(argv):
    ap = argparse.ArgumentParser(prog="gcal.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--start", required=True)
    a.add_argument("--end")
    a.add_argument("--all-day", action="store_true")
    a.add_argument("--cal", default="main", choices=CALS)
    a.add_argument("--desc", default="")
    a.add_argument("--key")
    a.add_argument("--heads-up", type=int, metavar="DAYS")
    ls = sub.add_parser("list")
    ls.add_argument("--days", type=int, default=7)
    ls.add_argument("--cal", default="all", choices=[*CALS, "all"])
    rm = sub.add_parser("rm")
    rm.add_argument("--key", required=True)
    rm.add_argument("--cal", default="main", choices=CALS)
    sub.add_parser("check")
    args = ap.parse_args(argv)
    if not key_file().exists():
        print(f"{NOT_CONFIGURED}: no service-account key at {key_file()} — see "
              "prompts/gcal-setup.md", file=sys.stderr)
        return 2
    if args.cmd == "check":
        ok = True
        for name in CALS:
            if name == "shared" and not configured(name):
                print(f"{name}: not set up ({CALS[name]}) — optional")
                continue
            try:
                meta = call("GET", f"/calendars/{urllib.parse.quote(cal_id(name), safe='')}")
                print(f"{name}: ok — {meta.get('summary')}")
            except Exception as e:
                ok = False
                print(f"{name}: FAIL — {e}")
        return 0 if ok else 1
    if args.cmd == "list":
        names = list(CALS) if args.cal == "all" else [args.cal]
        evs = [e for n in names if configured(n) for e in list_events(n, args.days)]
        evs.sort(key=lambda e: e["start"])
        for e in evs:
            print(f"{e['start'][:16]:16}  [{e['calendar']}] {e['title']}  key={e['key']}")
        return 0
    if args.cmd == "rm":
        print(f"removed {remove(args.cal, args.key)}")
        return 0
    start = parse_when(args.start)
    if args.all_day and isinstance(start, datetime):
        start = start.date()
    end = parse_when(args.end) if args.end else None
    body = build_event(args.title, start, end, args.desc, args.key)
    key = body["extendedProperties"]["private"]["westly_key"]
    print(f"{upsert(args.cal, body)}: {args.title} @ {start}  key={key}")
    if args.heads_up:
        day = start.date() if isinstance(start, datetime) else start
        early = day - timedelta(days=args.heads_up)
        hb = build_event(f"Heads-up: {args.title} ({day:%a %-m/%-d})", early,
                         desc=args.desc, key=f"{key}:heads-up")
        print(f"{upsert(args.cal, hb)}: heads-up @ {early}")
    return 0


def selftest():
    c = jwt_claims("sa@x.iam.gserviceaccount.com", 1000)
    assert c == {"iss": "sa@x.iam.gserviceaccount.com", "scope": SCOPE, "aud": TOKEN_URL,
                 "iat": 1000, "exp": 4600}, c
    # a real RS256 signature over a throwaway key verifies
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    jwt = sign_jwt({"client_email": "sa@x", "private_key": pem}, 1000)
    head, body, sig = jwt.split(".")
    k.public_key().verify(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)),
                          f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())

    assert parse_when("2026-10-21") == date(2026, 10, 21)
    t = parse_when("2026-10-15 21:00")
    assert t.tzinfo is not None and t.hour == 21, t
    assert default_key("Internet outage!", t) == "internet-outage:2026-10-15"

    ev = build_event("Dentist", t)
    assert ev["end"]["dateTime"].startswith("2026-10-15T22:00"), ev
    assert ev["reminders"] == {"useDefault": True}
    ad = build_event("Registration due", date(2026, 10, 21), key="reg")
    assert ad["start"] == {"date": "2026-10-21"} and ad["end"] == {"date": "2026-10-22"}, ad
    assert ad["extendedProperties"]["private"]["westly_key"] == "reg"
    # the API echoes times in its own offset form; same instant must compare equal
    echoed = dict(ev, start={"dateTime": "2026-10-16T04:00:00Z"},
                  end={"dateTime": "2026-10-16T05:00:00Z"})
    assert same_event(echoed, ev)
    assert not same_event(dict(echoed, summary="Dentist (moved)"), ev)

    f = event_from_input({"title": "  Dinner  with   friends ", "date": "2026-11-02", "start": "22:00", "end": "01:00"})
    assert f["summary"] == "Dinner with friends" and f["end"]["dateTime"].startswith("2026-11-03T01:00"), f
    f = event_from_input({"title": "Trip", "date": "2026-11-02", "end_date": "2026-11-04", "all_day": True})
    assert f["start"] == {"date": "2026-11-02"} and f["end"] == {"date": "2026-11-05"}, f
    assert event_from_input({"title": "X", "date": "2026-11-02", "start": "09:00"})["end"]["dateTime"].startswith("2026-11-02T10:00")
    for bad in ({"date": "2026-11-02"}, {"title": "X"}, {"title": "X", "date": "2026-11-02", "start": "9am"},
                {"title": "X", "date": "2026-11-02", "end_date": "2026-11-01", "all_day": True},
                {"title": "X", "date": "2026-11-02", "end_date": "2026-11-02", "start": "10:00", "end": "09:00"}):
        try:
            event_from_input(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("PASS")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    try:
        sys.exit(main(sys.argv[1:]))
    except NotConfigured as e:
        sys.exit(f"{NOT_CONFIGURED}: {e}")
