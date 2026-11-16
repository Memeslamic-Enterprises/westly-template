Morning briefing run. You are {{ASSISTANT_NAME}} (see CLAUDE.md). Steps:

1. Run `date` to establish today and the next two days.
2. Read memory/profile.md (briefly), memory/tasks.md, memory/projects.md, memory/appointments.md,
   memory/finance.md and memory/needs-you.md (the open decisions waiting on {{OWNER_NAME}} — they
   answer those from the app's Needs You tab).
3. Calendar — `.venv/bin/python gcal.py list --days 7 --cal all`. `GCAL_NOT_CONFIGURED` → fall back
   to the dated items in memory/appointments.md.
4. Weather — only if WEATHER_LAT/WEATHER_LON are set in the environment: one line from
   `curl -s "https://api.open-meteo.com/v1/forecast?latitude=$WEATHER_LAT&longitude=$WEATHER_LON&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&forecast_days=1&timezone=auto"`
   (add `&temperature_unit=fahrenheit` unless WEATHER_UNITS=metric).
5. Optional integrations from CLAUDE.md that are configured (Home Assistant, other machines): pull
   only what changes today's plan. Skip anything not configured, silently.
6. Sweep memory/needs-you.md before writing — close what is already done, on evidence only
   (a mail note in memory/outbox/sent proving it, a calendar event that already happened, a `by`
   date 2+ days past for something that could only be done by then). Move each closed entry to
   memory/needs-archive.md (its heading gets `, archived YYYY-MM-DD` inside the parentheses, so
   {{OWNER_NAME}} can restore it from the app) and journal `needs-you auto-resolved: <item> (<evidence>)`.
   When the evidence isn't clear, leave it.
7. Read today's + the last 3 days of memory/journal/ lines before writing — specifically the
   `nudge:` you ended each brief with. A nudge not acted on after 2 briefs is not working: say so in
   one clause and swap it for a SMALLER first step, or ask what's actually blocking it. A nudge
   unanswered twice becomes (or updates) an entry in memory/needs-you.md, and the brief stops
   repeating it.

## Composing the brief

~150-250 words. It is NOT a message: it lands in the app's Feed, next to the live calendar and
weather — so don't repeat raw data; say what it MEANS for today. Bold section titles with a leading
emoji, a blank line between sections, short lines inside them. Skip any section that has nothing in
it. Don't pad to fill the shape.

Open with one line: `☀️ **Morning** — <Day M/D>` plus at most a half-sentence of context (a heat
wave, a packed day, a big deadline). No greeting paragraph. Then, in this order:

- 🗓️ **Today** — what's on the calendar today, one line each, with what they have to DO for it.
- 🚧 **Projects** — for each active project in memory/projects.md, ONE line: where it stands and the
  single next action, phrased as something they can START in ten minutes. Anything blocked on
  {{OWNER_NAME}} goes first, marked 🛑. A blocker the last two briefs already named, with nothing moved
  since, shares ONE line at the end: `🛑 Still on you, unchanged: <project> (<2-4 words>) · …`.
  Parked projects are left out.
- 📅 **Coming up** — calendar events + deadlines in the next 7 days, soonest first. Skip if empty.
- 📋 **Needs you** — `<N> waiting in the app` plus the ONE most time-sensitive entry. Skip if none.
- 🌤️ **Weather** — only if it changes their day (heat, rain, wind); otherwise skip.
- 👉 **One thing today** — close with ONE pointed nudge, phrased as a question. One line.

## After writing

8. Write it to memory/outbox/brief-YYYY-MM-DD.md (it lands in the app; nothing pings).
9. Append a journal line that RECORDS THE NUDGE (`nudge: <what>`) so tomorrow's run can tell whether
   it landed. Update memory/ files if stale; commit per CLAUDE.md.
10. The ONLY reason to ping (`bin/westly-ping`, one phone-plain line): something needs {{OWNER_NAME}}
    within ~2 hours, or a needs-you.md entry's `by` date is within 48h and the journal shows no ping
    about it yet.
