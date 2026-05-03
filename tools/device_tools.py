"""Device tools — Termux:API integration with Android shell fallbacks.

Most tools try the Termux:API command first, then fall back to
plain Android shell commands (dumpsys, content query, settings, etc.)
when the Termux:API Android app is unavailable (common on the Google
Play version of Termux).
"""

import json
import re
import shlex
import subprocess

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.device")

_CMD_TIMEOUT = 10


def _run_termux_cmd(cmd, timeout=_CMD_TIMEOUT):
    """Run a Termux:API command and return (stdout, error)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if "not found" in err.lower() or "no such" in err.lower():
                return None, "Termux:API not available. Install with: pkg install termux-api"
            return None, err or f"Command exited with code {result.returncode}"
        return (result.stdout or "").strip(), None
    except subprocess.TimeoutExpired:
        return None, "Command timed out"
    except FileNotFoundError:
        return None, "Termux:API not available"
    except Exception as exc:
        return None, str(exc)


def _parse_content_query(output):
    """Parse Android `content query` output into a list of dicts.

    Each row looks like:
        Row: 0 address=+1234567, body=Hello there, date=1714500000000
    """
    rows = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Strip "Row: N " prefix
        payload = re.sub(r"^Row:\s*\d+\s*", "", line)
        row = {}
        # Split on ", key=" boundaries (values may contain commas)
        parts = re.split(r",\s+(?=\w+=)", payload)
        for part in parts:
            eq = part.find("=")
            if eq > 0:
                k = part[:eq].strip()
                v = part[eq + 1:].strip()
                if v == "NULL":
                    v = ""
                row[k] = v
        if row:
            rows.append(row)
    return rows


# ── Battery / Storage / Network / Info ───────────────────────────────────────


@registry.register(
    "device_battery",
    "Get device battery status (level, charging state, temperature).",
    {},
    cacheable=True,
)
def device_battery():
    output, err = _run_termux_cmd("termux-battery-status")
    if err:
        return f"ERROR: {err}"
    try:
        data = json.loads(output)
        return (
            f"Battery: {data.get('percentage', '?')}%\n"
            f"Status: {data.get('status', '?')}\n"
            f"Plugged: {data.get('plugged', '?')}\n"
            f"Temperature: {data.get('temperature', '?')}°C"
        )
    except (json.JSONDecodeError, KeyError):
        return output or "ERROR: Could not parse battery info"


@registry.register(
    "device_storage",
    "Get device storage usage information.",
    {},
    cacheable=True,
)
def device_storage():
    output, err = _run_termux_cmd("df -h /storage/emulated/0 /data 2>/dev/null || df -h")
    if err:
        return f"ERROR: {err}"
    return output or "No storage info available"


@registry.register(
    "device_network",
    "Get current network information (IP, location, ISP, and local interfaces).",
    {},
    cacheable=True,
)
def device_network():
    # Try Termux:API first
    output, err = _run_termux_cmd("termux-wifi-connectioninfo")
    if not err:
        try:
            data = json.loads(output)
            return (
                f"SSID: {data.get('ssid', '?')}\n"
                f"BSSID: {data.get('bssid', '?')}\n"
                f"IP: {data.get('ip', '?')}\n"
                f"Link Speed: {data.get('link_speed_mbps', '?')} Mbps\n"
                f"Signal: {data.get('rssi', '?')} dBm\n"
                f"Frequency: {data.get('frequency_mhz', '?')} MHz"
            )
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: public IP info via curl
    output2, err2 = _run_termux_cmd("curl -s --max-time 5 ipinfo.io")
    if not err2 and output2:
        try:
            data = json.loads(output2)
            parts = [
                f"IP: {data.get('ip', '?')}",
                f"City: {data.get('city', '?')}, {data.get('region', '?')}",
                f"Country: {data.get('country', '?')}",
                f"ISP: {data.get('org', '?')}",
            ]
            return "\n".join(parts)
        except (json.JSONDecodeError, KeyError):
            return output2

    # Last resort: ifconfig
    output3, err3 = _run_termux_cmd("ifconfig 2>/dev/null || cat /proc/net/if_inet6 2>/dev/null")
    if not err3 and output3:
        return output3

    return "ERROR: Could not retrieve network info (Termux:API unavailable, no internet)"


@registry.register(
    "device_info",
    "Get combined device information: battery, storage, and network.",
    {},
    cacheable=True,
)
def device_info():
    sections = []
    sections.append("== Battery ==")
    sections.append(device_battery())
    sections.append("\n== Storage ==")
    sections.append(device_storage())
    sections.append("\n== Network ==")
    sections.append(device_network())
    return "\n".join(sections)




# ── SMS / Calls / Contacts ───────────────────────────────────────────────────


@registry.register(
    "sms_inbox",
    "Read recent SMS messages. type: inbox|sent|draft|outbox. limit defaults to 10.",
    {"type": "string?", "limit": "integer?"},
    cacheable=True,
)
def sms_inbox(type="inbox", limit=10):
    box = (type or "inbox").lower()
    if box not in {"inbox", "sent", "draft", "outbox"}:
        return "ERROR: type must be inbox|sent|draft|outbox"
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(50, n))

    # Try Termux:API first
    output, err = _run_termux_cmd(f"termux-sms-list -t {box} -l {n}")
    if not err:
        try:
            msgs = json.loads(output)
            if msgs:
                lines = [f"{len(msgs)} messages ({box}):"]
                for m in msgs:
                    body = (m.get("body") or "").replace("\n", " ")[:120]
                    lines.append(
                        f"- [{m.get('received', '?')}] {m.get('number', '?')}  ({m.get('type','?')})\n  {body}"
                    )
                return "\n".join(lines)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: Android content query
    log.info("sms_inbox: Termux:API failed, trying content query fallback")
    uri = f"content://sms/{box}"
    cmd = (
        f'content query --uri {uri}'
        f' --projection address:body:date'
        f' --sort "date DESC LIMIT {n}"'
    )
    output2, err2 = _run_termux_cmd(cmd, timeout=15)
    if err2:
        return f"ERROR: {err} (fallback also failed: {err2})"
    rows = _parse_content_query(output2)
    if not rows:
        return "(no messages)"
    lines = [f"{len(rows)} messages ({box}):"]
    for r in rows:
        body = (r.get("body") or "").replace("\n", " ")[:120]
        # date is epoch millis
        date_str = r.get("date", "?")
        try:
            from datetime import datetime
            date_str = datetime.fromtimestamp(int(date_str) / 1000).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OSError):
            pass
        lines.append(f"- [{date_str}] {r.get('address', '?')}\n  {body}")
    return "\n".join(lines)


@registry.register(
    "call_log",
    "Read recent call history. limit defaults to 10.",
    {"limit": "integer?"},
    cacheable=True,
)
def call_log(limit=10):
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(50, n))

    # Try Termux:API first
    output, err = _run_termux_cmd(f"termux-call-log -l {n}")
    if not err:
        try:
            calls = json.loads(output)
            if calls:
                lines = [f"{len(calls)} calls:"]
                for c in calls:
                    lines.append(
                        f"- [{c.get('date', '?')}] {c.get('type','?'):<8} "
                        f"{c.get('name') or c.get('phone_number', '?')}  ({c.get('duration', '?')}s)"
                    )
                return "\n".join(lines)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: Android content query
    log.info("call_log: Termux:API failed, trying content query fallback")
    cmd = (
        f'content query --uri content://call_log/calls'
        f' --projection number:type:date:duration'
        f' --sort "date DESC LIMIT {n}"'
    )
    output2, err2 = _run_termux_cmd(cmd, timeout=15)
    if err2:
        return f"ERROR: {err} (fallback also failed: {err2})"
    rows = _parse_content_query(output2)
    if not rows:
        return "(no calls)"
    # type: 1=incoming, 2=outgoing, 3=missed
    type_map = {"1": "incoming", "2": "outgoing", "3": "missed", "4": "voicemail", "5": "rejected"}
    lines = [f"{len(rows)} calls:"]
    for r in rows:
        date_str = r.get("date", "?")
        try:
            from datetime import datetime
            date_str = datetime.fromtimestamp(int(date_str) / 1000).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OSError):
            pass
        ctype = type_map.get(r.get("type", ""), r.get("type", "?"))
        dur = r.get("duration", "?")
        lines.append(f"- [{date_str}] {ctype:<8} {r.get('number', '?')}  ({dur}s)")
    return "\n".join(lines)


@registry.register(
    "contacts_query",
    "List contacts whose name or number matches a substring. Pass empty query to list all.",
    {"query": "string?"},
    cacheable=True,
)
def contacts_query(query=""):
    # Try Termux:API first
    output, err = _run_termux_cmd("termux-contact-list", timeout=15)
    if not err:
        try:
            contacts = json.loads(output)
            q = (query or "").strip().lower()
            if q:
                contacts = [
                    c for c in contacts
                    if q in (c.get("name") or "").lower()
                    or q in (c.get("number") or "")
                ]
            if contacts:
                lines = [f"{len(contacts)} contacts:"]
                for c in contacts[:50]:
                    lines.append(f"- {c.get('name', '?'):<25} {c.get('number', '?')}")
                if len(contacts) > 50:
                    lines.append(f"... (+{len(contacts) - 50} more)")
                return "\n".join(lines)
            return "(no contacts matched)"
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: Android content query
    log.info("contacts_query: Termux:API failed, trying content query fallback")
    cmd = (
        "content query --uri content://com.android.contacts/data"
        " --projection display_name:data1"
        " --where \"mimetype='vnd.android.cursor.item/phone_v2'\""
    )
    output2, err2 = _run_termux_cmd(cmd, timeout=15)
    if err2:
        return f"ERROR: {err} (fallback also failed: {err2})"
    rows = _parse_content_query(output2)
    if not rows:
        return "(no contacts)"
    q = (query or "").strip().lower()
    if q:
        rows = [
            r for r in rows
            if q in (r.get("display_name") or "").lower()
            or q in (r.get("data1") or "")
        ]
    if not rows:
        return "(no contacts matched)"
    lines = [f"{len(rows)} contacts:"]
    for r in rows[:50]:
        lines.append(f"- {r.get('display_name', '?'):<25} {r.get('data1', '?')}")
    if len(rows) > 50:
        lines.append(f"... (+{len(rows) - 50} more)")
    return "\n".join(lines)



# ── Clipboard ────────────────────────────────────────────────────────────────


@registry.register(
    "clipboard_get",
    "Read the system clipboard contents.",
    {},
)
def clipboard_get():
    output, err = _run_termux_cmd("termux-clipboard-get")
    if err:
        return f"ERROR: {err}"
    return output or "(clipboard empty)"


@registry.register(
    "clipboard_set",
    "Write text to the system clipboard.",
    {"text": "string"},
)
def clipboard_set(text=None):
    if text is None:
        return "ERROR: text is required"
    cmd = f"echo {shlex.quote(str(text))} | termux-clipboard-set"
    output, err = _run_termux_cmd(cmd)
    if err:
        return f"ERROR: {err}"
    return f"clipboard set ({len(str(text))} chars)"


# ── Notifications, dialogs, volume, brightness ───────────────────────────────


@registry.register(
    "notification_send",
    "Show an Android notification. id lets you update/dismiss the same one later.",
    {"title": "string", "content": "string?", "id": "integer?"},
)
def notification_send(title=None, content=None, id=None):
    if not title:
        return "ERROR: title is required"
    parts = ["termux-notification", "-t", shlex.quote(str(title))]
    if content:
        parts.extend(["-c", shlex.quote(str(content))])
    if id is not None:
        try:
            parts.extend(["-i", str(int(id))])
        except (TypeError, ValueError):
            pass
    output, err = _run_termux_cmd(" ".join(parts))
    if err:
        return f"ERROR: {err}"
    return "notification sent"


@registry.register(
    "notification_remove",
    "Dismiss a previously-sent notification by its id.",
    {"id": "integer"},
)
def notification_remove(id=None):
    if id is None:
        return "ERROR: id is required"
    try:
        nid = int(id)
    except (TypeError, ValueError):
        return f"ERROR: bad id: {id!r}"
    output, err = _run_termux_cmd(f"termux-notification-remove {nid}")
    if err:
        return f"ERROR: {err}"
    return f"notification {nid} removed"


@registry.register(
    "dialog_input",
    "Show a text-input dialog and return what the user typed. "
    "kind: text|password|number (default text).",
    {"title": "string?", "hint": "string?", "kind": "string?"},
)
def dialog_input(title=None, hint=None, kind="text"):
    flag = {"text": "text", "password": "password", "number": "number"}.get(
        (kind or "text").lower(), "text"
    )
    parts = ["termux-dialog", flag]
    if title:
        parts.extend(["-t", shlex.quote(str(title))])
    if hint:
        parts.extend(["-i", shlex.quote(str(hint))])
    output, err = _run_termux_cmd(" ".join(parts), timeout=300)
    if err:
        return f"ERROR: {err}"
    try:
        data = json.loads(output)
        if data.get("code") != -1:
            return f"(cancelled, code={data.get('code')})"
        return data.get("text", "")
    except (json.JSONDecodeError, ValueError):
        return output


@registry.register(
    "dialog_confirm",
    "Show a yes/no confirm dialog. Returns 'yes' or 'no'.",
    {"title": "string?", "hint": "string?"},
)
def dialog_confirm(title=None, hint=None):
    parts = ["termux-dialog", "confirm"]
    if title:
        parts.extend(["-t", shlex.quote(str(title))])
    if hint:
        parts.extend(["-i", shlex.quote(str(hint))])
    output, err = _run_termux_cmd(" ".join(parts), timeout=300)
    if err:
        return f"ERROR: {err}"
    try:
        data = json.loads(output)
        return data.get("text", "no")
    except (json.JSONDecodeError, ValueError):
        return output


@registry.register(
    "volume_get",
    "Read all audio stream volumes.",
    {},
    cacheable=True,
)
def volume_get():
    output, err = _run_termux_cmd("termux-volume")
    if err:
        return f"ERROR: {err}"
    try:
        streams = json.loads(output)
        lines = ["Volume:"]
        for s in streams:
            lines.append(
                f"- {s.get('stream', '?'):<12} {s.get('volume', '?')}/{s.get('max_volume', '?')}"
            )
        return "\n".join(lines)
    except (json.JSONDecodeError, ValueError):
        return output


@registry.register(
    "volume_set",
    "Set a stream's volume. stream: music|call|ring|alarm|notification|system. "
    "level is the absolute target.",
    {"stream": "string", "level": "integer"},
)
def volume_set(stream=None, level=None):
    if not stream or level is None:
        return "ERROR: stream and level are required"
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return f"ERROR: bad level: {level!r}"

    # Try Termux:API first
    output, err = _run_termux_cmd(
        f"termux-volume {shlex.quote(str(stream))} {lvl}"
    )
    if not err:
        return f"{stream} volume → {lvl}"

    # Fallback: Android media command
    log.info("volume_set: Termux:API failed, trying media volume fallback")
    stream_map = {
        "music": 3, "call": 0, "ring": 2,
        "alarm": 4, "notification": 5, "system": 1,
    }
    stream_int = stream_map.get(stream.lower())
    if stream_int is None:
        return f"ERROR: unknown stream '{stream}'. Use: music|call|ring|alarm|notification|system"
    cmd = f"media volume --show --stream {stream_int} --set {lvl}"
    output2, err2 = _run_termux_cmd(cmd)
    if err2:
        return f"ERROR: {err} (fallback also failed: {err2})"
    return f"{stream} volume → {lvl}"


@registry.register(
    "brightness_set",
    "Set screen brightness 0..255 (or 'auto').",
    {"level": "integer|string"},
)
def brightness_set(level=None):
    if level is None:
        return "ERROR: level is required"
    if isinstance(level, str) and level.lower() == "auto":
        arg = "auto"
    else:
        try:
            arg = str(max(0, min(255, int(level))))
        except (TypeError, ValueError):
            return f"ERROR: bad level: {level!r}"

    # Try Termux:API first
    output, err = _run_termux_cmd(f"termux-brightness {arg}")
    if not err:
        return f"brightness → {arg}"

    # Fallback: Android settings command
    log.info("brightness_set: Termux:API failed, trying settings fallback")
    if arg == "auto":
        output2, err2 = _run_termux_cmd("settings put system screen_brightness_mode 1")
    else:
        # Disable auto first, then set manual brightness
        _run_termux_cmd("settings put system screen_brightness_mode 0")
        output2, err2 = _run_termux_cmd(f"settings put system screen_brightness {arg}")
    if err2:
        return f"ERROR: {err} (fallback also failed: {err2})"
    return f"brightness → {arg}"


# ── Telephony ────────────────────────────────────────────────────────────────


@registry.register(
    "telephony_info",
    "Show SIM/cellular information (carrier, network type, phone type).",
    {},
    cacheable=True,
)
def telephony_info():
    # Try Termux:API first
    output, err = _run_termux_cmd("termux-telephony-deviceinfo")
    if not err:
        try:
            data = json.loads(output)
            keep = {
                "data_state", "data_activity", "network_operator_name",
                "network_type", "phone_type", "sim_country_iso",
                "sim_operator_name", "sim_state",
            }
            lines = []
            for k, v in data.items():
                if k in keep:
                    lines.append(f"{k}: {v}")
            if lines:
                return "\n".join(lines)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: Android getprop
    log.info("telephony_info: Termux:API failed, trying getprop fallback")
    props = {
        "carrier": "gsm.sim.operator.alpha",
        "operator_code": "gsm.operator.numeric",
        "sim_state": "gsm.sim.state",
        "network_type": "gsm.network.type",
        "phone_type": "ro.telephony.default_network",
        "country": "gsm.sim.operator.iso-country",
    }
    lines = []
    for label, prop in props.items():
        out, e = _run_termux_cmd(f"getprop {prop}")
        if not e and out:
            lines.append(f"{label}: {out}")
    if not lines:
        return f"ERROR: {err} (fallback also failed — no telephony properties found)"
    return "\n".join(lines)
