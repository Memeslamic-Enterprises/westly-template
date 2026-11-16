Monthly finance check-in. You are {{ASSISTANT_NAME}} (see CLAUDE.md). Steps:

1. Run `date`.
2. Read memory/finance.md (the monthly checklist) and memory/bills.md (billers, autopay, renewals).
3. Compose the check-in note (under 250 words; it lands in the app's Feed, not as a message): walk
   each checklist item, then the bills without autopay and any renewal inside the next 60 days.
   Each item marked "(pending)" gets ONE memory/needs-you.md entry (add it, or refresh the one already
   there) so {{OWNER_NAME}} can answer from the app. Once the journal shows an item pending at two
   earlier check-ins, the note stops asking and gives it one line pointing at Needs You.
4. Renewals with a date and no calendar event yet → `.venv/bin/python gcal.py add --title "<what> expires"
   --start YYYY-MM-DD --all-day --heads-up 30` (skip when GCAL_NOT_CONFIGURED).
5. Write it to memory/outbox/finance-YYYY-MM.md.
6. Pruning pass: any memory file over ~200 lines (journal excluded) gets its resolved history moved
   into the journal.
7. Append a journal line; commit per CLAUDE.md. Never move money, log in to a bank, or pay anything.
