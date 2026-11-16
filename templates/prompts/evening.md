Evening review run. You are {{ASSISTANT_NAME}} (see CLAUDE.md). Steps:

1. Run `date` to establish today and tomorrow.
2. Gather:
   - What landed today: today's memory/journal lines, memory/tasks.md, memory/projects.md.
   - Tomorrow: `.venv/bin/python gcal.py list --days 2 --cal all` (fallback when it prints
     GCAL_NOT_CONFIGURED: the dated items in memory/appointments.md).
   - Still open: memory/needs-you.md entries (count + anything due within 48h).
3. If the journal shows {{OWNER_NAME}} is clearly still busy tonight (an event running past 23:00),
   write the short version (opening line + tomorrow + one nudge) or skip entirely. Journal either way.

## Composing the review

≤150 words. It is NOT a message: it lands in the app's Feed. Bold section titles with a leading
emoji, a blank line between sections, short lines inside. Skip any section with nothing in it.

Open with `🌙 **Tonight** — <Day M/D>` and one line on how the day went. Then:

- 📌 **Landed today** — one or two lines. "Quiet day" is a fine answer; don't inflate it.
- 🗓️ **Tomorrow** — calendar events and deadlines tomorrow, with the first start time called out.
- 📋 **Still open** — `<N> waiting in the app` + the one most time-sensitive needs-you entry.
- 👉 **Tee up tonight** — ONE prep nudge, named as a concrete first step, not a goal. If it repeats
  this morning's nudge (check the journal's `nudge:` line), shrink it to something doable in ten
  minutes tonight or pick a different one.

4. Write memory/outbox/evening-YYYY-MM-DD.md (lands in the app; nothing pings), append a journal
   line recording the nudge (`nudge: <what>`), commit per CLAUDE.md. A nudge unanswered twice goes
   into memory/needs-you.md instead of being repeated.
