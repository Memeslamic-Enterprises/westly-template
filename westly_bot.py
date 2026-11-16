#!/usr/bin/env python3
"""Westly — chat gateway: the phone app (PDA), Telegram and Discord DMs, one session.

Owner messages run headless Claude Code in this directory with one shared session
(subscription first, WESTLY_API_KEY as an optional fallback once it is out or logged
out). Files appearing in memory/outbox/ are filed into outbox/sent/, which the app
shows as its feed — nothing here pings the owner; that is bin/westly-ping's job.
Owner-only by design. Telegram and Discord are optional; the app always runs.
"""
import asyncio
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import discord

import config
import pda
import tg

REPO = config.REPO
NAME = config.ASSISTANT_NAME
PING = REPO / "bin" / "westly-ping"
SESSION_FILE = REPO / ".session"
OWNER_FILE = REPO / ".owner"
LOCK_FILE = REPO / ".claude-run.lock"
OUTBOX = REPO / "memory" / "outbox"
SENT = OUTBOX / "sent"
CHUNK = 1900
ALERT_DUP_WINDOW = 4 * 3600  # identical attn-* muted this long; flapping monitors repeat ~hourly
CLAUDE_TIMEOUT = 600
TG_RETRY = 5         # long polling returns on its own; this is only the error path
TG_RETRY_MAX = 300
MCP_CONFIG = REPO / ".mcp.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("westly")


DEFAULT_MODEL = config.MODEL


def owner_id():
    if os.environ.get("OWNER_ID"):
        return int(os.environ["OWNER_ID"])
    if OWNER_FILE.exists():
        return int(OWNER_FILE.read_text().strip())
    return None


def ping_owner(msg):
    try:
        subprocess.run([str(PING), msg], timeout=60, check=False)
    except Exception as e:
        log.error("westly-ping failed: %s", e)


_last_flag = {}


def ping_ratelimited(key, msg, min_gap=3600):
    now = time.time()
    if now - _last_flag.get(key, 0) >= min_gap:
        _last_flag[key] = now
        ping_owner(msg)


def _invoke_claude(text, model, session_args):
    cmd = [config.CLAUDE_BIN, "-p", text, "--model", model, "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           "--setting-sources", "project,local", "--add-dir", str(REPO)]
    if MCP_CONFIG.exists():  # optional MCP servers for chat turns
        cmd += ["--mcp-config", str(MCP_CONFIG)]
    cmd += session_args
    res = _run_cli(cmd, None)
    ok = res[0] == 0 and not res[1]
    auth = not ok and pda.cli_auth_failed(res[2])  # a good reply may say "not logged in" about anything
    if auth or ok:
        pda.note_auth(auth)
    key = os.environ.get("WESTLY_API_KEY")
    if key and not ok and (auth or limit_notice(res[2])):
        # Max meter exhausted or login expired: same turn again, billed to the API key instead
        log.warning("%s, retrying on API key: %s", "Login expired" if auth else "Max limit hit", res[2][:120])
        res = _run_cli(cmd, {**os.environ, "ANTHROPIC_API_KEY": key})
    return res


def _run_cli(cmd, env):
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env,
                       timeout=CLAUDE_TIMEOUT, stdin=subprocess.DEVNULL)
    out = r.stdout.strip()
    try:
        env = json.loads(out)
        return r.returncode, env.get("is_error", False), env.get("result", "")
    except json.JSONDecodeError:
        return r.returncode, True, (out or r.stderr)[-1500:]


LIMIT_MARKERS = ("hit your session limit", "hit your usage limit", "usage limit reached")


def limit_notice(result):
    """A usage-limit refusal from the CLI, rephrased for the owner — None for anything else.
    The raw meter string reads like the assistant being broken."""
    low = (result or "").lower()
    if any(m in low for m in LIMIT_MARKERS):
        return (f"⏳ Out of Claude capacity right now ({(result or '').strip()}). "
                "This message didn't process — resend it after the reset.")
    return None


def claude_chat(text, model, on_locked=None):
    """Runs one chat turn under the workspace lock. Sync — call via to_thread.
    on_locked() fires once the lock is held (the PDA's queued -> running)."""
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if on_locked:
            on_locked()
        prefix = ""
        if SESSION_FILE.exists():
            sid = SESSION_FILE.read_text().strip()
            rc, err, result = _invoke_claude(text, model, ["--resume", sid])
            if rc == 0 and not err:
                return result
            notice = limit_notice(result)
            if notice:
                return notice  # a fresh session would hit the same meter; keep continuity
            log.warning("resume of %s failed (rc=%s), starting fresh", sid, rc)
            prefix = "(started fresh) "
        sid = str(uuid.uuid4())
        rc, err, result = _invoke_claude(text, model, ["--session-id", sid])
        if rc == 0 and not err:
            SESSION_FILE.write_text(sid)
            return prefix + result
        notice = limit_notice(result)
        if notice:
            return notice
        raise RuntimeError(f"claude failed rc={rc}: {result[-800:]}")


def backoff(fails, base=TG_RETRY, ceiling=TG_RETRY_MAX):
    """Poll delay after `fails` consecutive errors — a refusing API refuses harder if you keep knocking."""
    return min(base * 2 ** min(fails, 8), ceiling)


def prep_turn(text):
    """(model, prompt, control_reply) for one owner message — shared by every channel."""
    text = text.strip()
    if not text:
        return None, None, None
    if text.lower() == "/start":
        # Telegram fires this on opening the bot; answering it with a Claude turn is waste
        return None, None, f"{NAME} here. Just talk to me normally — same memory as the app."
    if text.lower() == "new topic":
        SESSION_FILE.unlink(missing_ok=True)
        return None, None, "Fresh slate. What's up?"
    model = DEFAULT_MODEL
    if text.lower().startswith("opus:"):
        model, text = "opus", text[5:].strip()
    return (model, text, None) if text else (None, None, None)


def chunks(text):
    text = text.strip() or "(empty reply)"
    while text:
        if len(text) <= CHUNK:
            yield text
            return
        cut = text.rfind("\n", 0, CHUNK)
        if cut < CHUNK // 2:
            cut = CHUNK
        yield text[:cut]
        text = text[cut:].lstrip("\n")


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)


async def send_chunked(dest, text):
    for c in chunks(text):
        await dest.send(c)


async def resolve_owner():
    """Lazy owner resolution: the sole human sharing a guild with the bot."""
    oid = owner_id()
    if oid:
        return oid
    humans = {}
    for g in bot.guilds:
        try:
            if not g.chunked:
                await g.chunk()
        except Exception:
            pass
        for m in g.members:
            if not m.bot:
                humans[m.id] = str(m)
    if len(humans) == 1:
        oid = next(iter(humans))
        OWNER_FILE.write_text(str(oid))
        log.info("owner resolved from guild membership: %s", humans[oid])
        return oid
    if len(humans) > 1:
        ping_ratelimited("multi-owner",
                         f"{NAME}: several people share its Discord server - set OWNER_ID in westly.env")
    return None


def is_dup_alert(name, text, now, sent):
    """True when this attn-* outbox file repeats an alert already delivered within
    ALERT_DUP_WINDOW — a flapping monitor, not news. Anything else always files.
    `sent` iterates (mtime, name, text) of delivered alerts; pure, covered by --selftest."""
    if not name.startswith("attn-"):
        return False
    return any("attn-" in n and mt >= now - ALERT_DUP_WINDOW and t == text for mt, n, t in sent)


def _recent_alerts(window=ALERT_DUP_WINDOW):
    """(mtime, name, text) of alerts filed within `window`; dup- archives excluded so the
    mute clock runs from the last DELIVERED copy, not the last flap."""
    cutoff = time.time() - window
    out = []
    for p in SENT.glob("*attn-*.md"):
        if "dup-" in p.name:
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                out.append((p.stat().st_mtime, p.name, p.read_text()))
        except OSError:
            continue
    return out


def file_outbox():
    """File each outbox message into sent/ — the PDA feed. Repeat alerts are archived
    as dup-* and stay out of the feed. Messages nobody; pings go through bin/westly-ping."""
    SENT.mkdir(parents=True, exist_ok=True)
    for f in sorted(p for p in OUTBOX.glob("*.md") if p.is_file()):
        text = f.read_text()
        dup = is_dup_alert(f.name, text, time.time(), _recent_alerts())
        name = f"dup-{f.name}" if dup else f.name
        dest = SENT / name
        if dest.exists():
            dest = SENT / f"{int(time.time())}-{name}"
        shutil.move(str(f), str(dest))
        log.info("outbox %s: %s", "duplicate suppressed" if dup else "filed", f.name)


async def outbox_loop():
    while True:
        try:
            await asyncio.to_thread(file_outbox)
        except Exception:
            log.exception("outbox loop error")
        await asyncio.sleep(20)


def pda_turn(text, on_running=None):
    """One chat turn from the PDA app — same prep and session as a DM."""
    model, prompt, control = prep_turn(text)
    return control or (claude_chat(prompt, model, on_running) if prompt else "(empty message)")


async def tg_typing(stop):
    """Hold Telegram's 'typing…' up for the length of a turn; it expires after ~5s.
    Without it a slow turn looks like nothing happened, and the owner resends."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(tg.typing)
        except Exception:
            return  # cosmetic only; never let this interfere with the turn
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def tg_loop():
    """The owner's Telegram messages, answered like any chat: same session, same lock.
    Long-polled, so a reply lands in seconds with no interval to tune."""
    fails = 0
    while True:
        try:
            texts = await asyncio.to_thread(tg.poll, None, None, tg.POLL, log)
            fails = 0
        except Exception as e:
            fails += 1
            log.warning("telegram poll failed (%d in a row): %s", fails, e)
            texts = []
            await asyncio.sleep(backoff(fails, TG_RETRY, TG_RETRY_MAX))
        for text in texts:
            try:
                model, prompt, control = prep_turn(text)
                reply = control
                if not reply:
                    if not prompt:
                        continue
                    log.info("telegram turn: %.60s", prompt)
                    stop = asyncio.Event()
                    typer = asyncio.create_task(tg_typing(stop))
                    try:
                        reply = await asyncio.to_thread(claude_chat, prompt, model)
                    finally:
                        stop.set()
                        await typer
                await asyncio.to_thread(tg.send, reply)
            except subprocess.TimeoutExpired:
                log.exception("telegram turn timed out")
                await asyncio.to_thread(
                    tg.send, f"That ran past my {CLAUDE_TIMEOUT // 60}-minute limit and I "
                    "killed it. Send it again to retry, or 'new topic' to reset.")
            except Exception as e:
                log.exception("telegram turn failed")
                try:
                    await asyncio.to_thread(tg.send, f"That broke on my end: {e}"[:1900])
                except Exception:
                    log.exception("telegram error reply failed too")


@bot.event
async def on_ready():
    log.info("discord: logged in as %s (%s) in %d guild(s)", bot.user, bot.user.id, len(bot.guilds))
    await resolve_owner()


@bot.event
async def on_raw_reaction_add(payload):
    # owner's DM reactions only — guild events fire too, and a server may hold other people
    if payload.guild_id is not None or payload.user_id != owner_id():
        return
    try:
        chan = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        msg = await chan.fetch_message(payload.message_id)
        if msg.author.id != bot.user.id:
            return
        snippet = " ".join(msg.content.split())[:80]
        jf = REPO / "memory" / "journal" / f"{time.strftime('%Y-%m')}.md"
        with open(jf, "a") as f:
            f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} feedback: {payload.emoji} "
                    f"on \"{snippet}\"\n")
        log.info("feedback journaled: %s", payload.emoji)
    except Exception:
        log.exception("reaction journal failed")


@bot.event
async def on_message(message):
    if message.guild is not None or message.author.bot:
        return
    oid = owner_id() or await resolve_owner()
    if oid is None or message.author.id != oid:
        log.warning("ignored DM from non-owner %s", message.author.id)
        return
    text = message.content.strip()
    if not text:
        return
    model, prompt, control = prep_turn(text)
    if control:
        await message.channel.send(control)
        return
    if not prompt:
        return
    try:
        async with message.channel.typing():
            reply = await asyncio.to_thread(claude_chat, prompt, model)
        await send_chunked(message.channel, reply)
    except subprocess.TimeoutExpired:
        log.exception("chat turn timed out")
        await message.channel.send(
            f"That ran past my {CLAUDE_TIMEOUT // 60}-minute limit and I had to kill it "
            "mid-task. Re-send to retry, or 'new topic' for a fresh session.")
    except Exception as e:
        log.exception("chat turn failed")
        await message.channel.send(f"That broke on my end: {e}"[:1900])


def selftest():
    # chunks: pass-through, empty sentinel, newline-preferred splits, hard cap
    assert list(chunks("hi")) == ["hi"]
    assert list(chunks("  ")) == ["(empty reply)"]
    text = "\n".join(["x" * 80] * 40)
    parts = list(chunks(text))
    assert all(len(p) <= CHUNK for p in parts) and "\n".join(parts) == text, parts
    blob = "y" * 4000  # no newline to split on -> hard cuts at CHUNK
    parts = list(chunks(blob))
    assert [len(p) for p in parts] == [1900, 1900, 200] and "".join(parts) == blob
    # duplicate-alert suppression: same attn text within the window is muted;
    # different text, expired window, or a non-alert outbox file always sends
    now, alert = 1_000_000.0, "⚠️ storefront CRITICAL: 503"
    sent = [(now - 300, "attn-1.md", alert)]
    assert is_dup_alert("attn-2.md", alert, now, sent) is True
    assert is_dup_alert("attn-2.md", "⚠️ other alert", now, sent) is False
    assert is_dup_alert("attn-2.md", alert, now, [(now - 3800, "attn-1.md", alert)]) is True  # hourly flap
    assert is_dup_alert("attn-2.md", alert, now, [(now - ALERT_DUP_WINDOW - 1, "attn-1.md", alert)]) is False
    assert is_dup_alert("brief-2026-08-17.md", alert, now, sent) is False
    assert is_dup_alert("attn-2.md", alert, now, []) is False
    # collision archives ('<ts>-attn-…') still count as delivered copies
    assert is_dup_alert("attn-2.md", alert, now, [(now - 300, "999-attn-1.md", alert)]) is True
    assert is_dup_alert("mail-2.md", alert, now, sent) is False
    # backoff: steady cadence while healthy, retreats to the ceiling under refusal
    assert backoff(0) == TG_RETRY
    assert backoff(1) == TG_RETRY * 2
    assert backoff(3) == min(TG_RETRY * 8, TG_RETRY_MAX)
    assert backoff(99) == TG_RETRY_MAX
    assert backoff(0, 10, 30) == 10 and backoff(5, 10, 30) == 30

    # limit_notice: the meter string becomes a plain reply, anything else stays an error
    n = limit_notice("You've hit your session limit · resets 4:50am (America/Los_Angeles)")
    assert n and n.startswith("⏳") and "resets 4:50am" in n, n
    assert limit_notice("Execution error: something else") is None
    assert limit_notice("") is None and limit_notice(None) is None

    # _invoke_claude: a Max limit refusal reruns the turn on WESTLY_API_KEY, once
    global _run_cli
    real_run, calls = _run_cli, []

    def _run_cli(cmd, env):
        calls.append(env)
        return (1, True, "You've hit your usage limit") if len(calls) == 1 else (0, False, "ok")
    os.environ["WESTLY_API_KEY"] = "sk-test"
    real_flag = pda.AUTH_FLAG
    pda.AUTH_FLAG = Path(tempfile.mkdtemp()) / "auth-expired"
    try:
        assert _invoke_claude("hi", "sonnet", []) == (0, False, "ok")
        assert calls[0] is None and calls[1]["ANTHROPIC_API_KEY"] == "sk-test"
        assert not pda.AUTH_FLAG.exists()
        # an expired login also falls back, and leaves the flag westly-health pings on
        calls.clear()
        _run_cli = lambda cmd, env: calls.append(env) or (
            (1, True, "Failed to authenticate: OAuth session expired and could not be refreshed")
            if len(calls) == 1 else (0, False, "ok"))
        assert _invoke_claude("hi", "sonnet", []) == (0, False, "ok")
        assert calls[1]["ANTHROPIC_API_KEY"] == "sk-test" and pda.AUTH_FLAG.exists()
        # a later subscription success clears it, even when the reply itself mentions logins
        calls.clear()
        _run_cli = lambda cmd, env: calls.append(env) or (0, False, "the store says you're not logged in")
        assert _invoke_claude("hi", "sonnet", [])[1] is False and len(calls) == 1
        assert not pda.AUTH_FLAG.exists()
    finally:
        _run_cli = real_run
        pda.AUTH_FLAG = real_flag
        del os.environ["WESTLY_API_KEY"]

    # prep_turn: shared by the DM and SMS front ends, so both inherit these
    assert prep_turn("  hey  ") == (DEFAULT_MODEL, "hey", None)
    assert prep_turn("opus: dig into this") == ("opus", "dig into this", None)
    assert prep_turn("OPUS:  x") == ("opus", "x", None)
    assert prep_turn("") == (None, None, None)
    assert prep_turn("   ") == (None, None, None)
    assert prep_turn("opus:") == (None, None, None)  # no prompt left -> no turn
    assert prep_turn("/start")[2].startswith(NAME + " here")
    assert prep_turn("/START")[1] is None  # never reaches claude_chat
    global SESSION_FILE
    real, SESSION_FILE = SESSION_FILE, Path("/tmp/.westly-selftest-session")
    try:
        SESSION_FILE.write_text("abc")
        assert prep_turn("New Topic")[2] == "Fresh slate. What's up?"
        assert not SESSION_FILE.exists()
        assert prep_turn("new topic")[2]  # idempotent when already gone
    finally:
        SESSION_FILE.unlink(missing_ok=True)
        SESSION_FILE = real

    # file_outbox: everything moves to sent/ (the feed), repeats archive as dup-*,
    # and nothing is sent anywhere — there is no delivery call left to make
    global OUTBOX, SENT
    real_o, real_s = OUTBOX, SENT
    with tempfile.TemporaryDirectory() as tmp:
        OUTBOX = Path(tmp)
        SENT = OUTBOX / "sent"
        try:
            (OUTBOX / "brief-x.md").write_text("morning")
            (OUTBOX / "attn-1.md").write_text(alert)
            file_outbox()
            (OUTBOX / "attn-2.md").write_text(alert)
            (OUTBOX / "notes.txt").write_text("not an outbox message")
            file_outbox()
            assert sorted(p.name for p in SENT.iterdir()) == \
                ["attn-1.md", "brief-x.md", "dup-attn-2.md"], list(SENT.iterdir())
            assert (OUTBOX / "notes.txt").exists() and not list(OUTBOX.glob("*.md"))
        finally:
            OUTBOX, SENT = real_o, real_s
    print("PASS")
    return 0


async def main():
    # outbox filing + the app run independent of the chat networks: an outage there
    # must not blank the app or let the outbox pile up
    asyncio.create_task(outbox_loop())
    await pda.serve(pda_turn)
    jobs = []
    got = tg.creds()
    if got and got[1]:
        jobs.append(tg_loop())
    elif got:
        log.error("TELEGRAM_CHAT_ID unset: run setup.py again (it finds the id), telegram stays idle")
    token = config.get("DISCORD_TOKEN")
    if token:
        jobs.append(bot.start(token))
    log.info("channels: app%s%s", " + telegram" if got and got[1] else "", " + discord" if token else "")
    await asyncio.gather(*jobs, asyncio.Event().wait())


if __name__ == "__main__":  # importing this module must never start a second bot (10/24 review incident)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    asyncio.run(main())
