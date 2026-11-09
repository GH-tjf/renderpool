"""Box records for RenderPool: validation, defaults, and the commands RenderPool runs for a box.

A box is one machine running one picture engine. Its record is plain JSON (stored in RenderPool's state
database, editable from the web page), so every field is checked here before it is saved. The
commands RenderPool runs for a box (ssh tunnel, model list, relaunch, power check, app check) are BUILT
here from those checked fields as argv lists. Nothing is ever run through a shell on this machine,
and no free-form command can be set over HTTP.

Python 3 standard library only.
"""
import os
import re
import shlex

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$")
SSH_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MODEL_FILE_RE = re.compile(r"^[A-Za-z0-9._ +-]{1,200}$")

KINDS = ("image", "video")
WHERES = ("local", "ssh")
APPS = ("drawthings", "other")
OSES = ("macos", "windows", "linux")

# Draw Things on macOS
DT_MODELS_DIR = "~/Library/Containers/com.liuliu.draw-things/Data/Documents/Models"
DT_PROCESS = "DrawThings"       # the process name (no space), for pgrep -x
DT_APP = "Draw Things"          # the app name, for open -a

MAC_ON_AC = ["pmset", "-g", "batt"]
# UNVERIFIED on a real Windows laptop: prints 2 when on AC (Win32_Battery.BatteryStatus)
WIN_ON_AC = ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Battery).BatteryStatus"]
# Linux: prints 1 for each mains adapter that is plugged in (sysfs power_supply class)
LINUX_ON_AC_SH = 'for d in /sys/class/power_supply/*; do [ "$(cat "$d/type")" = Mains ] && cat "$d/online"; done; true'
LINUX_ON_AC = ["sh", "-c", LINUX_ON_AC_SH]

FIELDS = ("name", "where", "ssh_host", "ssh_key", "remote_port", "local_port", "api_port", "engine", "app", "os",
          "abilities", "laptop", "model_map", "pools", "speed_rank", "note")
NOTE_MAX = 60      # a free label shown under the box name, e.g. "Forge · RTX 4090"


def normalize(rec):
    """-> a copy of rec with every missing field given its default. Unknown keys are kept (validate refuses them)."""
    r = dict(rec or {})
    r.setdefault("where", "local")
    r.setdefault("ssh_host", "")
    r.setdefault("ssh_key", "")
    r.setdefault("remote_port", 7860)
    r.setdefault("api_port", 0)          # 0 = RenderPool talks HTTP straight to local_port (Draw Things in HTTP mode)
    r.setdefault("engine", "a1111")
    r.setdefault("app", "drawthings")
    r.setdefault("os", "macos")
    r.setdefault("abilities", ["image"])
    r.setdefault("laptop", False)
    r.setdefault("model_map", {})
    r.setdefault("speed_rank", 100)
    r.setdefault("note", "")
    if "pools" not in r:
        abil = r["abilities"] if isinstance(r["abilities"], list) else []
        r["pools"] = dict((k, k in abil) for k in KINDS)
    elif isinstance(r["pools"], dict):
        p = dict(r["pools"])          # unknown keys stay, so validate() refuses them
        for k in KINDS:
            p.setdefault(k, False)
        r["pools"] = p
    return r


def http_port(rec):
    """-> the local port RenderPool sends HTTP to: api_port (a dt-grpc-bridge in front of Draw Things' gRPC server,
    reached through local_port) when set, else local_port itself."""
    return rec.get("api_port") or rec["local_port"]


def ports(rec):
    """-> the set of local ports this box occupies."""
    return {rec["local_port"]} | ({rec["api_port"]} if rec.get("api_port") else set())


def _is_port(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def validate(rec, engines=("a1111",), cleared=None):
    """-> list of error strings for a normalized box record; empty means it is safe to save and to build commands from.

    cleared: {file: cleared_models entry}. When given, every model_map key must be a cleared file and its value
    one of that entry's "equivalents" (set in the config file only), so the page cannot map a cleared name onto
    an uncleared file."""
    errs = []
    if not isinstance(rec, dict):
        return ["a box must be a JSON object"]
    for k in rec:
        if k not in FIELDS:
            errs.append("unknown field %r" % k)
    note = rec.get("note", "")
    if not isinstance(note, str) or len(note) > NOTE_MAX or any(ord(c) < 32 or ord(c) == 127 for c in note):
        errs.append("note must be text of at most %d characters, one line" % NOTE_MAX)
    name = rec.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errs.append("name must be 1-32 letters, digits, _ or -")
    where = rec.get("where")
    if where not in WHERES:
        errs.append("where must be local or ssh")
    host = rec.get("ssh_host", "")
    if not isinstance(host, str) or (host and (not SSH_HOST_RE.match(host) or host.startswith("-"))):
        errs.append("ssh_host must look like user@host (letters, digits, . _ -)")
    elif where == "ssh" and not host:
        errs.append("an ssh box needs ssh_host")
    key = rec.get("ssh_key", "")
    if not isinstance(key, str) or (key and (not SSH_KEY_RE.match(key) or key in (".", "..") or key.startswith("-"))):
        errs.append("ssh_key must be a file name inside ~/.ssh (letters, digits, . _ -)")
    if not _is_port(rec.get("remote_port")):
        errs.append("remote_port must be a port number")
    if not _is_port(rec.get("local_port")):
        errs.append("local_port must be a port number")
    api = rec.get("api_port", 0)
    if api != 0 and not _is_port(api):
        errs.append("api_port must be 0 (none) or a port number")
    elif api and api == rec.get("local_port"):
        errs.append("api_port must differ from local_port")
    if rec.get("engine") not in engines:
        errs.append("engine must be one of: %s" % ", ".join(sorted(engines)))
    app = rec.get("app")
    if app not in APPS:
        errs.append("app must be drawthings or other")
    if rec.get("os") not in OSES:
        errs.append("os must be macos, windows or linux")
    if app == "drawthings" and rec.get("os") != "macos":
        errs.append("Draw Things boxes run macOS")
    abil = rec.get("abilities")
    if not isinstance(abil, list) or not abil or any(a not in KINDS for a in abil) or len(set(abil)) != len(abil):
        errs.append("abilities must be a non-empty list of image and/or video")
        abil = []
    if not isinstance(rec.get("laptop"), bool):
        errs.append("laptop must be true or false")
    mm = rec.get("model_map")
    if not isinstance(mm, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not MODEL_FILE_RE.match(k)
                                       or not MODEL_FILE_RE.match(v) for k, v in mm.items()):
        errs.append("model_map must map model file names to model file names")
    elif cleared is not None:
        for k, v in sorted(mm.items()):
            if k not in cleared:
                errs.append("model_map: %s is not a cleared model" % k)
            elif v not in (cleared[k].get("equivalents") or []):
                errs.append("model_map: %s is not a listed equivalent of %s (add it to that cleared model's "
                            "equivalents in the config file first)" % (v, k))
    pools = rec.get("pools")
    if not isinstance(pools, dict) or set(pools) != set(KINDS) or any(not isinstance(v, bool) for v in pools.values()):
        errs.append("pools must be {\"image\": true|false, \"video\": true|false}")
    else:
        for k in KINDS:
            if pools[k] and k not in abil:
                errs.append("the %s pool needs the %s ability" % (k, k))
    sr = rec.get("speed_rank")
    if not isinstance(sr, (int, float)) or isinstance(sr, bool) or sr <= 0:
        errs.append("speed_rank must be a positive number (expected seconds per picture)")
    return errs


def _key_path(rec):
    return os.path.join(os.path.expanduser("~/.ssh"), rec["ssh_key"])


def _ssh_base(rec):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if rec.get("ssh_key"):
        argv += ["-i", _key_path(rec)]
    return argv


def _on_box(rec, local_argv, remote_cmd):
    """-> the argv that runs a command on the box: local_argv here, or remote_cmd (a fixed string) over ssh."""
    if rec["where"] == "local":
        return list(local_argv)
    return _ssh_base(rec) + ["--", rec["ssh_host"], remote_cmd]


def commands(rec, dt_models_dir=DT_MODELS_DIR):
    """-> {"tunnel", "models", "relaunch", "on_ac", "app_running"}: argv lists, or None when not applicable.

    rec must have passed validate(). Remote command strings are made only of fixed words and
    shlex.quote()d values; the ssh host is its own argv element after "--"."""
    dt = rec["app"] == "drawthings"
    out = {"tunnel": None, "models": None, "relaunch": None, "on_ac": None, "app_running": None}
    if rec["where"] == "ssh":
        out["tunnel"] = (["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                          "-o", "ServerAliveCountMax=2"] + _ssh_base(rec)[1:]
                         + ["-L", "%d:127.0.0.1:%d" % (rec["local_port"], rec["remote_port"]), "--", rec["ssh_host"]])
    if dt:
        rel = dt_models_dir[2:] if dt_models_dir.startswith("~/") else None
        remote_ls = ("ls ~/" + shlex.quote(rel)) if rel is not None else ("ls " + shlex.quote(dt_models_dir))
        out["models"] = _on_box(rec, ["ls", os.path.expanduser(dt_models_dir)], remote_ls)
        out["relaunch"] = _on_box(rec, ["open", "-g", "-a", DT_APP], "open -g -a " + shlex.quote(DT_APP))
        out["app_running"] = _on_box(rec, ["pgrep", "-x", DT_PROCESS], "pgrep -x " + shlex.quote(DT_PROCESS))
    if rec["laptop"]:
        if rec["os"] == "macos":
            local, remote = MAC_ON_AC, " ".join(MAC_ON_AC)
        elif rec["os"] == "linux":
            local, remote = LINUX_ON_AC, "sh -c " + shlex.quote(LINUX_ON_AC_SH)
        else:
            local = WIN_ON_AC
            remote = 'powershell -NoProfile -Command "(Get-CimInstance Win32_Battery).BatteryStatus"'
        out["on_ac"] = _on_box(rec, local, remote)
    return out


def parse_on_ac(os_name, rc, output):
    """-> True only when the power check clearly says AC. Anything unreadable is False (out of the pool)."""
    if rc != 0 or not isinstance(output, str):
        return False
    if os_name == "macos":
        return "'AC Power'" in output     # pmset -g batt: "Now drawing from 'AC Power'"
    if os_name == "linux":
        return "1" in output.split()      # one line per mains adapter: 1 = plugged in
    return output.strip() == "2"
