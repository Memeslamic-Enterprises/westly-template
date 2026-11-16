"""Westly's single config: westly.env (next to this file) plus the process environment.

Every module reads its settings from here. Values set in the real environment win;
westly.env fills anything unset or empty (systemd's EnvironmentFile covers services,
this covers manual runs). Self-check: python config.py --selftest
"""
import os
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO = Path(__file__).resolve().parent
ENV_FILE = REPO / "westly.env"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{0,31}")


def parse_env(text):
    """KEY=VALUE lines -> dict. '#' comments and blank lines skipped; one pair of
    matching surrounding quotes is stripped from a value."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            out[k] = v
    return out


def load_env(path=ENV_FILE):
    """Fill unset AND empty vars from westly.env; an inherited empty stub must not shadow it."""
    try:
        pairs = parse_env(path.read_text())
    except OSError:
        return
    for k, v in pairs.items():
        if not os.environ.get(k):
            os.environ[k] = v


def get(key, default=""):
    return os.environ.get(key, "").strip() or default


def flag(key, default=False):
    v = get(key).lower()
    return default if not v else v in ("1", "true", "yes", "on")


def number(key, default):
    try:
        return float(get(key)) if get(key) else default
    except ValueError:
        return default


def system_tz_name():
    """IANA name of the host's zone from /etc/localtime or /etc/timezone, else 'UTC'."""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return "UTC"


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def safe_name(value, default):
    """Names are pasted into the app's HTML and JS verbatim, so only a plain charset passes."""
    value = " ".join((value or "").split())
    return value if NAME_RE.fullmatch(value) else default


load_env()

OWNER_NAME = safe_name(get("OWNER_NAME"), "Owner")
ASSISTANT_NAME = safe_name(get("ASSISTANT_NAME"), "Westly")
TZ_NAME = get("TIMEZONE") or system_tz_name()
TZ = zone(TZ_NAME)
MODEL = get("WESTLY_MODEL", "sonnet")
CLAUDE_BIN = get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
PUBLIC_URL = get("PUBLIC_URL").rstrip("/")
PDA_LOGIN = get("PDA_LOGIN").lower()

WEATHER_LAT = number("WEATHER_LAT", None)
WEATHER_LON = number("WEATHER_LON", None)
WEATHER_PLACE = safe_name(get("WEATHER_PLACE"), "Here")
WEATHER_METRIC = get("WEATHER_UNITS").lower() == "metric"
WEATHER_NWS_ALERTS = flag("WEATHER_NWS_ALERTS")


def weather_on():
    return (WEATHER_LAT is not None and WEATHER_LON is not None
            and -90 <= WEATHER_LAT <= 90 and -180 <= WEATHER_LON <= 180)


def selftest():
    env = parse_env("# c\nA=1\n export B = two \nC='q v'\nD=\"x\"\nbad line\n9X=no\nE=a=b\n\nF=\n")
    assert env == {"A": "1", "B": "two", "C": "q v", "D": "x", "E": "a=b", "F": ""}, env
    assert safe_name("  Ada  Lovelace ", "x") == "Ada Lovelace"
    for bad in ("", "<b>", "a'b", "x" * 40, "-lead", "semi;colon"):
        assert safe_name(bad, "dflt") == "dflt", bad
    assert zone("Not/AZone").key == "UTC" and zone("America/New_York").key == "America/New_York"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.env"
        p.write_text("WESTLY_T1=file\nWESTLY_T2=file\nWESTLY_T3=file\n")
        os.environ.update(WESTLY_T1="real", WESTLY_T2="")
        os.environ.pop("WESTLY_T3", None)
        load_env(p)
        assert (os.environ["WESTLY_T1"], os.environ["WESTLY_T2"], os.environ["WESTLY_T3"]) == ("real", "file", "file")
        for k in ("WESTLY_T1", "WESTLY_T2", "WESTLY_T3"):
            os.environ.pop(k)
    print("PASS")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else 2)
