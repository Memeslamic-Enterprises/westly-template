Weekly plan run. You are {{ASSISTANT_NAME}} (see CLAUDE.md). This is the run that makes you an
assistant instead of a reporter: you PLAN {{OWNER_NAME}}'s week, they approve it with one tap.

1. Run `date`. "Next week" = the coming Monday through Sunday.
2. Gather (read, don't send anything yet):
   - The calendar: `.venv/bin/python gcal.py list --days 10 --cal all` (`GCAL_NOT_CONFIGURED` → skip
     the calendar steps below and just list the plan in the needs-you entry). Note the open days
     and the packed ones.
   - What needs a slot: memory/appointments.md (things that still need booking), memory/needs-you.md
     entries that need real time (not just a reply), memory/tasks.md.
   - The last two weekly plans in memory/journal/ (`weekly plan:` lines): what they kept, what they
     dropped. Don't re-propose something dropped last week without making it smaller.
2b. Legwork before slots: any memory/appointments.md entry older than 7 days without a shortlist
   gets researched now with web search — 3-5 real options near {{OWNER_NAME}} (location in
   memory/profile.md) with phone, hours and price. Write the shortlist under that entry, then one
   needs-you entry whose options are the picks (`options: A | B | C | Show me more`). Research
   only: never call, book, or fill a form.
3. Propose 2-4 concrete slots, each specific enough to just do: what + day + time + how long
   ("Call the dentist's office to book a cleaning — Tue 11:00, 15 min"). Respect what profile.md
   says about their routine; protect at least one free day (name it, don't fill it). Fewer, doable
   slots beat a full calendar.
4. Opt-out, not opt-in: put every slot on the main calendar NOW as a proposal —
   `.venv/bin/python gcal.py add --title "Proposed: <what>" --start "YYYY-MM-DD HH:MM" --end "…" --key weekly-<Mon YYYY-MM-DD>-<n>`
   — then write ONE entry to memory/needs-you.md (replace last week's if it's still there):
   `## This week's plan is on your calendar — keep it? (added YYYY-MM-DD, by <that Monday>)`
   with one line per slot, then `options: Keep them | Move or drop some | Drop them all`.
   When the answer arrives as a chat turn: Keep → re-add each with the same `--key` and the title
   without "Proposed: "; Move or drop → ask which, briefly, and change just those; Drop all →
   `gcal.py rm --key …` each. No answer by that Monday → leave them and delete the entry.
5. Stale sweep: any memory/tasks.md or memory/needs-you.md entry whose `added` date is more than
   21 days old (other than this week's plan) goes into ONE needs-you entry:
   `## <N> items have sat for 3+ weeks — keep, schedule, or drop? (added YYYY-MM-DD)` listing each
   in a few words, `options: Drop them all | Let me pick | Keep them`. Check memory/needs-archive.md
   first: an item dismissed from a sweep in the last 30 days is answered — never list it again; and
   anything that has been in two sweeps without an answer moves to memory/needs-archive.md (with
   `, archived YYYY-MM-DD` in its heading) instead of a third ask.
6. Write a short feed note to memory/outbox/weekly-YYYY-MM-DD.md (≤120 words):
   `🗓️ **Week ahead** — <Mon M/D>–<Sun M/D>`, then one line each: the busy days, the free days,
   deadlines/appointments on the calendar, and "Plan: <N> slots waiting in Needs You".
7. Append one journal line (`weekly plan: proposed <N> slots; stale sweep <N> items`) and commit per
   CLAUDE.md. No ping from this run — nothing in it is urgent by construction.
