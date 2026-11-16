"""Westly PDA — the phone app's backend, served on westly-bot's own event loop.

Listens on a 0600 Unix socket only; `tailscale serve --https=8443 unix:<socket>`
publishes it tailnet-only and stamps Tailscale-User-Login, which every request must
carry as the configured owner (PDA_LOGIN). A socket instead of a TCP port, so nothing
else on the box (a host-network container, say) can reach it and forge that header —
/api/chat drives a shell-capable Claude.

The frontend lives in pda/ (one static index.html). Data comes from the same files
the briefs read: outbox/sent (the feed), memory/needs-you.md, memory/tasks.md, the
optional Google calendar (gcal.py) and the optional Open-Meteo forecast.
"""
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiohttp.web import ContentCoding

import config

REPO = config.REPO
STATIC = REPO / "pda"
SOCKET = REPO / ".pda.sock"
SENT = REPO / "memory" / "outbox" / "sent"
NEEDS = REPO / "memory" / "needs-you.md"
ARCHIVE = REPO / "memory" / "needs-archive.md"
NEEDS_LOCK = REPO / "memory" / ".needs.lock"
TASKS = REPO / "memory" / "tasks.md"
JOURNAL = REPO / "memory" / "journal"
HEALTH = REPO / ".health.json"
CHAT_LOG = REPO / ".pda-chat.json"
TZ = config.TZ
CALS = ("main", "shared")  # gcal.py calendar keys the app reads and writes
FEED_PAGE = 30
CHAT_KEEP = 200
STATIC_FILES = {"manifest.json", "icon.svg", "icon-180.png", "icon-512.png", "sw.js"}
UPSTREAM_TIMEOUT = 4    # seconds; how long Today waits on a cold calendar or forecast
TODAY_TTL = 90          # an events/forecast answer is served as fresh this long
TODAY_WAIT_AFTER = 300  # older than this, a read waits (bounded) for the refresh instead of serving stale
TODAY_STALE_MAX = 1800  # past this a failing source reads as missing rather than wrong
FAIL_RETRY = 30         # after a failed fetch, reads don't start another for this long
SENT_CACHE_MAX_AGE = 30
COMPRESS_MIN = 1024     # bytes; smaller JSON isn't worth gzip's overhead
DAY_BACK, DAY_AHEAD, DAY_SPAN_MAX = 60, 180, 14  # /api/day window limits (days from today; span)
DAY_SOURCES_MAX = 16
CAL_SPAN_MAX = 42       # /api/cal: a month grid is 6 weeks
ARCHIVE_HEAD = ("# Needs you — archive\n\nItems dismissed from the app without an answer. "
                "Restore from the app, or delete.\n")
BUILD_MARK = b"__WESTLY_BUILD__"    # the page compares its own ETag (written here) with the server's to spot an update
CONFIG_MARK = b"__WESTLY_CONFIG__"  # replaced with the app's settings as JSON
NAME_MARK = b"{{ASSISTANT}}"        # the assistant's name; config restricts it to a plain charset

log = logging.getLogger("westly.pda")

# filename prefix -> (kind, label). Collision archives carry a '<epoch>-' prefix.
KINDS = {"brief": "Morning brief", "evening": "Evening review", "mail": "Mail",
         "health": "Health", "attn": "Alert", "finance": "Finance",
         "weekly": "Week ahead", "docs": "Docs", "draft": "Draft"}


# ---------- pure parsing (selftested) ----------

def feed_kind(name):
    base = re.sub(r"^\d+-", "", name)
    prefix = re.split(r"[-_.]", base, maxsplit=1)[0]
    return (prefix, KINDS[prefix]) if prefix in KINDS else ("other", config.ASSISTANT_NAME)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


NEED_HEAD = re.compile(r"^## (.+?)\s*\((added \d{4}-\d{2}-\d{2}[^)]*)\)\s*$")


def parse_needs(text):
    """needs-you.md -> [{id, question, added, by, context, options}].
    Entry: `## <question> (added YYYY-MM-DD[, by YYYY-MM-DD])`, context lines,
    optional `options: A | B | C`."""
    items, cur = [], None
    for line in text.splitlines():
        m = NEED_HEAD.match(line)
        if m:
            meta = m.group(2)
            added = re.search(r"added (\d{4}-\d{2}-\d{2})", meta)
            by = re.search(r"by (\d{4}-\d{2}-\d{2})", meta)
            cur = {"id": slug(m.group(1)), "question": m.group(1).strip(),
                   "added": added.group(1) if added else None,
                   "by": by.group(1) if by else None, "context": "", "options": []}
            items.append(cur)
        elif line.startswith("#"):
            cur = None
        elif cur is not None:
            if line.lower().startswith("options:"):
                cur["options"] = [o.strip() for o in line[8:].split("|") if o.strip()]
            elif line.strip():
                cur["context"] = (cur["context"] + "\n" + line.strip()).strip()
    return items


def upsert_need(text, question, context="", options=(), by=None, today=None, match=None):
    """needs-you.md text with an entry added, or replaced in place when a heading
    starts with `match` (a stable prefix like 'Backups:'). Keeps the original
    added date on replace so the stale-item sweep still sees its true age."""
    today = today or datetime.now(TZ).date().isoformat()
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.startswith("## ") and match and line[3:].startswith(match):
            start = i
        elif start is not None and line.startswith("#"):
            end = i
            break
    added = today
    if start is not None:
        m = re.search(r"added (\d{4}-\d{2}-\d{2})", lines[start])
        added = m.group(1) if m else today
    meta = f"added {added}" + (f", by {by}" if by else "")
    entry = [f"## {question} ({meta})"] + ([context] if context else [])
    if options:
        entry.append("options: " + " | ".join(options))
    entry.append("")
    if start is None:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + "\n".join(entry)
    end = len(lines) if end is None else end
    return "\n".join(lines[:start] + entry + lines[end:]).rstrip("\n") + "\n"


def need_span(lines, nid):
    """(start, end) line range of the entry whose slug is `nid`, or None. An entry runs
    from its heading to the next line starting with '#'."""
    for i, line in enumerate(lines):
        m = NEED_HEAD.match(line)
        if m and slug(m.group(1)) == nid:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("#")), len(lines))
            return i, end
    return None


def take_need(text, nid):
    """(entry block or None, text without it)."""
    lines = text.splitlines()
    span = need_span(lines, nid)
    if span is None:
        return None, text
    a, b = span
    block = "\n".join(lines[a:b]).strip("\n")
    rest = "\n".join(lines[:a] + lines[b:])
    rest = re.sub(r"\n{3,}", "\n\n", rest).rstrip("\n") + "\n"
    return block, rest


def stamp_archived(block, day):
    """Heading meta gains ', archived YYYY-MM-DD' (an older stamp is replaced)."""
    head, *body = block.split("\n")
    head = re.sub(r",\s*archived \d{4}-\d{2}-\d{2}", "", head)
    head = re.sub(r"\)\s*$", f", archived {day})", head)
    return "\n".join([head] + body)


def unstamp_archived(block):
    head, *body = block.split("\n")
    return "\n".join([re.sub(r",\s*archived \d{4}-\d{2}-\d{2}", "", head)] + body)


def append_block(text, block, header):
    body = (text if text.strip() else header).rstrip("\n")
    return body + "\n\n" + block.strip("\n") + "\n"


def archived_on(text):
    """slug -> 'YYYY-MM-DD' archived stamp for each entry in the archive file."""
    out = {}
    for line in text.splitlines():
        m = NEED_HEAD.match(line)
        if m:
            d = re.search(r"archived (\d{4}-\d{2}-\d{2})", m.group(2))
            out[slug(m.group(1))] = d.group(1) if d else None
    return out


TASK_HEAD = re.compile(r"^## (.+?)\s*\(added (\d{4}-\d{2}-\d{2})\)\s*$")


def parse_tasks(text):
    items, cur = [], None
    for line in text.splitlines():
        m = TASK_HEAD.match(line)
        if m:
            cur = {"title": m.group(1), "added": m.group(2), "body": ""}
            items.append(cur)
        elif line.startswith("#"):
            cur = None
        elif cur is not None and line.strip():
            cur["body"] = (cur["body"] + "\n" + line.strip()).strip()
    return {"items": items}


def health_summary(state):
    problems = list((state or {}).get("reported", {}).values())
    return {"ok": not problems, "problems": problems}


def feedback_line(emoji, text, now):
    snippet = " ".join(text.split())[:80]
    return f"- {now:%Y-%m-%d %H:%M} feedback: {emoji} on \"{snippet}\" (PDA)\n"


def day_rows(start, days):
    return [{"date": (start + timedelta(days=i)).isoformat()} for i in range(days)]


def app_settings():
    """What the page needs to know about this install; injected at CONFIG_MARK."""
    import gcal
    return {"assistant": config.ASSISTANT_NAME, "owner": config.OWNER_NAME,
            "weather": config.weather_on(), "place": config.WEATHER_PLACE,
            "calendar": gcal.configured("main"),
            "cals": {"main": config.get("GCAL_MAIN_LABEL", "Me")[:24],
                     "shared": config.get("GCAL_SHARED_LABEL", "Shared")[:24]}}


def page_json(obj):
    """JSON safe to paste inside a <script> element."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_page(raw, settings, tag):
    return (raw.replace(BUILD_MARK, tag.encode(), 1)
            .replace(CONFIG_MARK, page_json(settings).encode(), 1)
            .replace(NAME_MARK, settings["assistant"].encode()))


# ---------- data sources ----------

def _get_json(url, headers, timeout=UPSTREAM_TIMEOUT):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def events():
    """Events on the app's calendars for the next 8 days."""
    return cal_fetch(CALS, datetime.now(TZ).date(), 8)


# ---------- weather (Open-Meteo forecast + optional US NWS alerts, both keyless) ----------

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
       82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms", 96: "Thunderstorms",
       99: "Thunderstorms"}


def shape_forecast(f, alerts, metric=False, nws=True):
    """Open-Meteo + NWS JSON -> the app's weather sheet payload (pure; selftested).
    alerts=None means the alert lookup failed; {"features": []} means there are none."""
    cur, hr, dy = f.get("current") or {}, f.get("hourly") or {}, f.get("daily") or {}
    hours = [{"t": t, "temp": hr["temperature_2m"][i], "pop": (hr.get("precipitation_probability") or [None] * 99)[i],
              "code": hr["weather_code"][i], "day": bool((hr.get("is_day") or [1] * 99)[i])}
             for i, t in enumerate(hr.get("time", []))]
    days = [{"date": d, "hi": dy["temperature_2m_max"][i], "lo": dy["temperature_2m_min"][i],
             "pop": (dy.get("precipitation_probability_max") or [None] * 99)[i], "code": dy["weather_code"][i],
             "sunrise": (dy.get("sunrise") or [None] * 99)[i], "sunset": (dy.get("sunset") or [None] * 99)[i],
             "uv": (dy.get("uv_index_max") or [None] * 99)[i], "wind": (dy.get("wind_speed_10m_max") or [None] * 99)[i]}
            for i, d in enumerate(dy.get("time", []))]
    code = cur.get("weather_code")
    return {
        "current": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                    "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                    "gust": cur.get("wind_gusts_10m"), "dir": cur.get("wind_direction_10m"),
                    "code": code, "text": WMO.get(code, "—"), "day": bool(cur.get("is_day", 1)),
                    "time": cur.get("time")},
        "hours": hours, "days": days,
        "alerts": None if alerts is None else [
            {"event": p.get("event"), "headline": p.get("headline"), "severity": p.get("severity"),
             "ends": p.get("ends") or p.get("expires"), "desc": (p.get("description") or "")[:600]}
            for p in (a.get("properties") or {} for a in alerts.get("features", []))],
        "units": {"temp": "°C" if metric else "°F", "wind": "km/h" if metric else "mph"},
        "source": "Open-Meteo" + (" · alerts: National Weather Service" if nws else ""),
    }


def forecast():
    """None when WEATHER_LAT/LON are not set; raises on an upstream failure."""
    if not config.weather_on():
        return None
    lat, lon, metric = config.WEATHER_LAT, config.WEATHER_LON, config.WEATHER_METRIC
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": config.TZ_NAME,
        "temperature_unit": "celsius" if metric else "fahrenheit", "wind_speed_unit": "kmh" if metric else "mph",
        "precipitation_unit": "mm" if metric else "inch", "forecast_days": 7, "forecast_hours": 24,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,"
                   "wind_gusts_10m,wind_direction_10m,weather_code,is_day",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "sunrise,sunset,uv_index_max,wind_speed_10m_max"})
    f = _get_json(f"https://api.open-meteo.com/v1/forecast?{q}", {})
    alerts = {"features": []}
    if config.WEATHER_NWS_ALERTS:
        try:  # alerts are a bonus: their outage must not hide the forecast
            alerts = _get_json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
                               {"Accept": "application/geo+json"})
        except Exception as e:
            log.warning("nws alerts: %s", e)
            alerts = None
    return shape_forecast(f, alerts, metric, config.WEATHER_NWS_ALERTS)


class Source:
    """One cached upstream: TTL cache, stale-while-revalidate, one fetch in flight.
    A cold or invalidated read waits for that fetch, at most UPSTREAM_TIMEOUT after it began."""

    def __init__(self, name, fetch, default=None):
        self.name, self.fetch, self.default = name, fetch, default
        self.value, self.ok_at = default, None  # ok_at: monotonic time of the last good fetch
        self.dirty = True  # cold or invalidated: the held value must not be served unchallenged
        self.failed_at = float("-inf")
        self.gen = 0
        self.task = None
        self.started = 0.0

    def invalidate(self):
        self.gen += 1
        self.dirty = True
        self.started = time.monotonic()
        self._kick()

    def _kick(self):
        if self.task is None:
            self.started = time.monotonic()
            self.task = asyncio.get_running_loop().create_task(self._refresh())

    async def _refresh(self):
        try:
            while True:
                gen = self.gen
                try:
                    value = await asyncio.to_thread(self.fetch)
                except Exception as e:
                    log.warning("%s: %s", self.name, e)
                    self.failed_at = time.monotonic()
                    return
                self.value, self.ok_at = value, time.monotonic()
                if gen == self.gen:
                    self.dirty = False
                    return
                self.started = time.monotonic()  # invalidated mid-fetch: that answer may predate it
        finally:
            self.task = None

    async def get(self):
        now = time.monotonic()
        age = float("inf") if self.ok_at is None else now - self.ok_at
        if not self.dirty and age < TODAY_TTL:
            return self.value
        if self.task is None and now - self.failed_at >= FAIL_RETRY:
            self._kick()
        cold = self.dirty or age > TODAY_WAIT_AFTER
        left = self.started + UPSTREAM_TIMEOUT - now
        if cold and self.task is not None and left > 0:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), left)
            except TimeoutError:
                pass
        if self.ok_at is None or time.monotonic() - self.ok_at > TODAY_STALE_MAX:
            return self.default
        return self.value


_sent_cache = (None, 0.0, ())  # (dir key, monotonic time, files)


def sent_files():
    """(mtime, path) of feed items, newest first; suppressed dup- copies excluded.
    Cached on the dir's mtime + entry count (file_outbox renames files in), max SENT_CACHE_MAX_AGE."""
    global _sent_cache
    try:
        key = (SENT.stat().st_mtime_ns, len(os.listdir(SENT)))  # taken before listing, never after
    except OSError:
        return ()
    k, at, files = _sent_cache
    if k == key and time.monotonic() - at < SENT_CACHE_MAX_AGE:
        return files
    out = []
    for p in SENT.glob("*.md"):
        if p.name.startswith("dup-") or "-dup-" in p.name:
            continue
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            continue
    out.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    files = tuple(out)
    _sent_cache = (key, time.monotonic(), files)
    return files


def feed_item(mtime, path):
    kind, label = feed_kind(path.name)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    return {"id": path.name, "kind": kind, "source": label, "at": int(mtime), "text": text}


def today_payload(now, events):
    """Assemble /api/today; the calendar lookup comes from the caller's Source cache."""
    try:
        hstate = json.loads(HEALTH.read_text())
    except (OSError, ValueError):
        hstate = {}
    return {
        "date": now.date().isoformat(),
        "days": day_rows(now.date(), 7),
        "events": events,
        "health": health_summary(hstate),
        "needs_count": len(parse_needs(_read(NEEDS))),
    }


@contextlib.contextmanager
def needs_lock():
    """Serialises the app's needs-you.md / archive edits (threads and processes alike).
    Writers outside the app may replace the file atomically, so every edit re-reads
    the file inside the lock and writes via tmp + rename."""
    NEEDS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(NEEDS_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path, text):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def dismiss_need(nid, today=None):
    """needs-you.md -> archive. True when the entry was found and moved."""
    today = today or datetime.now(TZ).date().isoformat()
    with needs_lock():
        block, rest = take_need(_read(NEEDS), nid)
        if block is None:
            return False
        # archive first: a crash between the writes leaves a duplicate, never a lost entry
        arch = _read(ARCHIVE)
        _, arch = take_need(arch, nid) if arch else (None, arch)
        _atomic_write(ARCHIVE, append_block(arch, stamp_archived(block, today), ARCHIVE_HEAD))
        _atomic_write(NEEDS, rest)
        return True


def restore_need(nid):
    """archive -> needs-you.md. True when found."""
    with needs_lock():
        block, rest = take_need(_read(ARCHIVE), nid)
        if block is None:
            return False
        needs = _read(NEEDS)
        if need_span(needs.splitlines(), nid) is None:
            _atomic_write(NEEDS, append_block(needs, unstamp_archived(block), "# Needs you\n"))
        _atomic_write(ARCHIVE, rest)
        return True


def delete_archived(nid=None):
    """Drop one archived entry (or all when nid is None). Returns how many went."""
    with needs_lock():
        text = _read(ARCHIVE)
        if nid is None:
            n = len(parse_needs(text))
            if n:
                _atomic_write(ARCHIVE, ARCHIVE_HEAD)
            return n
        block, rest = take_need(text, nid)
        if block is None:
            return 0
        _atomic_write(ARCHIVE, rest)
        return 1


def archive_items():
    text = _read(ARCHIVE)
    stamps = archived_on(text)
    items = parse_needs(text)
    for it in items:
        it["archived"] = stamps.get(it["id"])
    return items


def day_payload(start, days, events):
    return {"start": start.isoformat(), "days": day_rows(start, days), "events": events}


def _read(path):
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# ---------- calendar (optional: gcal.py + a Google service account) ----------

def cal_fetch(cals, start, days):
    """Events from each configured calendar in `cals`, merged by start. Raises on an upstream failure."""
    import gcal
    t0 = datetime(start.year, start.month, start.day, tzinfo=TZ)
    evs = [e for c in cals if gcal.configured(c) for e in gcal.list_events(c, days=days, start=t0)]
    evs.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    return evs


def add_cal_routes(routes, readable, writable, on_change=None):
    """GET /api/cal?start&days, POST /api/cal/save, POST /api/cal/delete over the given calendars."""
    sources = {}

    def invalidate():
        for src in sources.values():
            src.dirty = True
        if on_change:
            on_change()

    @routes.get("/api/cal")
    async def cal_get(request):
        import gcal
        today = datetime.now(TZ).date()
        try:
            start = date.fromisoformat(request.query.get("start", ""))
            days = int(request.query.get("days", "14"))
        except ValueError:
            return web.json_response({"error": "start=YYYY-MM-DD&days=N"}, status=400)
        if not (-DAY_BACK <= (start - today).days <= DAY_AHEAD) or not 1 <= days <= CAL_SPAN_MAX:
            return web.json_response({"error": "out of range"}, status=400)
        key = (start, days)
        src = sources.get(key)
        if src is None:
            src = sources[key] = Source(f"cal {start}+{days}", lambda: cal_fetch(readable, start, days), default=None)
            while len(sources) > DAY_SOURCES_MAX:
                old = next(iter(sources))
                if sources[old].task is not None:
                    break
                del sources[old]
        evs = await src.get()
        cals = {c: gcal.configured(c) for c in readable}
        return web.json_response({"start": start.isoformat(), "days": days, "ok": evs is not None,
                                  "events": evs or [], "calendars": cals,
                                  "writable": [c for c in writable if cals.get(c)]})

    async def _write(request, fn):
        import gcal
        data = await _body(request)
        cal = data.get("cal")
        if cal not in writable or not gcal.configured(cal):
            return web.json_response({"error": "That calendar can't be changed here."}, status=400)
        try:
            out = await asyncio.to_thread(fn, cal, data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            log.warning("cal write: %s", e)
            gone = "HTTP 404" in str(e) or "HTTP 410" in str(e)
            return web.json_response({"error": "That event isn't there any more." if gone else
                                      "Google Calendar didn't take it. Try again in a minute."}, status=404 if gone else 502)
        invalidate()
        return web.json_response({"ok": True, **(out or {})})

    @routes.post("/api/cal/save")
    async def cal_save_route(request):
        import gcal
        return await _write(request, gcal.save_input)

    @routes.post("/api/cal/delete")
    async def cal_delete_route(request):
        import gcal
        return await _write(request, lambda cal, d: gcal.delete_input(cal, d.get("id")))

    return invalidate


# ---------- docs shelf (files and links for the owner's phone; bin/westly-doc fills it) ----------

DOCS = REPO / "docs-shelf"           # gitignored: whatever lands here stays private
DOCS_INDEX = DOCS / "index.json"
DOCS_LOCK = DOCS / ".lock"
DOC_MAX = 50 * 1024 * 1024
DOC_TRASH_DAYS = 7                   # a removed doc can be restored this long, then its file goes
DOC_KINDS = {"pdf": "pdf", "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image",
             "heic": "image", "svg": "image", "html": "page", "htm": "page", "md": "text", "txt": "text",
             "csv": "text", "json": "text", "log": "text"}
DOC_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "webp": "image/webp", "heic": "image/heic", "svg": "image/svg+xml",
             "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8",
             "md": "text/plain; charset=utf-8", "txt": "text/plain; charset=utf-8",
             "csv": "text/plain; charset=utf-8", "json": "text/plain; charset=utf-8",
             "log": "text/plain; charset=utf-8"}
DOC_ID = re.compile(r"[0-9a-f]{10}")


@contextlib.contextmanager
def docs_lock():
    DOCS.mkdir(parents=True, exist_ok=True)
    with open(DOCS_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _docs_read():
    try:
        got = json.loads(DOCS_INDEX.read_text())
        return [d for d in got if isinstance(d, dict) and DOC_ID.fullmatch(str(d.get("id", "")))]
    except (OSError, ValueError):
        return []


def doc_ext(name):
    ext = Path(name).suffix.lower().lstrip(".")
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else ""


def doc_kind(ext):
    return DOC_KINDS.get(ext, "file")


def doc_add(title, source, data=None, name="", url="", now=None):
    """Put one file (bytes + original name) or one https link on the shelf; returns its entry."""
    now = time.time() if now is None else now
    if url:
        if not re.fullmatch(r"https?://\S{3,2000}", url):
            raise ValueError("links must start with http:// or https://")
        entry = {"kind": "link", "url": url, "ext": "", "name": "", "size": 0}
    else:
        if data is None or not name:
            raise ValueError("a file needs its bytes and its name")
        if len(data) > DOC_MAX:
            raise ValueError(f"over {DOC_MAX // (1024 * 1024)} MB")
        ext = doc_ext(name)
        entry = {"kind": doc_kind(ext), "ext": ext, "name": Path(name).name[:120], "size": len(data)}
    entry.update(id=uuid.uuid4().hex[:10], title=(title or entry.get("name") or url)[:120].strip(),
                 source=(source or "")[:40], added=int(now))
    with docs_lock():
        if not url:
            path = DOCS / (entry["id"] + ("." + entry["ext"] if entry["ext"] else ""))
            path.write_bytes(data)
            os.chmod(path, 0o600)
        docs = _docs_prune(_docs_read(), now)
        _atomic_write(DOCS_INDEX, json.dumps([entry] + docs, indent=1))
    return entry


def _docs_prune(docs, now):
    """Drop entries removed more than DOC_TRASH_DAYS ago, with their files."""
    keep = []
    for d in docs:
        if d.get("removed") and now - d["removed"] > DOC_TRASH_DAYS * 86400:
            if d.get("kind") != "link":
                doc_path(d).unlink(missing_ok=True)
            continue
        keep.append(d)
    return keep


def doc_path(d):
    return DOCS / (d["id"] + ("." + d["ext"] if d.get("ext") else ""))


def docs_list():
    return [d for d in _docs_read() if not d.get("removed")]


def doc_get(did):
    return next((d for d in docs_list() if d["id"] == did), None)


def doc_set_removed(did, removed, now=None):
    """Remove (removed=True) or restore one doc; False when there is no such doc in that state."""
    now = time.time() if now is None else now
    with docs_lock():
        docs = _docs_read()
        hit = next((d for d in docs if d["id"] == did and bool(d.get("removed")) != removed), None)
        if not hit:
            return False
        if removed:
            hit["removed"] = int(now)
        else:
            hit.pop("removed", None)
        _atomic_write(DOCS_INDEX, json.dumps(docs, indent=1))
    return True


# ---------- bedtime check: "am I set for tomorrow?" ----------

def bedtime_target(now):
    """The day a bedtime check is about: before 6 AM it is already "tomorrow" on the calendar."""
    return now.date() if now.hour < 6 else now.date() + timedelta(days=1)


def bedtime_payload(now, events, needs):
    """/api/bedtime from already-fetched parts: that day's events and the decisions due by the day after."""
    day = bedtime_target(now)
    soon = (day + timedelta(days=1)).isoformat()
    due_needs = [{"id": n["id"], "question": n["question"], "by": n.get("by")}
                 for n in needs if n.get("by") and n["by"] <= soon]
    on_day = [e for e in (events or []) if e["start"][:10] == day.isoformat()
              or (e.get("all_day") and e["start"][:10] < day.isoformat() < (e.get("end") or "")[:10])]
    return {"date": day.isoformat(), "label": f"{day:%A %b %-d}", "events": on_day,
            "needs": due_needs, "needs_total": len(needs), "count": len(due_needs)}


# ---------- Claude CLI login (shared by chat and bin/westly-run) ----------

AUTH_FLAG = REPO / ".auth-expired"  # present while the subscription login is dead; westly-health pings on it
AUTH_MARKERS = ("failed to authenticate", "oauth session expired", "oauth token has expired",
                "not logged in", "please run /login")


def cli_auth_failed(text):
    """True when a claude -p result says the subscription login is gone (only usage-limit
    text used to fall back to the API key, so expired logins failed silently for days)."""
    low = (text or "").lower()
    return any(m in low for m in AUTH_MARKERS)


def note_auth(expired):
    """Record the login state seen by a run that used the subscription (not the API key)."""
    if expired:
        if not AUTH_FLAG.exists():
            AUTH_FLAG.write_text(datetime.now(TZ).isoformat(timespec="minutes") + "\n")
    else:
        AUTH_FLAG.unlink(missing_ok=True)


# ---------- chat ----------

class Chat:
    """PDA chat history + in-flight turns. Turns run through the same claude_chat
    (and workspace lock) as Discord and Telegram, so it is one assistant everywhere.
    A reply's phase is "queued" until turn_fn reports the lock held, then "running", then "done"."""

    def __init__(self, turn_fn, on_done=None):
        self.turn_fn, self.on_done = turn_fn, on_done
        try:
            self.msgs = json.loads(CHAT_LOG.read_text())
        except (OSError, ValueError):
            self.msgs = []
        for m in self.msgs:  # a restart killed whatever was mid-turn
            if m.get("status") == "pending":
                m.update(status="error", text="Interrupted by a restart — send it again.",
                         phase="done")
        self.answering = {}  # need id -> chat id

    def _reply(self, cid):
        return next((m for m in self.msgs if m["id"] == cid and m["role"] == "westly"), None)

    def _mark_running(self, cid):
        m = self._reply(cid)
        if m and m["status"] == "pending":
            m["phase"] = "running"

    def _save(self):
        self.msgs = self.msgs[-CHAT_KEEP:]
        tmp = CHAT_LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.msgs))
        os.replace(tmp, CHAT_LOG)

    def start(self, text, shown=None):
        cid = "c_" + uuid.uuid4().hex[:10]
        now = int(time.time())
        self.msgs.append({"role": "me", "text": shown or text, "at": now, "id": cid,
                          "status": "done"})
        self.msgs.append({"role": "westly", "text": "", "at": now, "id": cid,
                          "status": "pending", "phase": "queued"})
        self._save()
        asyncio.get_running_loop().create_task(self._run(cid, text))
        return cid

    async def _run(self, cid, text):
        loop = asyncio.get_running_loop()

        def running():  # called on the worker thread once the turn lock is held
            try:
                loop.call_soon_threadsafe(self._mark_running, cid)
            except RuntimeError:  # loop already closed: shutting down
                pass

        try:
            reply, status = await asyncio.to_thread(self.turn_fn, text, running), "done"
        except subprocess.TimeoutExpired:
            reply, status = "That ran past my 10-minute limit and I killed it. Send it again to retry.", "error"
        except Exception as e:
            log.exception("pda chat turn failed")
            reply, status = f"That broke on my end: {e}"[:1900], "error"
        m = self._reply(cid)
        if m:
            m.update(text=reply, status=status, at=int(time.time()), phase="done")
        self.answering = {k: v for k, v in self.answering.items() if v != cid}
        self._save()
        if self.on_done:
            self.on_done()

    def get(self, cid):
        m = self._reply(cid)
        if not m:
            return None
        return {"id": cid, "status": m["status"], "reply": m["text"],
                "phase": m.get("phase", "done")}


# ---------- HTTP ----------

def origin_ok(origin, host, public_url):
    """A POST's Origin must be the app's own address: PUBLIC_URL when set, else the
    request's Host (ports ignored, since tailscale serve drops the :8443 from Host)."""
    if not origin:
        return True  # same-origin navigations and non-browser clients send none
    origin = origin.rstrip("/").lower()
    if public_url:
        return origin == public_url.rstrip("/").lower()
    got = urllib.parse.urlsplit(origin)
    return got.scheme == "https" and bool(got.hostname) and got.hostname == (host or "").split(":")[0].lower()


@web.middleware
async def guard(request, handler):
    who = config.PDA_LOGIN
    if not who or request.headers.get("Tailscale-User-Login", "").lower() != who:
        return web.json_response({"error": "forbidden"}, status=403)
    if request.method == "POST":
        if not origin_ok(request.headers.get("Origin"), request.host, config.PUBLIC_URL):
            return web.json_response({"error": "cross-origin"}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"error": "json only"}, status=415)
    resp = await handler(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    if isinstance(resp, web.Response) and resp.content_type == "application/json" \
            and len(resp.body or b"") >= COMPRESS_MIN:
        _compress(request, resp)
    return resp


def accepts(header, coding):
    """True when an Accept-Encoding header allows `coding` (directly or via *) with q > 0.
    Entries with a malformed or out-of-range q are ignored."""
    qs = {}
    for part in header.lower().split(","):
        name, *params = [x.strip() for x in part.split(";")]
        q = 1.0
        for prm in params:
            k, _, v = prm.partition("=")
            if k.strip() == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = None
        if name and q is not None and 0 <= q <= 1:
            qs[name] = q
    return qs.get(coding, qs.get("*", 0)) > 0


def _compress(request, resp):
    """gzip when the client accepts it, identity otherwise."""
    if accepts(request.headers.get("Accept-Encoding", ""), "gzip"):
        resp.enable_compression(ContentCoding.gzip)
    resp.headers["Vary"] = "Accept-Encoding"


async def _body(request):
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text='{"error":"bad json"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"error":"bad json"}', content_type="application/json")
    return data


def make_app(turn_fn):
    events_src = Source("gcal", events, default=[])
    forecast_src = Source("forecast", forecast)
    day_sources = {}  # /api/day: (start, days) -> Source, oldest dropped past DAY_SOURCES_MAX

    def events_changed():
        events_src.invalidate()
        for src in day_sources.values():
            src.dirty = True

    routes = web.RouteTableDef()
    cal_changed = add_cal_routes(routes, CALS, CALS, on_change=events_changed)
    chat = Chat(turn_fn, on_done=cal_changed)  # calendar edits made through chat show on the next read

    @routes.get("/")
    async def index(request):
        # a plain Response, not FileResponse: aiohttp 3.14 writes stray gzip bytes after a compressed 304
        path = STATIC / "index.html"
        st = path.stat()
        settings = await asyncio.to_thread(app_settings)
        conf = hashlib.sha256(page_json(settings).encode()).hexdigest()[:8]
        tag = f"{st.st_mtime_ns:x}-{st.st_size:x}-{conf}"  # a settings change is a new page too
        if any(e.value in (tag, "*") for e in request.if_none_match or ()):
            resp = web.Response(status=304, headers={"Cache-Control": "no-cache",
                                                     "Vary": "Accept-Encoding"})
            resp.etag = tag
            return resp
        resp = web.Response(body=render_page(path.read_bytes(), settings, tag), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-cache"})
        resp.etag = tag
        _compress(request, resp)
        return resp

    @routes.get("/{name}")
    async def static(request):
        name = request.match_info["name"]
        if name not in STATIC_FILES or not (STATIC / name).exists():
            raise web.HTTPNotFound()
        if name == "sw.js":  # revalidated every launch so a new worker is picked up promptly
            return web.FileResponse(STATIC / name, headers={
                "Cache-Control": "no-cache", "Content-Type": "text/javascript; charset=utf-8"})
        if name == "manifest.json":
            body = (STATIC / name).read_bytes().replace(NAME_MARK, config.ASSISTANT_NAME.encode())
            return web.Response(body=body, content_type="application/manifest+json",
                                headers={"Cache-Control": "max-age=3600"})
        return web.FileResponse(STATIC / name, headers={"Cache-Control": "max-age=3600"})

    @routes.get("/api/today")
    async def today(_):
        now = datetime.now(TZ)
        e = await events_src.get()
        return web.json_response(await asyncio.to_thread(today_payload, now, e))

    @routes.get("/api/feed")
    async def feed(request):
        try:
            before = float(request.query.get("before", "inf"))
        except ValueError:
            before = float("inf")
        files = [(mt, p) for mt, p in await asyncio.to_thread(sent_files) if mt < before]
        page = files[:FEED_PAGE]
        items = [feed_item(mt, p) for mt, p in page]
        nxt = page[-1][0] if len(files) > FEED_PAGE else None  # exact mtime: an int cursor skips same-second items
        return web.json_response({"items": items, "next": nxt})

    @routes.get("/api/needs")
    async def needs(_):
        items = parse_needs(_read(NEEDS))
        for it in items:
            it["pending"] = it["id"] in chat.answering
            it["chat"] = chat.answering.get(it["id"])  # lets the client re-find a lost POST reply
        return web.json_response({"items": items})

    @routes.post("/api/needs/answer")
    async def answer(request):
        data = await _body(request)
        q, a = str(data.get("question", "")).strip(), str(data.get("answer", "")).strip()
        if not q or not a:
            return web.json_response({"error": "question and answer required"}, status=400)
        nid = str(data.get("id") or slug(q))
        if nid in chat.answering:  # already being answered: never start a second turn
            cid = chat.answering[nid]
            got = chat.get(cid)
            return web.json_response({"id": cid, "duplicate": True,
                                      "status": got["status"] if got else "pending"})
        prompt = (f"[{config.OWNER_NAME} answered a Needs-You item in the app]\n"
                  f"Item: {q}\nAnswer: {a}\n\n"
                  "Act on it. Then delete that entry from memory/needs-you.md (or update it "
                  "if it is still open) and journal the resolution. Reply in 1-3 short lines.")
        cid = chat.start(prompt, shown=f"↳ {q}\n{a}")
        chat.answering[nid] = cid
        return web.json_response({"id": cid})

    def _nid(data):
        nid = str(data.get("id", "")).strip()
        return nid if re.fullmatch(r"[a-z0-9-]{1,60}", nid) else None

    @routes.post("/api/needs/dismiss")
    async def dismiss(request):
        nid = _nid(await _body(request))
        if not nid:
            return web.json_response({"error": "bad id"}, status=400)
        if nid in chat.answering:
            return web.json_response({"error": "Already being worked on"}, status=409)
        if not await asyncio.to_thread(dismiss_need, nid):
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.get("/api/needs/archive")
    async def archive(_):
        return web.json_response({"items": await asyncio.to_thread(archive_items)})

    @routes.post("/api/needs/archive/restore")
    async def restore(request):
        nid = _nid(await _body(request))
        if not nid:
            return web.json_response({"error": "bad id"}, status=400)
        if not await asyncio.to_thread(restore_need, nid):
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/needs/archive/delete")
    async def delete(request):
        data = await _body(request)
        if data.get("all") is True:
            return web.json_response({"ok": True, "deleted": await asyncio.to_thread(delete_archived, None)})
        nid = _nid(data)
        if not nid:
            return web.json_response({"error": "bad id"}, status=400)
        n = await asyncio.to_thread(delete_archived, nid)
        if not n:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True, "deleted": n})

    @routes.get("/api/day")
    async def day(request):
        today = datetime.now(TZ).date()
        try:
            start = date.fromisoformat(request.query.get("start", ""))
            days = int(request.query.get("days", "7"))
        except ValueError:
            return web.json_response({"error": "start=YYYY-MM-DD&days=N"}, status=400)
        if not (-DAY_BACK <= (start - today).days <= DAY_AHEAD) or not 1 <= days <= DAY_SPAN_MAX:
            return web.json_response({"error": "out of range"}, status=400)
        key = (start, days)
        src = day_sources.get(key)
        if src is None:
            def fetch(start=start, days=days):
                return cal_fetch(CALS, start, days)
            src = day_sources[key] = Source(f"gcal {start}+{days}", fetch, default=None)
            while len(day_sources) > DAY_SOURCES_MAX:
                old = next(iter(day_sources))
                if day_sources[old].task is None:
                    del day_sources[old]
                else:
                    break
        ev = await src.get()
        body = day_payload(start, days, ev if ev is not None else [])
        body["events_ok"] = ev is not None  # False: calendar not answering yet, retry later
        return web.json_response(body)

    @routes.get("/api/weather")
    async def weather_detail(_):
        w = await forecast_src.get()
        return web.json_response({"weather": w, "ok": w is not None, "configured": config.weather_on()})

    @routes.get("/api/chat")
    async def history(_):
        return web.json_response({"messages": chat.msgs[-50:]})

    @routes.post("/api/chat")
    async def send(request):
        text = str((await _body(request)).get("text", "")).strip()
        if not text:
            return web.json_response({"error": "empty message"}, status=400)
        return web.json_response({"id": chat.start(text)})

    @routes.get("/api/chat/{cid}")
    async def poll(request):
        got = chat.get(request.match_info["cid"])
        if not got:
            return web.json_response({"error": "unknown id"}, status=404)
        return web.json_response(got)

    @routes.post("/api/feedback")
    async def feedback(request):
        data = await _body(request)
        name, emoji = str(data.get("id", "")), data.get("emoji")
        path = SENT / name
        if emoji not in ("👍", "👎") or not re.fullmatch(r"[\w.-]+\.md", name) \
                or not path.is_file():
            return web.json_response({"error": "bad feedback"}, status=400)
        now = datetime.now(TZ)
        JOURNAL.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL / f"{now:%Y-%m}.md", "a") as f:
            f.write(feedback_line(emoji, f"{name}: {_read(path)}", now))
        return web.json_response({"ok": True})

    @routes.get("/api/tasks")
    async def tasks(_):
        return web.json_response(parse_tasks(_read(TASKS)))

    @routes.get("/api/bedtime")
    async def bedtime(_):
        now = datetime.now(TZ)
        day = bedtime_target(now)
        try:
            evs = await asyncio.wait_for(asyncio.to_thread(cal_fetch, CALS, day, 1), 6)
        except Exception as e:
            log.warning("bedtime: calendar unavailable: %s", e)
            evs = None
        body = bedtime_payload(now, evs, parse_needs(_read(NEEDS)))
        body["events_ok"] = evs is not None
        return web.json_response(body)

    @routes.get("/api/docs")
    async def docs(_):
        return web.json_response({"items": await asyncio.to_thread(docs_list)})

    async def _doc_op(request, removed):
        did = str((await _body(request)).get("id", ""))
        if not DOC_ID.fullmatch(did):
            return web.json_response({"error": "bad id"}, status=400)
        if not await asyncio.to_thread(doc_set_removed, did, removed):
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/docs/remove")
    async def doc_remove(request):
        return await _doc_op(request, True)

    @routes.post("/api/docs/restore")
    async def doc_restore(request):
        return await _doc_op(request, False)

    @routes.get("/doc/{id}")
    async def doc_file(request):
        did = request.match_info["id"]
        d = doc_get(did) if DOC_ID.fullmatch(did) else None
        if not d:
            raise web.HTTPNotFound()
        if d["kind"] == "link":
            raise web.HTTPFound(d["url"])
        path = doc_path(d)
        if not path.is_file():
            raise web.HTTPNotFound()
        ctype = DOC_TYPES.get(d["ext"], "application/octet-stream")
        safe_name = re.sub(r'[^\w. -]', "_", d.get("name") or path.name)
        headers = {"Content-Type": ctype, "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300",
                   "Content-Disposition": f'{"attachment" if ctype == "application/octet-stream" else "inline"}; filename="{safe_name}"'}
        if d["ext"] in ("html", "htm", "svg"):
            headers["Content-Security-Policy"] = "sandbox"  # no scripts, opaque origin: it can't drive this app's API
        return web.FileResponse(path, headers=headers)

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app.add_routes(routes)
    return app


async def serve(turn_fn):
    """Start the PDA on its socket. Never raises: a broken PDA must not take the bot down."""
    if not config.PDA_LOGIN:
        log.warning("PDA_LOGIN unset: the app will refuse every request (set it in westly.env)")
    try:
        runner = web.AppRunner(make_app(turn_fn), access_log=None)
        await runner.setup()
        SOCKET.unlink(missing_ok=True)
        old = os.umask(0o177)  # the socket is born 0600, never briefly world-connectable
        try:
            site = web.UnixSite(runner, str(SOCKET))
            await site.start()
        finally:
            os.umask(old)
        os.chmod(SOCKET, 0o600)
        log.info("pda listening on %s", SOCKET)
        return runner
    except Exception:
        log.exception("pda failed to start")
        return None


def selftest():
    assert feed_kind("brief-2026-10-12.md") == ("brief", "Morning brief")
    assert feed_kind("1791990000-attn-123.md") == ("attn", "Alert")
    assert feed_kind("test-x.md") == ("other", config.ASSISTANT_NAME)

    doc = ("# Needs you\nIntro line.\n\n"
           "## Renew the passport? (added 2026-10-05, by 2026-10-14)\n"
           "It expires in March.\n2 minutes online.\n"
           "options: Done | Snooze a week | Drop it\n\n"
           "## Pick a dentist (added 2026-10-10)\nTwo near home.\n")
    n = parse_needs(doc)
    assert [i["id"] for i in n] == ["renew-the-passport", "pick-a-dentist"], n
    assert n[0]["by"] == "2026-10-14" and n[0]["added"] == "2026-10-05", n[0]
    assert n[0]["options"] == ["Done", "Snooze a week", "Drop it"], n[0]
    assert n[0]["context"] == "It expires in March.\n2 minutes online.", n[0]
    assert n[1]["by"] is None and n[1]["options"] == [] and n[1]["context"] == "Two near home."
    assert parse_needs("") == [] and parse_needs("## no meta heading\nx") == []

    # upsert_need: append, then replace-in-place keeps the original added date
    t1 = upsert_need("# Needs you\n", "Backups: 1 failure waiting", "ctx a",
                     today="2026-10-01", match="Backups:")
    assert "## Backups: 1 failure waiting (added 2026-10-01)\nctx a" in t1, t1
    t2 = upsert_need(t1 + "\n## Other (added 2026-10-02)\nkeep me\n",
                     "Backups: 2 failures waiting", "ctx b", options=["Seen"],
                     today="2026-10-09", match="Backups:")
    got = parse_needs(t2)
    assert [i["question"] for i in got] == ["Backups: 2 failures waiting", "Other"], got
    assert got[0]["added"] == "2026-10-01" and got[0]["options"] == ["Seen"], got[0]
    assert got[1]["context"] == "keep me", got[1]
    assert len(parse_needs(upsert_need(t2, "Fresh", today="2026-10-09"))) == 3

    # dismiss/archive helpers: move an entry out and back without touching its neighbours
    block, rest = take_need(doc, "pick-a-dentist")
    assert block == "## Pick a dentist (added 2026-10-10)\nTwo near home.", block
    assert [i["id"] for i in parse_needs(rest)] == ["renew-the-passport"], rest
    assert rest.startswith("# Needs you\nIntro line.") and rest.endswith("Drop it\n"), rest
    assert take_need(doc, "nope") == (None, doc)
    first, rest1 = take_need(doc, "renew-the-passport")
    assert first.endswith("options: Done | Snooze a week | Drop it") and "Pick a dentist" in rest1
    st = stamp_archived(first, "2026-10-22")
    assert st.split("\n")[0].endswith("(added 2026-10-05, by 2026-10-14, archived 2026-10-22)"), st
    assert stamp_archived(st, "2026-10-23").count("archived") == 1
    a = parse_needs(st)[0]
    assert a["id"] == "renew-the-passport" and a["by"] == "2026-10-14", a
    assert archived_on(st) == {"renew-the-passport": "2026-10-22"}
    assert unstamp_archived(st) == first
    back = append_block(rest1, unstamp_archived(st), "# Needs you\n")
    assert sorted(i["id"] for i in parse_needs(back)) == sorted(i["id"] for i in n), back
    assert append_block("", "## X (added 2026-10-01)", "# H\n") == "# H\n\n## X (added 2026-10-01)\n"

    # weather shaping, both unit systems; alerts None = lookup failed, [] = none
    fc = {"current": {"temperature_2m": 73.7, "apparent_temperature": 71.7, "relative_humidity_2m": 50,
                      "wind_speed_10m": 7.7, "wind_gusts_10m": 15.4, "wind_direction_10m": 101,
                      "weather_code": 3, "is_day": 0, "time": "2026-10-22T02:45"},
          "hourly": {"time": ["2026-10-22T03:00"], "temperature_2m": [72.0], "precipitation_probability": [5],
                     "weather_code": [3], "is_day": [0]},
          "daily": {"time": ["2026-10-22"], "temperature_2m_max": [84.0], "temperature_2m_min": [66.0],
                    "precipitation_probability_max": [0], "weather_code": [1], "sunrise": ["2026-10-22T06:47"],
                    "sunset": ["2026-10-22T17:52"], "uv_index_max": [5.1], "wind_speed_10m_max": [12.0]}}
    w = shape_forecast(fc, {"features": [{"properties": {"event": "Wind Advisory", "severity": "Moderate",
                                                          "headline": "Gusts to 50", "ends": "x"}}]})
    assert w["current"]["text"] == "Cloudy" and w["current"]["feels"] == 71.7 and w["current"]["day"] is False, w
    assert w["hours"] == [{"t": "2026-10-22T03:00", "temp": 72.0, "pop": 5, "code": 3, "day": False}], w
    assert w["days"][0]["sunset"] == "2026-10-22T17:52" and w["days"][0]["hi"] == 84.0, w
    assert w["alerts"][0]["event"] == "Wind Advisory" and shape_forecast(fc, None)["alerts"] is None
    assert w["units"] == {"temp": "°F", "wind": "mph"}
    m = shape_forecast(fc, {"features": []}, metric=True, nws=False)
    assert m["units"] == {"temp": "°C", "wind": "km/h"} and m["alerts"] == [] and m["source"] == "Open-Meteo", m

    tasks = parse_tasks("# Open tasks\n\n## Do X (added 2026-08-16)\nline one\nline two\n\n## not a task\nx\n")
    assert tasks == {"items": [{"title": "Do X", "added": "2026-08-16", "body": "line one\nline two"}]}, tasks

    assert health_summary({}) == {"ok": True, "problems": []}
    assert health_summary({"reported": {"a": "X down"}}) == {"ok": False, "problems": ["X down"]}
    line = feedback_line("👍", "brief-1.md: ☀️  **Morning**\n\nhi",
                         datetime(2026, 10, 12, 9, 5, tzinfo=timezone.utc))
    assert line == '- 2026-10-12 09:05 feedback: 👍 on "brief-1.md: ☀️ **Morning** hi" (PDA)\n', line
    assert day_rows(date(2026, 12, 31), 2) == [{"date": "2026-12-31"}, {"date": "2027-01-01"}]

    # page rendering: settings land as script-safe JSON, the name everywhere it is marked
    raw = b"<title>{{ASSISTANT}}</title><script>const B='__WESTLY_BUILD__', C=__WESTLY_CONFIG__;</script>"
    out = render_page(raw, {"assistant": "Ada", "x": "</script><b>&"}, "t1").decode()
    assert out.startswith("<title>Ada</title>") and "B='t1'" in out and "</script><b>" not in out, out
    assert json.loads(out.split("C=", 1)[1].split(";</script>")[0]) == {"assistant": "Ada", "x": "</script><b>&"}
    page = (STATIC / "index.html").read_bytes()
    assert page.count(BUILD_MARK) == 1 and page.count(CONFIG_MARK) == 1, "one build marker and one config marker"

    # origin check: PUBLIC_URL when set, else the request's own host over https
    assert origin_ok(None, "x", "") and origin_ok("https://box.example.ts.net:8443", "", "https://box.example.ts.net:8443/")
    assert not origin_ok("https://evil.example", "", "https://box.example.ts.net:8443")
    assert origin_ok("https://box.example.ts.net:8443", "box.example.ts.net", "")
    assert not origin_ok("https://evil.example", "box.example.ts.net", "") and not origin_ok("http://box", "box", "")
    assert not origin_ok("null", "box", "")

    # Source: cached while fresh, stale served while revalidating, a failure keeps the last value
    async def source_check():
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) == 3:
                raise OSError("down")
            return len(calls)
        s = Source("t", fetch)
        assert await s.get() == 1 and await s.get() == 1 and len(calls) == 1
        s.ok_at -= TODAY_TTL
        assert await s.get() == 1
        await s.task
        assert await s.get() == 2
        s.ok_at -= TODAY_TTL
        assert await s.get() == 2
        await s.task
        assert await s.get() == 2 and len(calls) == 3
        s.invalidate()
        assert await s.get() == 4
        s.ok_at -= TODAY_STALE_MAX + 1  # long idle: the same read waits for the fresh value
        s.failed_at = float("-inf")  # (the call-3 failure would otherwise hold off the retry)
        assert await s.get() == 5
    asyncio.run(source_check())
    assert accepts("gzip, deflate, br", "gzip") and accepts("*", "gzip")
    assert not accepts("gzip;q=0", "gzip") and not accepts("deflate", "gzip") and not accepts("", "gzip")
    assert accepts("gzip;q=0.5", "gzip") and not accepts("*;q=0", "gzip")
    assert not accepts("gzip;q=0, *", "gzip") and accepts("gzip;q=bad, *", "gzip")

    # docs shelf: add a file and a link, remove + restore, trash purged after DOC_TRASH_DAYS
    global DOCS, DOCS_INDEX, DOCS_LOCK
    import tempfile
    real_docs = DOCS, DOCS_INDEX, DOCS_LOCK
    DOCS = Path(tempfile.mkdtemp()) / "shelf"
    DOCS_INDEX, DOCS_LOCK = DOCS / "index.json", DOCS / ".lock"
    try:
        f = doc_add("Quote", "laptop", b"%PDF-1.4", "Quote 1001.PDF", now=1000)
        assert f["kind"] == "pdf" and f["ext"] == "pdf" and doc_path(f).read_bytes() == b"%PDF-1.4"
        ln = doc_add("", "laptop", url="https://example.com/x", now=1001)
        assert ln["kind"] == "link" and ln["title"] == "https://example.com/x"
        assert [d["id"] for d in docs_list()] == [ln["id"], f["id"]], "newest first"
        assert doc_kind(doc_ext("notes.md")) == "text" and doc_kind(doc_ext("x.zip")) == "file" and doc_ext("a.b/c") == ""
        assert doc_set_removed(f["id"], True, now=2000) and doc_get(f["id"]) is None
        assert not doc_set_removed(f["id"], True), "already removed"
        assert doc_set_removed(f["id"], False) and doc_get(f["id"])
        doc_set_removed(f["id"], True, now=2000)
        doc_add("later", "server", b"x", "a.txt", now=2000 + DOC_TRASH_DAYS * 86400 + 1)
        assert not doc_path(f).exists() and all(d["id"] != f["id"] for d in _docs_read()), "purged"
        for bad in (dict(url="javascript:alert(1)"), dict(data=b"x")):
            try:
                doc_add("t", "s", **bad)
                raise AssertionError(bad)
            except ValueError:
                pass
    finally:
        DOCS, DOCS_INDEX, DOCS_LOCK = real_docs

    # bedtime: before 6 AM the check is about today; events on that day, decisions due by the next
    tz = timezone.utc
    assert bedtime_target(datetime(2026, 10, 24, 1, 30, tzinfo=tz)) == date(2026, 10, 24)
    assert bedtime_target(datetime(2026, 10, 24, 22, 0, tzinfo=tz)) == date(2026, 10, 25)
    needs = [{"id": "n1", "question": "q1", "by": "2026-10-26"}, {"id": "n2", "question": "q2", "by": None},
             {"id": "n3", "question": "q3", "by": "2026-10-27"}]
    evs = [{"start": "2026-10-25T09:00:00-07:00", "title": "Dentist", "all_day": False},
           {"start": "2026-10-26", "end": "2026-10-27", "title": "Later", "all_day": True},
           {"start": "2026-10-24", "end": "2026-10-27", "title": "Trip", "all_day": True}]
    p = bedtime_payload(datetime(2026, 10, 24, 23, 0, tzinfo=tz), evs, needs)
    assert p["date"] == "2026-10-25" and [e["title"] for e in p["events"]] == ["Dentist", "Trip"], p
    assert [x["id"] for x in p["needs"]] == ["n1"] and p["needs_total"] == 3 and p["count"] == 1, p
    q = bedtime_payload(datetime(2026, 10, 24, 23, 0, tzinfo=tz), None, [])
    assert q["events"] == [] and q["count"] == 0
    print("PASS")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else 2)
