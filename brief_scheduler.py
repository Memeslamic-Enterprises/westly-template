#!/usr/bin/env python3
"""Westly daily-brief scheduler.

Fires the morning brief once a day: BRIEF_LEAD_H hours before the first timed event
on BRIEF_CALENDAR (a gcal.py calendar key such as "main"; a work calendar is the
usual pick), or at BRIEF_TIME on days without one. With BRIEF_CALENDAR unset, or
Google Calendar not configured, it is simply a daily BRIEF_TIME trigger.
Deterministic; a state file keeps it to one fire per date.

Run modes:
  brief_scheduler.py            real run (check calendar, maybe trigger brief)
  brief_scheduler.py --selftest assert-based checks of the pure logic
"""
import json
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta

import config

REPO = config.REPO
STATE_FILE = REPO / ".brief_triggered.json"
TZ = config.TZ
UNIT = "westly-run@morning.service"


def parse_hhmm(text, default=dtime(7, 45)):
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return default


BRIEF_TIME = parse_hhmm(config.get("BRIEF_TIME", "07:45"))
BRIEF_LEAD_H = config.number("BRIEF_LEAD_H", 2)
BRIEF_CALENDAR = config.get("BRIEF_CALENDAR")


def start_dt(event, tz=TZ):
    """Aware datetime for a timed gcal.list_events() item, or None for all-day/malformed."""
    if not isinstance(event, dict) or event.get("all_day") or not isinstance(event.get("start"), str):
        return None
    try:
        dt = datetime.fromisoformat(event["start"])
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


def target_time(events, now, fallback=BRIEF_TIME, lead_h=BRIEF_LEAD_H):
    """Pure decision: local datetime the brief should fire today."""
    starts = [s for s in (start_dt(e, now.tzinfo) for e in events) if s and s.date() == now.date()]
    if starts:
        return min(starts) - timedelta(hours=lead_h)
    return datetime.combine(now.date(), fallback, tzinfo=now.tzinfo)


def should_trigger(events, now, state, fallback=BRIEF_TIME, lead_h=BRIEF_LEAD_H):
    """Pure decision: (fire?, state-key). Fires once per calendar date."""
    key = now.date().isoformat()
    if key in state:
        return False, key
    return now >= target_time(events, now, fallback, lead_h), key


def prune_state(state, now):
    cutoff = (now - timedelta(days=3)).date().isoformat()
    return {k: v for k, v in state.items() if k >= cutoff}


def fetch_events(now):
    if not BRIEF_CALENDAR:
        return []
    import gcal
    if not gcal.configured(BRIEF_CALENDAR):
        return []
    day = datetime.combine(now.date(), dtime.min, tzinfo=TZ)
    return gcal.list_events(BRIEF_CALENDAR, days=1, start=day)


def load_state():
    try:
        got = json.loads(STATE_FILE.read_text())
        return got if isinstance(got, dict) else {}
    except (ValueError, OSError):
        return {}


def main():
    now = datetime.now(TZ)
    try:
        events = fetch_events(now)
    except Exception as e:
        # a calendar blip falls back to BRIEF_TIME instead of skipping the day
        print(f"brief_scheduler: calendar fetch failed: {e}", file=sys.stderr)
        events = []
    state = load_state()
    fire, key = should_trigger(events, now, state)
    if not fire:
        return 0
    state[key] = now.isoformat()
    STATE_FILE.write_text(json.dumps(prune_state(state, now), indent=2, sort_keys=True))
    # --no-block: a plain start waits for the oneshot brief, which outlasts this unit's timeout
    subprocess.run(["systemctl", "--user", "start", "--no-block", UNIT], check=False)
    print(f"triggered morning brief for {key}")
    return 0


def selftest():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    fb, lead = dtime(7, 45), 2

    def ev(start, all_day=False):
        return {"start": start, "all_day": all_day, "title": "x"}

    events = [ev("2026-08-17T14:00:00-04:00")]
    assert not should_trigger(events, datetime(2026, 8, 17, 11, 59, tzinfo=tz), {}, fb, lead)[0]
    fire, key = should_trigger(events, datetime(2026, 8, 17, 12, 0, tzinfo=tz), {}, fb, lead)
    assert fire and key == "2026-08-17"
    assert not should_trigger(events, datetime(2026, 8, 17, 12, 0, tzinfo=tz), {key: "x"}, fb, lead)[0]
    # no event today -> fallback time; tomorrow's event and all-day events don't count
    assert not should_trigger([], datetime(2026, 8, 17, 7, 44, tzinfo=tz), {}, fb, lead)[0]
    assert should_trigger([], datetime(2026, 8, 17, 7, 45, tzinfo=tz), {}, fb, lead)[0]
    assert should_trigger([ev("2026-08-18T09:00:00-04:00")], datetime(2026, 8, 17, 8, 0, tzinfo=tz), {}, fb, lead)[0]
    assert should_trigger([ev("2026-08-17", True)], datetime(2026, 8, 17, 7, 45, tzinfo=tz), {}, fb, lead)[0]
    # earliest timed event wins; an event in another offset is compared as the same instant
    two = [ev("2026-08-17T14:00:00-04:00"), ev("2026-08-17T13:00:00Z")]  # 13:00Z = 09:00 local
    assert target_time(two, datetime(2026, 8, 17, 6, 0, tzinfo=tz), fb, lead).hour == 7
    assert start_dt({"start": "garbage"}, tz) is None and start_dt(None, tz) is None
    assert parse_hhmm("6:30") == dtime(6, 30) and parse_hhmm("nope") == dtime(7, 45)
    assert "2026-08-10" not in prune_state({"2026-08-10": "x", "2026-08-16": "y"}, datetime(2026, 8, 17, tzinfo=tz))
    print("PASS")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    sys.exit(main())
