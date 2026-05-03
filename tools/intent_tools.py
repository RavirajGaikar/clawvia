"""Intent tools — drive other Android apps via `am start` / `am broadcast`.

All tools shell out to `am` (Activity Manager, available on any Android
device — it is a stock binary, not Termux:API). This is the cheapest,
most-compatible way to launch apps and trigger system actions without
root, an Accessibility service, or a companion APK.

Patterns we use:
  * `am start -a <ACTION> -d <DATA>` for VIEW-style intents (URLs, geo:, tel:)
  * `am start -n <package>/<activity>` for direct activity launches
  * `am start -t <mime> --es android.intent.extra.TEXT "..."` for share sheets

Sensitive actions (placing a call) are routed through the existing
`APPROVAL_REQUIRED:` flow.

Note: this is non-root, intent-based control only. We cannot tap UI
buttons or read the screen — those need an Accessibility service.
That ceiling is documented in the Phase 2 plan.
"""

import shlex
import subprocess
import urllib.parse

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.intent")

_CMD_TIMEOUT = 10


def _run(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=_CMD_TIMEOUT
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return None, err or f"Exit {result.returncode}"
        return (result.stdout or "").strip() or "OK", None
    except subprocess.TimeoutExpired:
        return None, "intent timed out"
    except FileNotFoundError:
        return None, "'am' not found — only available on Android (Termux)"
    except Exception as exc:
        return None, str(exc)


def _approval(cmd):
    return f"APPROVAL_REQUIRED: {cmd}"


def _quote(s):
    return shlex.quote(str(s))


# ── Generic intent firing ────────────────────────────────────────────────────


@registry.register(
    "intent_fire",
    "Fire an arbitrary Android intent via `am start`. "
    "action = e.g. android.intent.action.VIEW. "
    "data = URI like https://..., tel:..., geo:.... "
    "package = optional target package. "
    "extras = dict of string-typed extras (key→value).",
    {
        "action": "string",
        "data": "string?",
        "package": "string?",
        "type": "string?",
        "extras": "object?",
    },
)
def intent_fire(action=None, data=None, package=None, type=None, extras=None):
    if not action:
        return "ERROR: action is required"
    parts = ["am", "start", "-a", _quote(action)]
    if data:
        parts.extend(["-d", _quote(data)])
    if type:
        parts.extend(["-t", _quote(type)])
    if package:
        parts.extend(["-p", _quote(package)])
    if isinstance(extras, dict):
        for k, v in extras.items():
            parts.extend(["--es", _quote(str(k)), _quote(str(v))])
    out, err = _run(" ".join(parts))
    if err:
        return f"ERROR: {err}"
    return out


# ── Curated wrappers (preferred — clearer to the LLM) ────────────────────────


@registry.register(
    "app_open",
    "Launch an installed app by package name (e.g. com.spotify.music).",
    {"package": "string"},
)
def app_open(package=None):
    if not package:
        return "ERROR: package is required"
    cmd = (
        f"am start -n $(cmd package resolve-activity --brief {_quote(package)} "
        f"| tail -n1)"
    )
    out, err = _run(cmd)
    if err:
        # Fallback: monkey launcher (works even when no main activity is exposed
        # via `cmd package`, e.g. on older Android).
        out2, err2 = _run(
            f"monkey -p {_quote(package)} -c android.intent.category.LAUNCHER 1 "
            "2>&1 | tail -n3"
        )
        if err2:
            return f"ERROR: {err}"
        return out2
    return out or f"launched {package}"


@registry.register(
    "web_open",
    "Open a URL in the system default browser.",
    {"url": "string"},
)
def web_open(url=None):
    if not url:
        return "ERROR: url is required"
    cmd = f"am start -a android.intent.action.VIEW -d {_quote(url)}"
    out, err = _run(cmd)
    if err:
        return f"ERROR: {err}"
    return out or f"opened {url}"


@registry.register(
    "maps_navigate",
    "Open Maps and search for a place or address. Pulls up the system map app.",
    {"query": "string"},
)
def maps_navigate(query=None):
    if not query:
        return "ERROR: query is required"
    geo = "geo:0,0?q=" + urllib.parse.quote(str(query))
    return web_open(url=geo)


@registry.register(
    "dial_open",
    "Open the phone dialer with a number pre-filled — user still taps Call.",
    {"number": "string"},
)
def dial_open(number=None):
    if not number:
        return "ERROR: number is required"
    tel = "tel:" + urllib.parse.quote(str(number))
    return web_open(url=tel)


@registry.register(
    "sms_compose",
    "Open the SMS composer with recipient + body pre-filled "
    "(user still has to tap Send).",
    {"to": "string", "body": "string?"},
)
def sms_compose(to=None, body=None):
    if not to:
        return "ERROR: to is required"
    uri = "smsto:" + urllib.parse.quote(str(to))
    parts = ["am", "start", "-a", "android.intent.action.SENDTO", "-d", _quote(uri)]
    if body:
        parts.extend([
            "--es", "sms_body", _quote(str(body)),
        ])
    out, err = _run(" ".join(parts))
    if err:
        return f"ERROR: {err}"
    return out or f"composer opened for {to}"


@registry.register(
    "whatsapp_send",
    "Open WhatsApp with a chat pre-filled to a phone number "
    "(user taps Send). number must include country code, no '+'.",
    {"number": "string", "text": "string?"},
)
def whatsapp_send(number=None, text=None):
    if not number:
        return "ERROR: number is required"
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if not digits:
        return "ERROR: number must contain digits"
    url = f"https://wa.me/{digits}"
    if text:
        url += "?text=" + urllib.parse.quote(str(text))
    return web_open(url=url)


@registry.register(
    "share_text",
    "Open the system share sheet with the given text.",
    {"text": "string"},
)
def share_text(text=None):
    if not text:
        return "ERROR: text is required"
    cmd = (
        "am start -a android.intent.action.SEND -t text/plain "
        f"--es android.intent.extra.TEXT {_quote(text)}"
    )
    out, err = _run(cmd)
    if err:
        return f"ERROR: {err}"
    return out or "share sheet opened"


@registry.register(
    "share_file",
    "Open the system share sheet with a file (path on device storage). "
    "mime defaults to */*; pass 'image/*' or 'application/pdf' for richer pickers.",
    {"path": "string", "mime": "string?"},
)
def share_file(path=None, mime=None):
    if not path:
        return "ERROR: path is required"
    mime_type = mime or "*/*"
    # 'file://' URIs are blocked on modern Android; the user-facing "Share to"
    # path from Termux is termux-share which handles content:// for us.
    cmd = f"termux-share -a send -t {_quote(mime_type)} {_quote(path)}"
    out, err = _run(cmd)
    if err:
        return f"ERROR: {err}"
    return out or "share sheet opened"


@registry.register(
    "settings_open",
    "Open an Android Settings page. section examples: wifi, bluetooth, "
    "data_usage, battery, applications, sound, display, location, security, "
    "accessibility, notification.",
    {"section": "string?"},
)
def settings_open(section=None):
    sec = (section or "").strip().lower()
    action_map = {
        "": "android.settings.SETTINGS",
        "wifi": "android.settings.WIFI_SETTINGS",
        "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
        "data_usage": "android.settings.DATA_USAGE_SETTINGS",
        "data": "android.settings.DATA_USAGE_SETTINGS",
        "battery": "android.settings.BATTERY_SAVER_SETTINGS",
        "applications": "android.settings.APPLICATION_SETTINGS",
        "apps": "android.settings.APPLICATION_SETTINGS",
        "sound": "android.settings.SOUND_SETTINGS",
        "display": "android.settings.DISPLAY_SETTINGS",
        "location": "android.settings.LOCATION_SOURCE_SETTINGS",
        "security": "android.settings.SECURITY_SETTINGS",
        "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
        "notification": "android.settings.NOTIFICATION_SETTINGS",
        "airplane": "android.settings.AIRPLANE_MODE_SETTINGS",
        "date": "android.settings.DATE_SETTINGS",
        "language": "android.settings.LOCALE_SETTINGS",
    }
    action = action_map.get(sec)
    if not action:
        return f"ERROR: unknown section '{section}'. Try: {', '.join(sorted(k for k in action_map if k))}"
    out, err = _run(f"am start -a {action}")
    if err:
        return f"ERROR: {err}"
    return out or f"opened settings/{sec or 'home'}"


@registry.register(
    "app_list",
    "List installed app package names. filter is an optional substring.",
    {"filter": "string?"},
    cacheable=True,
)
def app_list(filter=None):
    out, err = _run("pm list packages -3")
    if err:
        out, err2 = _run("cmd package list packages -3")
        if err2:
            return f"ERROR: {err}"
    pkgs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):])
    if filter:
        f = filter.lower()
        pkgs = [p for p in pkgs if f in p.lower()]
    if not pkgs:
        return "(no packages matched)"
    pkgs.sort()
    head = pkgs[:80]
    out = f"{len(pkgs)} packages:\n" + "\n".join(f"- {p}" for p in head)
    if len(pkgs) > len(head):
        out += f"\n... (+{len(pkgs) - len(head)} more)"
    return out
