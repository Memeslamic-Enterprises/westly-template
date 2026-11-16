# Westly

A self-hosted personal assistant: headless [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
with a file-based memory, a small set of delivery tiers so it only interrupts you when it should,
and a phone app (a home-screen web app) served privately over your tailnet.

You talk to it from the app, and optionally Telegram or Discord — one conversation everywhere. On a
schedule it writes a morning brief, an evening review, a weekly plan and a monthly finance check-in,
and (optionally) triages your inbox. Everything it knows lives in plain Markdown files in this
directory, committed to a local git history.

> **Screenshots:** _placeholder — add `docs/today.png`, `docs/needs.png`, `docs/chat.png` here._

## How it fits together

```
 phone (home-screen app)          Telegram / Discord (optional)
          │  https, tailnet only            │  bot APIs (outbound only)
          ▼                                 ▼
 tailscale serve :8443 ──unix socket──► westly_bot.py ─────────────┐
   (stamps Tailscale-User-Login)        ├─ pda.py  (the app's API)  │ one shared session,
                                        ├─ tg.py / discord.py       │ one lock
                                        └─ outbox filer (every 20s) │
                                                                     ▼
                                              claude -p  (headless Claude Code, cwd = this repo)
                                                 │ reads CLAUDE.md + memory/*.md
                                                 │ writes memory/, memory/outbox/, calendar
                                                 ▼
  systemd timers ──► bin/westly-run <task> ──► claude -p prompts/<task>.md   (fresh session each run)
  westly-health.timer ──► bin/westly-health ──► feed note on change, ping if urgent
  bin/westly-ping ──► Telegram, SMS fallback       gcal.py ──► Google Calendar (optional)
```

- **Memory** is `memory/*.md` (profile, tasks, projects, appointments, finance, bills, needs-you,
  journal). Sessions are disposable; the files are the assistant.
- **The app** reads the same files the assistant writes: the Feed is `memory/outbox/sent/`, Needs You is
  `memory/needs-you.md`, Tasks is `memory/tasks.md`, plus the calendar and the weather.

## The delivery tiers

Every output picks exactly one tier, so your phone only buzzes when it should:

| Tier | What qualifies | How |
|---|---|---|
| **Ping** | Needs you within ~48h, or something breaks without you | `bin/westly-ping "one line"` → Telegram, SMS fallback; throttled (same text muted 4h, max 6/hour) |
| **Calendar** | Anything with a date or time | `gcal.py add …` — your calendar app does the reminding |
| **Needs You** | A decision that can wait > 48h | an entry in `memory/needs-you.md`; you answer with one tap in the app |
| **Feed** | Everything else: briefs, digests, reports | a file in `memory/outbox/`; shows in the app's Feed, never pings |

## Quickstart

Requirements: Linux with systemd (user services), Python 3.10+, git, [Tailscale](https://tailscale.com),
and the Claude Code CLI (`claude`) with a Claude subscription or API key.

```sh
git clone <this repo> ~/westly && cd ~/westly
python3 setup.py
```

The wizard asks for your name, the assistant's name, your time zone, your Tailscale login, which chat
channels to use (app only / + Telegram / + Discord), and optionally a location for weather. It then:

- writes `westly.env` (mode 0600, gitignored) — every setting in one file;
- renders `CLAUDE.md` and `prompts/*.md` from `templates/` with your names filled in;
- creates `.venv` and installs `requirements.txt`;
- installs and starts the systemd user units (`westly-bot` and the timers).

Then, once:

```sh
claude                     # then type /login — chat and scheduled runs use this login
loginctl enable-linger $USER          # keep the services running while you're logged out
sudo tailscale serve --bg --https=8443 unix:$HOME/westly/.pda.sock
```

Open `https://<this machine>.<tailnet>.ts.net:8443` on your phone (signed in to Tailscale as the login
you gave setup) and **Add to Home Screen**. Run `bin/westly-health --report` to check everything.

Re-run `python3 setup.py` any time; your current answers are the defaults. It never overwrites an
existing `CLAUDE.md` or `prompts/` unless you pass `--rerender`.

## Configuration reference (`westly.env`)

`westly.env.example` documents every key. The important ones:

| Key | Default | What it does |
|---|---|---|
| `OWNER_NAME` / `ASSISTANT_NAME` | — / `Westly` | Names used by the persona and the app (letters, digits, spaces, hyphens) |
| `TIMEZONE` | server's zone | IANA zone for briefs and the app |
| `WESTLY_MODEL` | `sonnet` | Model for chat and scheduled runs; prefix a message `opus:` for a one-off heavier turn |
| `CLAUDE_BIN` | `claude` on PATH | Path to the CLI |
| `WESTLY_API_KEY` | — | Optional API key used only when the subscription login expired or hit its limit |
| `PDA_LOGIN` | — (**required**) | The only Tailscale identity the app answers |
| `PUBLIC_URL` | — | The app's address; used for the cross-site check and `westly-doc` links |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram chat + pings (setup finds the chat id) |
| `DISCORD_TOKEN` / `OWNER_ID` | — | Discord DMs |
| `SMS_TO` | — | SMS ping fallback via carrier email gateways (US), sent with the `MAIL_*` SMTP account |
| `MAIL_USER` / `MAIL_PASSWORD` / `MAIL_IMAP_HOST` / `MAIL_SMTP_*` | — | Read-only inbox triage (use an app password) |
| `GCAL_KEY` / `GCAL_MAIN` / `GCAL_SHARED` / `GCAL_*_LABEL` | `gcal-sa.json` | Google Calendar through a service account |
| `BRIEF_TIME` / `BRIEF_LEAD_H` / `BRIEF_CALENDAR` | `07:45` / `2` / — | When the morning brief fires |
| `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_PLACE` / `WEATHER_UNITS` / `WEATHER_NWS_ALERTS` | — | Keyless Open-Meteo forecast (+ optional US NWS alerts) |
| `HA_URL` / `HA_TOKEN` | — | Home Assistant, for the assistant's recipes and the health check |
| `HEALTH_SSH_HOSTS` | — | `user@host` list the health check pings over ssh |

After editing: `systemctl --user restart westly-bot`.

## Schedules

| Unit | When | What |
|---|---|---|
| `westly-briefcheck.timer` | every 15 min | fires the morning brief once a day (`brief_scheduler.py`) |
| `westly-evening.timer` | 21:30 daily | evening review |
| `westly-weekly.timer` | Sun 17:00 | weekly plan (proposed slots on your calendar, one tap to keep) |
| `westly-finance.timer` | 1st of the month | finance check-in |
| `westly-mail.timer` | 09:20, 12:20, 17:20, 21:20 | mail triage (installed only when `MAIL_*` is set) |
| `westly-health.timer` | hourly | health check |

Each scheduled task is `westly-run@<task>.service` running `prompts/<task>.md`. Change a time with
`systemctl --user edit westly-evening.timer`; add a task by writing `prompts/<name>.md` plus a timer
with `Unit=westly-run@<name>.service`.

## Adding an integration

1. Give the assistant the credentials in `westly.env` (never in a prompt or memory file).
2. Teach it the recipe: add a short, clearly optional section under **Optional integrations** in your
   `CLAUDE.md` (the Home Assistant section is the pattern: a read recipe, and actions only on your ask).
3. If a scheduled run should use it, mention it in the relevant `prompts/*.md`.
4. If it should be health-checked, add a probe to `bin/westly-health` (`probe_hands`).
5. MCP servers: put them in `.mcp.json` in this directory; chat turns load it automatically.

Google Calendar setup is walked through in `prompts/gcal-setup.md` — ask the assistant to walk you
through it, or follow it yourself.

## Safety

- **Tailnet only.** The app listens on a 0600 Unix socket and trusts the `Tailscale-User-Login`
  header that `tailscale serve` adds. Never expose it with `tailscale funnel`, a reverse proxy, or a
  public port: anyone who can reach the socket's HTTP endpoint with a forged header can drive the
  assistant. `PDA_LOGIN` restricts it to your own login.
- **The assistant has a full shell as your user** (`--permission-mode bypassPermissions`). It is told
  to read freely and ask before anything destructive or outward, but that is an instruction, not a
  sandbox. Run it under a dedicated user account if that matters to you, and keep secrets it shouldn't
  touch out of that account.
- **Untrusted content.** Email bodies, web pages and calendar invites are data, never instructions;
  `CLAUDE.md` and the prompts say so explicitly. Prompt injection is still possible — keep risky
  integrations (payments, sending mail) out, or behind a yes in chat.
- **Secrets** live only in `westly.env` and `gcal-sa.json` (both gitignored, mode 0600). Your
  memory repo is private: don't add a remote to it or push it anywhere.

## Development

Each module carries an offline self-check:

```sh
python3 config.py --selftest      python3 setup.py --selftest
python3 brief_scheduler.py --selftest
python3 tg.py && python3 sms.py   python3 bin/westly-ping --selftest
python3 bin/westly-health --selftest   bin/westly-run --selftest
.venv/bin/python gcal.py --selftest    .venv/bin/python mail_scan.py --selftest
.venv/bin/python pda.py --selftest     .venv/bin/python westly_bot.py --selftest
```

The app is one static page, `pda/index.html` (no build step). `pda.py` fills in the assistant's name
and the install settings as it serves it.

## License

MIT — see `LICENSE`.
