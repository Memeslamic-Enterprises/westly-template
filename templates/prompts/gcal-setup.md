Google Calendar setup for {{ASSISTANT_NAME}} — one time, ~10 minutes. Used when {{OWNER_NAME}} asks
to connect the calendar. Walk them through it in short steps; they do the clicking. Never paste the
key file's contents anywhere — it is a secret.

On a computer, signed in to the Google account whose calendar should be used:

1. console.cloud.google.com → project picker → New project → name it (e.g. "assistant") → Create.
   No billing account needed.
2. In that project: APIs & Services → Library → "Google Calendar API" → Enable.
3. IAM & Admin → Service accounts → Create service account → name it (e.g. "calendar") →
   Create and continue → skip the role → Done. Copy its email (…@<project>.iam.gserviceaccount.com).
4. Open the service account → Keys → Add key → Create new key → JSON. A file downloads.
   It must end up on the server as `gcal-sa.json` in this directory, mode 600:
   `scp <file> <server>:<this directory>/gcal-sa.json && ssh <server> chmod 600 <this directory>/gcal-sa.json`
   Then delete the downloaded copy.
5. calendar.google.com → Other calendars "+" → Create new calendar (e.g. "{{ASSISTANT_NAME}}").
   In its Settings:
   - Share with specific people → add the service-account email → "Make changes to events".
   - Event notifications: choose the reminders the phone should show (they come from HERE).
   - Integrate calendar → copy the Calendar ID (…@group.calendar.google.com).
   Optional: repeat for a shared calendar (shared with a partner or household).
6. Put the IDs in westly.env as `GCAL_MAIN=` (and `GCAL_SHARED=` if made). These IDs are not
   secrets; you may write those lines yourself once {{OWNER_NAME}} sends them.
7. Verify: `.venv/bin/python gcal.py check` → an "ok" line per calendar. Then backfill any dated items
   from memory/appointments.md (`gcal.py add …`) and delete them there.
8. Restart so the app picks up the new settings:
   `systemd-run --user --on-active=5s systemctl --user restart westly-bot`. Journal it.
