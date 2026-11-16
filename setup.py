#!/usr/bin/env python3
"""Westly setup wizard — run once after cloning, and again any time to change answers.

  python3 setup.py               interactive setup
  python3 setup.py --no-venv     skip creating .venv / installing requirements
  python3 setup.py --no-systemd  skip installing the systemd user units
  python3 setup.py --rerender    overwrite CLAUDE.md and prompts/ from templates/ (your edits are lost)
  python3 setup.py --selftest    offline checks of the pure helpers

It writes westly.env (mode 0600, gitignored), renders CLAUDE.md and prompts/ from
templates/, creates the virtualenv, installs the systemd user units, and prints how to
expose the app on your tailnet. Existing westly.env values are offered as defaults.
Stdlib only, so it runs before the virtualenv exists.
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import config  # noqa: E402

ENV = REPO / "westly.env"
EXAMPLE = REPO / "westly.env.example"
TEMPLATES = REPO / "templates"
UNITS_SRC = REPO / "systemd"
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
TIMERS = ["westly-briefcheck.timer", "westly-evening.timer", "westly-weekly.timer",
          "westly-finance.timer", "westly-health.timer"]


# ---------- pure helpers (selftested) ----------

def render(text, values):
    """Replace every {{KEY}} with values[KEY]; an unknown placeholder is an error, never left in."""
    def sub(m):
        if m.group(1) not in values:
            raise KeyError(f"no value for {{{{{m.group(1)}}}}}")
        return values[m.group(1)]
    return re.sub(r"\{\{([A-Z_]+)\}\}", sub, text)


def merge_env(example, values):
    """westly.env text: the example's layout and comments, with `values` filled in.
    Keys in `values` that the example lacks are appended at the end."""
    out, seen = [], set()
    for line in example.splitlines():
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    extra = [f"{k}={v}" for k, v in values.items() if k not in seen]
    if extra:
        out += ["", "# ---------- other settings ----------"] + extra
    return "\n".join(out) + "\n"


def clean_value(v):
    """One-line env value: no newlines or NULs, surrounding space trimmed."""
    return re.sub(r"[\r\n\x00]", "", str(v)).strip()


def check_tz(name):
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def check_login(v):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+", v))


def check_url(v):
    return v == "" or bool(re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?", v))


def check_coord(v, limit):
    if v == "":
        return True
    try:
        return -limit <= float(v) <= limit
    except ValueError:
        return False


def render_unit(text, repo, assistant):
    return text.replace("@REPO@", str(repo)).replace("@ASSISTANT@", assistant)


# ---------- prompting ----------

def ask(prompt, default="", check=None, why="", secret=False):
    while True:
        shown = f" [{'(kept)' if secret and default else default}]" if default else ""
        try:
            raw = (getpass.getpass(f"{prompt}{shown}: ") if secret else input(f"{prompt}{shown}: ")).strip()
        except EOFError:
            raw = ""
        value = raw or default
        if check is None or check(value):
            return clean_value(value)
        print(f"  {why or 'That does not look right'} — try again.")


def yes(prompt, default=True):
    raw = ask(f"{prompt} ({'Y/n' if default else 'y/N'})", "").lower()
    return default if not raw else raw.startswith("y")


def telegram_setup(token, chat):
    """Validate a bot token and find the owner's chat id. Network: Telegram's Bot API only."""
    import tg
    try:
        me = tg.api("getMe", token)
        print(f"  Telegram bot: @{me.get('username')}")
    except Exception as e:
        print(f"  Telegram rejected that token ({e}); leaving Telegram off.")
        return "", ""
    if chat:
        return token, chat
    print(f"  Now send any message to @{me.get('username')} from your own Telegram account.")
    for _ in range(12):
        try:
            found = tg.discover_chat(token)
        except Exception as e:
            print(f"  Couldn't check for messages ({e}).")
            found = {}
        if len(found) == 1:
            cid, who = next(iter(found.items()))
            print(f"  Found your chat: {who} ({cid}).")
            return token, cid
        if len(found) > 1:
            for cid, who in found.items():
                print(f"    {cid}  {who}")
            return token, ask("  Several chats messaged the bot. Which chat id is yours?", check=lambda v: v in found)
        time.sleep(5)
    print("  No message arrived. Set TELEGRAM_CHAT_ID in westly.env later, or run setup.py again.")
    return token, ""


def detect_public_url():
    """https://<this machine>.<tailnet>.ts.net:8443 from `tailscale status`, or ''."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        name = json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
        return f"https://{name}:8443" if name else ""
    except Exception:
        return ""


# ---------- steps ----------

def write_env(values):
    text = merge_env(EXAMPLE.read_text(), values)
    fd, tmp = tempfile.mkstemp(dir=REPO, prefix=".westly.env.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, ENV)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(ENV, 0o600)
    print(f"Wrote {ENV} (mode 600).")


def render_templates(values, force):
    targets = [(TEMPLATES / "CLAUDE.md", REPO / "CLAUDE.md")]
    targets += [(p, REPO / "prompts" / p.name) for p in sorted((TEMPLATES / "prompts").glob("*.md"))]
    (REPO / "prompts").mkdir(exist_ok=True)
    for src, dst in targets:
        if dst.exists() and not force:
            print(f"Kept your {dst.relative_to(REPO)} (--rerender overwrites it).")
            continue
        dst.write_text(render(src.read_text(), values))
        print(f"Rendered {dst.relative_to(REPO)}.")


def make_venv():
    venv = REPO / ".venv"
    if not (venv / "bin" / "python").exists():
        print("Creating .venv …")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    print("Installing requirements …")
    subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "-r", str(REPO / "requirements.txt")], check=True)


def install_units(values):
    if not SAFE_PATH.fullmatch(str(REPO)):
        print(f"Not installing systemd units: {REPO} contains characters systemd unit files would mangle.\n"
              "Move the checkout to a plain path (letters, digits, . _ - /) and run setup.py again.")
        return False
    dest = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    dest.mkdir(parents=True, exist_ok=True)
    mail = all(values.get(k) for k in ("MAIL_USER", "MAIL_PASSWORD", "MAIL_IMAP_HOST"))
    for unit in sorted(UNITS_SRC.iterdir()):
        if unit.name == "westly-mail.timer" and not mail:
            (dest / unit.name).unlink(missing_ok=True)
            continue
        (dest / unit.name).write_text(render_unit(unit.read_text(), REPO, values["ASSISTANT_NAME"]))
    print(f"Installed units into {dest}.")
    timers = TIMERS + (["westly-mail.timer"] if mail else [])
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", *timers], check=True)
        subprocess.run(["systemctl", "--user", "enable", "westly-bot.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "westly-bot.service"], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"systemctl failed ({e}). Units are on disk; enable them by hand:\n"
              f"  systemctl --user daemon-reload && systemctl --user enable --now westly-bot.service {' '.join(timers)}")
        return False
    print("Started westly-bot and enabled the timers.")
    return True


def welcome_note(assistant):
    outbox = REPO / "memory" / "outbox"
    (outbox / "sent").mkdir(parents=True, exist_ok=True)
    (REPO / "memory" / "journal").mkdir(parents=True, exist_ok=True)
    if not any((outbox / "sent").glob("*.md")):
        (outbox / "welcome.md").write_text(
            f"Welcome — this is {assistant}'s feed. Briefs, reports and alerts land here; "
            "anything that needs a decision shows up under Needs You.\n")


def main(args):
    print("Westly setup. Enter keeps the value in [brackets]; secrets are typed hidden.\n")
    cur = {k: v for k, v in (config.parse_env(ENV.read_text()) if ENV.exists() else {}).items()}
    v = dict(cur)
    name_ok = lambda s: bool(config.NAME_RE.fullmatch(s))  # noqa: E731
    name_why = "Use letters, digits, spaces or hyphens (max 32)"
    v["OWNER_NAME"] = ask("Your name", cur.get("OWNER_NAME", ""), name_ok, name_why)
    v["ASSISTANT_NAME"] = ask("Assistant's name", cur.get("ASSISTANT_NAME") or "Westly", name_ok, name_why)
    v["TIMEZONE"] = ask("Time zone (IANA name)", cur.get("TIMEZONE") or config.system_tz_name(), check_tz,
                        "Not a known time zone, e.g. America/Chicago or Europe/Berlin")

    print("\nThe phone app is served only on your tailnet (Tailscale), to one login.")
    v["PDA_LOGIN"] = ask("Your Tailscale login (e.g. you@example.com)", cur.get("PDA_LOGIN", ""), check_login,
                         "Expected the login Tailscale shows for you, like name@provider").lower()
    v["PUBLIC_URL"] = ask("App address", cur.get("PUBLIC_URL") or detect_public_url(), check_url,
                          "Expected https://host[:port] with no path").rstrip("/")

    print("\nChat channels: the app is always on.")
    choice = ask("Also use 1) app only  2) + Telegram  3) + Discord  4) + both",
                 "2" if cur.get("TELEGRAM_TOKEN") and not cur.get("DISCORD_TOKEN") else
                 "3" if cur.get("DISCORD_TOKEN") and not cur.get("TELEGRAM_TOKEN") else
                 "4" if cur.get("DISCORD_TOKEN") else "1", lambda s: s in ("1", "2", "3", "4"), "Pick 1-4")
    if choice in ("2", "4"):
        tok = ask("Telegram bot token (from @BotFather)", cur.get("TELEGRAM_TOKEN", ""), secret=True)
        v["TELEGRAM_TOKEN"], v["TELEGRAM_CHAT_ID"] = telegram_setup(tok, cur.get("TELEGRAM_CHAT_ID", "")) if tok else ("", "")
    else:
        v["TELEGRAM_TOKEN"], v["TELEGRAM_CHAT_ID"] = "", ""
    if choice in ("3", "4"):
        v["DISCORD_TOKEN"] = ask("Discord bot token", cur.get("DISCORD_TOKEN", ""), secret=True)
        v["OWNER_ID"] = ask("Your Discord user id (blank = learn it from the server)", cur.get("OWNER_ID", ""),
                            lambda s: s == "" or s.isdigit(), "Digits only")
    else:
        v["DISCORD_TOKEN"], v["OWNER_ID"] = "", ""

    print("\nWeather on the Today screen (optional, keyless Open-Meteo). Blank skips it.")
    v["WEATHER_LAT"] = ask("Latitude", cur.get("WEATHER_LAT", ""), lambda s: check_coord(s, 90), "-90 to 90")
    if v["WEATHER_LAT"]:
        v["WEATHER_LON"] = ask("Longitude", cur.get("WEATHER_LON", ""), lambda s: s != "" and check_coord(s, 180), "-180 to 180")
        v["WEATHER_PLACE"] = ask("Place name for the app", cur.get("WEATHER_PLACE", ""), lambda s: s == "" or name_ok(s), name_why)
        v["WEATHER_UNITS"] = ask("Units: imperial or metric", cur.get("WEATHER_UNITS") or "imperial",
                                 lambda s: s in ("imperial", "metric"), "imperial or metric")
    else:
        v["WEATHER_LON"] = ""

    write_env(v)
    values = {"OWNER_NAME": v["OWNER_NAME"], "ASSISTANT_NAME": v["ASSISTANT_NAME"],
              "ASSISTANT_NAME_LOWER": v["ASSISTANT_NAME"].lower(), "TIMEZONE": v["TIMEZONE"]}
    render_templates(values, args.rerender)
    welcome_note(v["ASSISTANT_NAME"])
    if not args.no_venv:
        make_venv()
    if not args.no_systemd and yes("\nInstall and start the systemd user units now?"):
        install_units(v)

    sock = REPO / ".pda.sock"
    print(f"""
Done. Next steps:

1. Log in to Claude on this machine (once):  {config.CLAUDE_BIN}   then type /login
   Scheduled runs and chat use that login; WESTLY_API_KEY in westly.env is an optional fallback.
2. Keep services running while you're logged out:  loginctl enable-linger $USER
3. Publish the app on your tailnet ONLY (never `tailscale funnel`, never a public port):
     sudo tailscale serve --bg --https=8443 unix:{sock}
   Then open {v['PUBLIC_URL'] or 'https://<this machine>.<tailnet>.ts.net:8443'} on your phone
   (signed in to Tailscale as {v['PDA_LOGIN']}) and use "Add to Home Screen".
4. Optional integrations (calendar, mail, SMS, Home Assistant): edit westly.env, see README.md,
   then  systemctl --user restart westly-bot  (and run setup.py again to add the mail timer).
5. Check everything:  bin/westly-health --report

The assistant runs with full shell access as {getpass.getuser()}. Keep the app tailnet-only.""")
    return 0


def selftest():
    assert render("Hi {{OWNER_NAME}}, I'm {{ASSISTANT_NAME}}.", {"OWNER_NAME": "Sam", "ASSISTANT_NAME": "Ada"}) == "Hi Sam, I'm Ada."
    try:
        render("{{NOPE}}", {})
        raise AssertionError("unknown placeholder must fail")
    except KeyError:
        pass
    assert render("{not} {{ a }}", {}) == "{not} {{ a }}"
    ex = "# comment\nA=\nB=old\n\nC=keep\n"
    assert merge_env(ex, {"A": "1", "B": "2", "Z": "9"}) == "# comment\nA=1\nB=2\n\nC=keep\n\n# ---------- other settings ----------\nZ=9\n"
    assert clean_value(" a\nb\r\x00 ") == "ab"
    assert check_tz("Europe/Berlin") and not check_tz("Mars/Base")
    assert check_login("sam@example.com") and check_login("sam@github") and not check_login("sam") and not check_login("a b@c")
    assert check_url("") and check_url("https://box.tail1234.ts.net:8443") and not check_url("http://x") and not check_url("https://x/y")
    assert check_coord("", 90) and check_coord("-33.9", 90) and not check_coord("91", 90) and not check_coord("x", 90)
    assert render_unit("WorkingDirectory=@REPO@\necho @ASSISTANT@", Path("/srv/w"), "Ada") == "WorkingDirectory=/srv/w\necho Ada"
    assert SAFE_PATH.fullmatch("/home/sam/westly") and not SAFE_PATH.fullmatch("/home/sam/my westly")
    # every template placeholder has a value at render time
    vals = {"OWNER_NAME": "Sam", "ASSISTANT_NAME": "Ada", "ASSISTANT_NAME_LOWER": "ada", "TIMEZONE": "UTC"}
    for t in [TEMPLATES / "CLAUDE.md", *sorted((TEMPLATES / "prompts").glob("*.md"))]:
        out = render(t.read_text(), vals)
        assert "{{" not in out, t
    # every example key is documented exactly once, and the example parses
    keys = re.findall(r"^([A-Z_]+)=", EXAMPLE.read_text(), re.M)
    assert len(keys) == len(set(keys)) and set(keys) == set(config.parse_env(EXAMPLE.read_text())), keys
    print("PASS")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Westly setup wizard")
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--no-systemd", action="store_true")
    ap.add_argument("--rerender", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    try:
        sys.exit(main(a))
    except KeyboardInterrupt:
        print("\nSetup cancelled; nothing after the last 'Wrote'/'Rendered' line was changed.")
        sys.exit(130)
