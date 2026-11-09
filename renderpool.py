#!/usr/bin/env python3
"""RenderPool - one queue in front of one or more Draw Things HTTP APIs.

A drop-in broker: it speaks the same HTTP API as Draw Things (GET /, POST /sdapi/v1/txt2img,
POST /sdapi/v1/img2img), so a caller only changes its base URL. Every picture request is queued,
routed to an idle backend that holds the requested model, and the caller's connection blocks until
its picture is done, then receives the backend's status and body verbatim.

Controls (pause / reorder / cancel / pin / enable backends) are exposed through a small web page at
GET /renderpool/ and through `renderpool.py ctl ...`, both of which talk to the JSON API under /renderpool/api/.

Python 3 standard library only. See README.md.
"""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boxes as boxlib  # noqa: E402
from engines import ENGINES, BoxSaysDown, EngineDown, EngineTimeout  # noqa: E402

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
TAILNET = ipaddress.ip_network("100.64.0.0/10")
TERMINAL = ("done", "failed", "canceled", "abandoned", "orphaned")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# body keys that name further model files. Only `model` is routed through the cleared list, so in v1 any
# other model-bearing key must be empty (callers often send "loras": [] and "upscaler": null); keys ending in _model too.
GUIDANCE_KEYS = ("guidance_scale", "cfg_scale")   # aliases: Draw Things refuses a body carrying both
MODEL_BEARING_KEYS = ("refiner_model", "loras", "controls", "upscaler", "face_restoration")
NEEDS_YOU_TEXT = "needs you: turn on API Server in Draw Things"

DEFAULTS = {
    "listen": ["127.0.0.1"],        # loopback and/or this host's tailnet IP (100.64.0.0/10); never 0.0.0.0
    "allowed_origins": [],          # extra browser Origin host names, e.g. this host's tailnet name
    "allow_lan": False,             # true = listen may also name a private LAN address (10/8, 172.16/12, 192.168/16); no login
    "port": 7875,
    "db_path": "state/renderpool.db",
    "outputs_dir": "outputs",
    "pool_log_path": "state/pool-changes.log",   # one line per pool tick/untick: when, box, pool, ON/OFF, who
    "pool_log_keep_days": 14,       # lines older than this are trimmed (start + hourly); 0 = keep
    "keep_copies_for": ["me", "gui"],
    "hide_prompt_for": [],          # extra: every API job's prompt is hidden anyway; this also hides page submits
    "default_max_wait_s": 1800,     # >= the longest time a caller holds its request open
    "retry_after_s": 30,
    "key_replay_s": 600,
    "group_affinity": True,
    "affinity_max_wait_s": 120,
    "inventory_refresh_s": 300,
    "probe_interval_s": 10,
    "probe_timeout_s": 5,
    "relaunch_wait_s": 90,
    "down_retry_s": 60,
    "run_timeout_s": {"image": 900, "video": 3600},   # per kind: a video is never cut off by the picture clock
    "power_interval_s": 30,         # how often a laptop's power source is re-read
    "notify_cmd": [],               # argv run ONCE when a box needs you, with the message as its last argument
    "max_attempts": 3,
    "history_limit": 50,
    "password": "",                 # optional: when set, every call needs it (README "Password");
                                    # the RENDERPOOL_PASSWORD environment variable wins over this file
    "history_keep_days": 7,        # finished jobs (and their thumbnails/copies) older than this are deleted; 0 = keep
    "thumb_px": 256,                # long side of the History thumbnail every finished picture gets
    "cleared_models": [],       # the allow-list: [{"file", "kind": "image"|"video", "label", "license", "fixed"?}]
    "default_model": "",        # used when a job names no model; must be a cleared file
    "prompt_test": {},              # {"box": "laptop", "for_models": [cleared file, ...], "run_model": "<box file>"}:
                                    # that box may take those models' jobs as PROMPT TESTS - text only (img2img becomes
                                    # txt2img, the start picture is dropped), its picture stretched to the requested size.
                                    # For quick throwaway previews while a person refines prompts; {} = off.
    "test_sources": ["me", "gui"],  # sources that may also run TEST models: any generation model a box holds that is not cleared
    "test_kinds": {},           # {"file.ckpt": "image"|"video"|"support"}: overrides the file-name rule below
    "test_notes": {},           # {"file name prefix": "license status shown beside matching test models"}
    "backends": [],             # box records that seed the state database on the first start only
    "drawthings_models_dir": boxlib.DT_MODELS_DIR,
    "verbose": False,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL, source TEXT, grp TEXT, label TEXT, priority TEXT,
  endpoint TEXT, model TEXT, width INTEGER, height INTEGER, steps INTEGER,
  body TEXT, params TEXT,
  state TEXT, position INTEGER, paused INTEGER DEFAULT 0, pin TEXT,
  backend TEXT, run_model TEXT, started REAL, finished REAL, attempts INTEGER DEFAULT 0,
  http_status INTEGER, error TEXT, output TEXT, has_caller INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backend TEXT, model TEXT, width INTEGER, height INTEGER, seconds REAL, finished REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
# columns added after v1.0; added in place to an older database
MIGRATIONS = (("dkey", "TEXT"), ("resp_status", "INTEGER"), ("resp_ctype", "TEXT"), ("resp_body", "BLOB"),
              ("thumb", "TEXT"), ("frames", "INTEGER"), ("seat", "TEXT"))
SEAT_RE = re.compile(r"[^A-Za-z0-9 _.:@-]")
PRIORITY_WORDS = {"high": 5, "normal": 0, "low": -5}


def parse_priority(v):
    """X-RenderPool-Priority: high | normal | low | a whole number -9..9 (higher runs sooner). -> int, or raises ValueError."""
    v = (v or "").strip().lower()
    if not v:
        return 0
    if v in PRIORITY_WORDS:
        return PRIORITY_WORDS[v]
    n = int(v)
    if not -9 <= n <= 9:
        raise ValueError("priority must be high, normal, low or -9..9")
    return n


def clean_seat(v):
    """X-RenderPool-Seat: who sent the job (a person, script or AI session). Free text, trimmed to 40 safe characters."""
    return SEAT_RE.sub("", (v or "").strip())[:40]


def _prio(stored):
    """The items.priority column (NULL = normal) -> int."""
    try:
        return int(stored or 0)
    except (TypeError, ValueError):
        return 0
# Which model files a box holds are GENERATION models. Draw Things' HTTP API lists no models and its
# built-in model metadata is not on disk (custom.json covers imported models only), so a file-name rule decides;
# test_kinds overrides it per file. Support files: text encoders, VAEs, upscalers, LoRAs, control models.
SUPPORT_RE = re.compile(r"(vae|upscaler|lora|controlnet|control_net|ip_adapter|t2i_adapter|text_encoder|clip_vit|"
                        r"open_clip|tensordata|realesrgan|esrgan|restoreformer|gfpgan|codeformer|insightface|"
                        r"^t5_|^umt5|^clip_|^long_clip|^qwen_3_|^qwen_2\.5_vl|^gemma_|^llama_|^mistral_|^siglip|"
                        r"^dino|^pulid)", re.I)
VIDEO_RE = re.compile(r"^(ltx|wan_?2|wan_v2|hunyuan_?video|minimax_h3|svd|stable_video|mochi|cogvideo|skyreels|"
                      r"magi|kandinsky_?6)", re.I)
IMAGE_RE = re.compile(r"^(flux|sd_v|sd_xl|sdxl|sd3|sd_3|stable_diffusion|qwen_image|hidream|z_image|chroma|kolors|"
                      r"auraflow|pixart|playground|ssd_1b|segmind|wurstchen|kandinsky_?[2-5]|lumina|hunyuan_?image)",
                      re.I)
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")   # an outputs/ file: name, or folder/name (video frames)
INDEXES = """
CREATE INDEX IF NOT EXISTS items_state ON items(state, position);
CREATE INDEX IF NOT EXISTS items_dkey ON items(dkey);
"""


# ---------------------------------------------------------------------------------------------- config

def load_config(path=None, overrides=None):
    """-> config dict with defaults filled and relative paths resolved against the config's folder."""
    cfg = dict(DEFAULTS)
    user = {}
    base = os.getcwd()
    if path:
        with open(path) as f:
            user.update(json.load(f))
        base = os.path.dirname(os.path.abspath(path))
    cfg["_config_path"] = os.path.abspath(path) if path else None   # where the Settings tab saves
    if overrides:
        user.update(overrides)
    cfg.update(user)
    if os.environ.get("RENDERPOOL_PASSWORD"):
        cfg["password"] = os.environ["RENDERPOOL_PASSWORD"]
    if not isinstance(cfg.get("password") or "", str):
        raise SystemExit("renderpool: password must be a string")
    if "host" in user and "listen" not in user:   # an older config
        cfg["listen"] = [user["host"]]
    cfg.pop("host", None)
    for key in ("db_path", "outputs_dir", "pool_log_path"):
        if not cfg.get(key):
            continue
        p = os.path.expanduser(cfg[key])
        cfg[key] = p if os.path.isabs(p) else os.path.join(base, p)
    cfg["listen"] = list(cfg["listen"] or [])
    cfg["allowed_origins"] = [str(h).lower() for h in (cfg["allowed_origins"] or [])]
    if not cfg["listen"]:
        raise SystemExit("renderpool: listen needs at least one address")
    for ip in cfg["listen"]:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise SystemExit("renderpool: listen takes IP addresses only (got %r)" % (ip,))
        lan_ok = (cfg.get("allow_lan") is True and addr.version == 4 and addr.is_private
                  and not addr.is_unspecified and not addr.is_link_local)
        if not (addr.is_loopback or (addr.version == 4 and addr in TAILNET) or lan_ok):
            raise SystemExit("renderpool: listen address %s is not loopback or a tailnet (100.64.0.0/10) address; "
                             "RenderPool listens on a private LAN address only with \"allow_lan\": true (set a "
                             "\"password\" too), and never on every address" % ip)
    nc = cfg["notify_cmd"]
    if not isinstance(nc, list) or any(not isinstance(x, str) for x in nc):
        raise SystemExit("renderpool: notify_cmd must be a list of strings (an argv), or []")
    rt = cfg["run_timeout_s"]
    if isinstance(rt, (int, float)) and not isinstance(rt, bool):
        cfg["run_timeout_s"] = {"image": float(rt), "video": float(rt)}
    elif not isinstance(rt, dict) or any(k not in rt for k in ("image", "video")):
        raise SystemExit("renderpool: run_timeout_s must be {\"image\": seconds, \"video\": seconds}")
    if "model_aliases" in cfg:
        raise SystemExit("renderpool: model_aliases was removed; give the box a model_map instead")
    files = set()
    for m in cfg["cleared_models"]:
        if not isinstance(m, dict) or not isinstance(m.get("file"), str) or not m["file"] \
                or m.get("kind") not in ("image", "video"):
            raise SystemExit("renderpool: every cleared_models entry needs a file and a kind (image or video): %r" % (m,))
        lic = m.get("license")
        if not isinstance(lic, str) or not lic.strip():   # a model is cleared only with a cited license
            raise SystemExit("RenderPool: cleared model %s has no license citation" % m["file"])
        eq = m.get("equivalents", [])
        if not isinstance(eq, list) or any(not isinstance(x, str) or not x for x in eq):
            raise SystemExit("renderpool: equivalents of %s must be a list of file names" % m["file"])
        fx = m.get("fixed", {})
        if not isinstance(fx, dict) or any(not isinstance(k, str) or not k or k == "model"
                                           or not isinstance(v, (int, float, str, bool)) for k, v in fx.items()):
            raise SystemExit("renderpool: fixed of %s must be {\"param\": number|text|true|false}" % m["file"])
        files.add(m["file"])
    try:
        cfg["history_keep_days"] = float(cfg["history_keep_days"] or 0)
    except (TypeError, ValueError):
        raise SystemExit("renderpool: history_keep_days must be a number of days (0 = keep forever)")
    if cfg["history_keep_days"] < 0:
        raise SystemExit("renderpool: history_keep_days must be a number of days (0 = keep forever)")
    if cfg["default_model"] not in files:
        raise SystemExit("renderpool: default_model %r is not on cleared_models" % cfg["default_model"])
    seed, names, ports = [], set(), set()
    for b in cfg["backends"]:
        rec = boxlib.normalize(b)
        errs = boxlib.validate(rec, ENGINE_NAMES, dict((m["file"], m) for m in cfg["cleared_models"]))
        if errs:
            raise SystemExit("renderpool: box %r in backends: %s" % (rec.get("name"), "; ".join(errs)))
        if rec["name"] in names or boxlib.ports(rec) & ports:
            raise SystemExit("renderpool: duplicate box name or local port (%r)" % rec["name"])
        names.add(rec["name"])
        ports |= boxlib.ports(rec)
        seed.append(rec)
    cfg["backends"] = seed
    ts = cfg["test_sources"]
    if not isinstance(ts, list) or any(not isinstance(x, str) for x in ts):
        raise SystemExit("renderpool: test_sources must be a list of source names, or []")
    cfg["test_sources"] = [x.strip().lower() for x in ts]
    tk = cfg["test_kinds"]
    if not isinstance(tk, dict) or any(v not in ("image", "video", "support") for v in tk.values()):
        raise SystemExit("renderpool: test_kinds must be {\"file\": \"image\"|\"video\"|\"support\"}")
    tn = cfg["test_notes"]
    if not isinstance(tn, dict) or any(not isinstance(v, str) for v in tn.values()):
        raise SystemExit("renderpool: test_notes must be {\"file name prefix\": \"text\"}")
    return cfg


def classify_file(name, test_kinds=None):
    """-> "image" | "video" for a generation model file, "support" for a text encoder / VAE / upscaler / LoRA,
    None when the rule cannot tell (shown as "unknown type"; set it in test_kinds)."""
    if test_kinds and name in test_kinds:
        return test_kinds[name]
    if SUPPORT_RE.search(name):
        return "support"
    if VIDEO_RE.search(name):
        return "video"
    if IMAGE_RE.search(name):
        return "image"
    return None


# ---------------------------------------------------------------------------------------------- helpers

def peer_closed(sock):
    """True when the client side of `sock` has gone away (EOF or reset)."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def jdump(obj):
    return json.dumps(obj, separators=(",", ":"))


ENGINE_NAMES = tuple(sorted(ENGINES))


def run_box_cmd(box_name, which, argv, timeout):
    """Run one argv built by boxes.commands() (never through a shell). -> (returncode, stdout text).

    Commands are run, never logged: the argv holds an ssh host and key path."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, universal_newlines=True)
        return p.returncode, p.stdout
    except Exception:  # noqa: BLE001 - not found / timed out = the command failed
        return -1, ""


def spawn_tunnel(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def stretch_images(body, w, h):
    """A1111 JSON reply -> the same reply with every picture resized to exactly w x h (macOS sips)."""
    j = json.loads(body)
    out = []
    for im in j.get("images") or []:
        raw = base64.b64decode(im.split(",", 1)[1] if im.startswith("data:") else im)
        with tempfile.TemporaryDirectory(prefix="renderpool-stretch-") as td:
            src, dst = os.path.join(td, "in.png"), os.path.join(td, "out.png")
            with open(src, "wb") as f:
                f.write(raw)
            subprocess.run(["sips", "-z", str(h), str(w), "-s", "format", "png", src, "--out", dst],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=True)
            with open(dst, "rb") as f:
                out.append(base64.b64encode(f.read()).decode())
    j["images"] = out
    return json.dumps(j).encode("utf-8")


def make_thumb(raw, dest_dir, stem, px):
    """Write a small thumbnail of one picture. -> its file name in dest_dir, or None.

    macOS `sips` shrinks it to a JPEG with a long side of px; where sips is missing or refuses the data,
    a PNG/JPEG is stored as it is (the page scales it down) and anything else gets no thumbnail."""
    src = os.path.join(dest_dir, ".%s.src" % stem)
    name = stem + ".jpg"
    try:
        with open(src, "wb") as f:
            f.write(raw)
        p = subprocess.run(["sips", "-Z", str(int(px)), "-s", "format", "jpeg", src, "--out",
                            os.path.join(dest_dir, name)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        if p.returncode == 0 and os.path.isfile(os.path.join(dest_dir, name)):
            return name
    except Exception:  # noqa: BLE001 - no sips (not macOS), timed out: fall back below
        pass
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
    ext = ".png" if raw[:8] == PNG_MAGIC else (".jpg" if raw[:3] == b"\xff\xd8\xff" else None)
    if ext is None:
        return None
    with open(os.path.join(dest_dir, stem + ext), "wb") as f:
        f.write(raw)
    return stem + ext


class Box(object):
    """One machine running one engine. It runs one job at a time, whatever the pool."""

    def __init__(self, rec, engine):
        self.rec = rec
        self.engine = engine      # the plug-in; RenderPool never calls an engine's HTTP API itself
        self.up = False
        self.next_probe = 0.0
        self.current = None
        self.current_started = None
        self.current_expect = 0.0
        self.models = None         # set of .ckpt names, or None = unknown (accepts any model)
        self.models_checked = 0.0
        self.models_error = ""
        self.last_error = ""
        self.relaunching = False
        self.relaunched = False
        self.tunnel = None
        self.tunnel_started = 0.0
        self.on_ac = None          # laptops: True / False, re-read every power_interval_s; unreadable = False
        self.power_checked = 0.0
        self.needs_you = False
        self.api_down_since = None  # when the API was first seen down while the app was running

    @property
    def name(self):
        return self.rec["name"]

    @property
    def url(self):
        return "http://127.0.0.1:%d" % boxlib.http_port(self.rec)

    @property
    def speed_rank(self):
        return float(self.rec.get("speed_rank", 100))

    def in_pool(self, kind):
        return bool(self.rec["pools"].get(kind))

    @property
    def enabled(self):
        """Checked in at least one pool."""
        return any(self.rec["pools"].values())

    def state_text(self, broker):
        """The one-line state the Pools screen shows."""
        if self.current is not None:
            return "drawing"
        if self.needs_you:
            return NEEDS_YOU_TEXT
        if self.relaunching:
            return "relaunching"
        if not self.enabled:
            return "unchecked"
        if not self.up:
            return "offline"
        if self.rec["laptop"] and self.on_ac is not True:
            return "on battery"
        return "idle"


# ---------------------------------------------------------------------------------------------- broker

class Broker(object):
    def __init__(self, cfg, box_cmd=None, spawn=None):
        self.cfg = cfg
        self.box_cmd = box_cmd or run_box_cmd     # (box name, which, argv, timeout) -> (rc, stdout)
        self.spawn = spawn or spawn_tunnel        # argv -> Popen-like (the ssh tunnel)
        self.make_thumb = make_thumb              # (raw bytes, folder, stem, px) -> file name | None
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.stopping = False
        self.stop_event = threading.Event()
        os.makedirs(os.path.dirname(cfg["db_path"]) or ".", exist_ok=True)
        os.makedirs(cfg["outputs_dir"], exist_ok=True)
        self.db = sqlite3.connect(cfg["db_path"], check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = set(r[1] for r in self.db.execute("PRAGMA table_info(items)"))
        for name, typ in MIGRATIONS:
            if name not in cols:
                self.db.execute("ALTER TABLE items ADD COLUMN %s %s" % (name, typ))
        self.db.executescript(INDEXES)
        recs = self.get_setting("boxes", None)
        if recs is None:  # first start: the config's backends seed the database; after that the database wins
            recs = [dict(r) for r in cfg["backends"]]
            self.set_setting("boxes", recs)
        self.cleared = dict((m["file"], m) for m in cfg["cleared_models"])
        self.boxes = []
        self.invalid = []         # stored records that no longer validate: kept, shown, never routed to
        for r in recs:
            rec = boxlib.normalize(r) if isinstance(r, dict) else r
            errs = boxlib.validate(rec, ENGINE_NAMES, self.cleared)
            if errs:
                self.log("box %r in the database is not valid; kept out of every pool" % (rec.get("name") if isinstance(rec, dict) else rec,))
                self.pool_log(rec.get("name") if isinstance(rec, dict) else rec, "all", False, "renderpool start",
                              "record not valid: " + "; ".join(errs))
                self.invalid.append({"rec": r, "errors": errs})
                continue
            self.boxes.append(Box(rec, self._make_engine(rec)))
        self.pool_log("-", "-", None, "renderpool start", " ".join(
            "%s=%s" % (b.name, ",".join(k for k in boxlib.KINDS if b.rec["pools"].get(k)) or "none") for b in self.boxes))
        self.events = {}          # item id -> Event, set when the item reaches a final state
        self.results = {}         # item id -> (status, ctype, body, headers) while someone is waiting
        self.waiters = {}         # item id -> number of callers blocked on it
        self.dropped = set()      # running items whose last caller disconnected
        self.group_backend = {}   # group -> backend name it last ran on (affinity)
        self.threads = []
        self.httpd = None
        self.httpds = []
        self.recover()

    # -- settings ---------------------------------------------------------------------------------
    def get_setting(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, jdump(value)))

    def kind_of(self, model_file):
        """-> "image" | "video" for a cleared model file, or for a TEST model (a generation model a box holds
        that is not cleared); None otherwise."""
        m = self.cleared.get(model_file)
        if m:
            return m["kind"]
        return self.test_kind(model_file)

    def test_kind(self, model_file):
        """-> the kind of a test model: not cleared, held by a box (exact file), and a generation model."""
        if not model_file or model_file in self.cleared:
            return None
        k = classify_file(model_file, self.cfg.get("test_kinds"))
        if k not in ("image", "video"):
            return None
        if not any(b.models is not None and model_file in b.models for b in self.boxes):
            return None
        return k

    def test_note(self, model_file):
        best = ""
        for prefix, text in (self.cfg.get("test_notes") or {}).items():
            if model_file.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        return (self.cfg.get("test_notes") or {}).get(best, "") if best else ""

    def model_roles(self):
        """-> {"test": [{file, kind, boxes, note}], "unknown": [{file, boxes}]} over every box's held files."""
        held = {}
        for b in self.boxes:
            mapped = set((b.rec.get("model_map") or {}).values())
            for f in (b.models or ()):
                if f not in self.cleared and f not in mapped:
                    held.setdefault(f, []).append(b.name)
        test, unknown = [], []
        for f in sorted(held):
            k = classify_file(f, self.cfg.get("test_kinds"))
            if k in ("image", "video"):
                test.append({"file": f, "kind": k, "boxes": held[f], "note": self.test_note(f)})
            elif k is None:
                unknown.append({"file": f, "boxes": held[f]})
        return {"test": test, "unknown": unknown}

    def box(self, name):
        for b in self.boxes:
            if b.name == name:
                return b
        return None

    def _save_boxes(self):
        self.set_setting("boxes", [b.rec for b in self.boxes] + [x["rec"] for x in self.invalid])

    def _invalid_named(self, name):
        for x in self.invalid:
            if isinstance(x["rec"], dict) and x["rec"].get("name") == name:
                return x
        return None

    def _make_engine(self, rec):
        name = rec["name"]

        def run_cmd(which, argv, timeout):
            return self.box_cmd(name, which, argv, timeout)
        return ENGINES[rec["engine"]]("http://127.0.0.1:%d" % boxlib.http_port(rec), rec, run_cmd=run_cmd,
                                      dt_models_dir=self.cfg["drawthings_models_dir"])

    def box_commands(self, b):
        return boxlib.commands(b.rec, self.cfg["drawthings_models_dir"])

    # -- lifecycle --------------------------------------------------------------------------------
    def recover(self):
        """After a restart: API items whose caller is gone are abandoned; GUI items run again."""
        now = time.time()
        with self.lock:
            self.db.execute(
                "UPDATE items SET state='abandoned', finished=?, error='broker restarted; caller gone', body=NULL "
                "WHERE state IN ('queued','running') AND has_caller=1", (now,))
            self.db.execute(
                "UPDATE items SET state='queued', backend=NULL, run_model=NULL, started=NULL "
                "WHERE state='running' AND has_caller=0")

    def start(self):
        """One HTTP server per listen address, all on one port, all sharing this broker."""
        port = int(self.cfg["port"])
        for ip in self.cfg["listen"]:
            srv = _server_class(ip)((ip, port), Handler)
            srv.daemon_threads = True
            srv.broker = self
            port = srv.server_address[1]   # with port 0 the first bind picks it; the rest reuse it
            self.httpds.append(srv)
        self.httpd = self.httpds[0]
        self.port = port
        try:
            self.autoclear()
        except Exception as e:  # noqa: BLE001 - never stop RenderPool from starting
            self.log("autoclear error: %r" % e)
        for b in self.boxes:
            self.refresh_models(b)
            if b.enabled:
                if b.rec["laptop"]:
                    self.check_power(b)
                self.probe(b)
        for target in [h.serve_forever for h in self.httpds] + [self._dispatcher, self._monitor]:
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self):
        with self.wake:
            self.stopping = True
            self.stop_event.set()
            self.wake.notify_all()
        for h in self.httpds:
            h.shutdown()
            h.server_close()
        for b in self.boxes:
            self._stop_tunnel(b)
        for t in self.threads:
            t.join(timeout=3)
        with self.lock:
            self.db.close()

    # -- enqueue ----------------------------------------------------------------------------------
    def queued_ids(self):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM items WHERE state='queued' ORDER BY position, id")]

    def _renumber(self, ids):
        for i, item_id in enumerate(ids):
            if item_id is not None:   # None = a slot kept free for an item about to be inserted
                self.db.execute("UPDATE items SET position=? WHERE id=?", (i, item_id))

    def _insert_position(self, priority):
        """Where a new queued item of this priority goes: after the last queued item whose priority is the same
        or higher (equal priorities keep arrival order). Renumbers the queue when it lands before the end."""
        rows = self.db.execute("SELECT id, priority FROM items WHERE state='queued' ORDER BY position, id").fetchall()
        at = 0
        for i, r in enumerate(rows):
            if _prio(r["priority"]) >= priority:
                at = i + 1
        if at == len(rows):
            return self.db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM items WHERE state='queued'").fetchone()[0]
        ids = [r["id"] for r in rows]
        self._renumber(ids[:at] + [None] + ids[at:])
        return at

    def enqueue(self, endpoint, body_bytes, source="api", group="", label="", pin="", has_caller=True, key="",
                seat="", priority=0):
        """-> (item id, None) or (None, (http status, error text))."""
        try:
            body = json.loads(body_bytes.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body is not a JSON object")
        except (ValueError, UnicodeDecodeError) as e:
            return None, (400, "renderpool: request body is not JSON: %s" % e)
        if pin and not self.box(pin):
            return None, (400, "renderpool: unknown backend %r" % pin)
        if body.get("model") is not None and not isinstance(body.get("model"), str):
            return None, (400, "RenderPool: model must be a string")
        for k in sorted(body):
            if (k in MODEL_BEARING_KEYS or (k.endswith("_model") and k != "model")) and body[k] not in (None, "", [], {}):
                return None, (422, "RenderPool: %s is not supported" % k)
        model = body.get("model") or self.cfg["default_model"]
        kind = self.kind_of(model)
        if not kind:
            return None, (422, "RenderPool: model %s is not on the cleared list" % model)
        if model not in self.cleared and (source or "api") not in self.cfg["test_sources"]:
            return None, (422, "RenderPool: model %s is not on the cleared list (test models run only for sources %s)"
                          % (model, ", ".join(self.cfg["test_sources"]) or "none"))
        params = dict((k, v) for k, v in body.items() if k != "init_images")
        if "init_images" in body:
            params["init_images"] = "%d image(s)" % len(body.get("init_images") or [])

        def as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        with self.wake:
            if not self.holders(model, pin):  # "no box can run this" is said at once, never a silent wait
                return None, (422, "RenderPool: no box that can do %s holds model %s" % (kind, model))
            pos = self._insert_position(priority)
            cur = self.db.execute(
                "INSERT INTO items(created, source, grp, label, priority, endpoint, model, width, height, steps, "
                "body, params, state, position, pin, has_caller, dkey, seat) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (time.time(), source or "api", group or "", (label or "")[:120], str(priority) if priority else None,
                 endpoint, model, as_int(body.get("width")), as_int(body.get("height")),
                 as_int(body.get("steps")), body_bytes.decode("utf-8"), jdump(params), pos, pin or "",
                 1 if has_caller else 0, key or None, seat or None))
            item_id = cur.lastrowid  # joins after every queued item of the same or higher priority
            if has_caller:
                self.events[item_id] = threading.Event()
            self.wake.notify_all()
        return item_id, None

    def api_submit(self, endpoint, data, source="api", group="", label="", pin="", key="", seat="", priority=0):
        """Drop-in entry point. -> ("wait", item id) | ("reply", (status, ctype, body, headers)).

        With an X-RenderPool-Key: a key that is queued or running ATTACHES to that item (one picture, many waiters);
        a key that finished within key_replay_s gets the stored response, so a retry never paints twice.
        """
        with self.wake:
            if key:
                row = self.db.execute("SELECT id FROM items WHERE dkey=? AND state IN ('queued','running') "
                                      "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
                if row and row["id"] in self.events:
                    return "wait", self._register_waiter(row["id"])
                row = self.db.execute(
                    "SELECT * FROM items WHERE dkey=? AND resp_body IS NOT NULL AND finished>=? "
                    "ORDER BY finished DESC LIMIT 1", (key, time.time() - float(self.cfg["key_replay_s"]))).fetchone()
                if row:
                    return "reply", (row["resp_status"], row["resp_ctype"] or "application/json", row["resp_body"],
                                     {"X-RenderPool-Backend": row["backend"] or "", "X-RenderPool-Model": row["run_model"] or "",
                                      "X-RenderPool-Replay": "1", "X-RenderPool-Item": str(row["id"])})
            item_id, err = self.enqueue(endpoint, data, source=source, group=group, label=label,
                                        pin=pin, has_caller=True, key=key, seat=seat, priority=priority)
            if err:
                return "reply", (err[0], "application/json", jdump({"error": err[1]}).encode(), {})
            return "wait", self._register_waiter(item_id)

    def _register_waiter(self, item_id):
        """Called under the lock in the same step that found/created the item. -> (item id, its Event)."""
        self.waiters[item_id] = self.waiters.get(item_id, 0) + 1
        self.dropped.discard(item_id)
        return item_id, self.events[item_id]

    def _unwait(self, item_id):
        n = self.waiters.get(item_id, 0) - 1
        if n <= 0:
            self.waiters.pop(item_id, None)
            self.results.pop(item_id, None)
            return 0
        self.waiters[item_id] = n
        return n

    def queued_reply(self, item_id):
        """The 503 a caller gets when its max wait passes before its item started. The item stays queued."""
        row = self.db.execute("SELECT grp, paused FROM items WHERE id=?", (item_id,)).fetchone()
        ids = self.queued_ids()
        pg = set(self.get_setting("paused_groups", []))
        body = {"error": "RenderPool: still queued", "renderpool": "queued", "item": item_id, "position": ids.index(item_id) + 1 if item_id in ids else None,
                "group_paused": bool(row["grp"]) and row["grp"] in pg, "item_paused": bool(row["paused"]),
                "paused": bool(self.get_setting("paused", False))}
        return (503, "application/json", jdump(body).encode(),
                {"Retry-After": str(int(self.cfg["retry_after_s"])), "X-RenderPool-Item": str(item_id)})

    def wait_result(self, item_id, ev, sock, max_wait=None):
        """Block until the item finishes, the caller goes away (-> None), or max_wait passes before the item
        STARTED (-> 503 queued reply; the item keeps its place). -> (status, ctype, body, headers) or None.
        The caller was registered as a waiter by api_submit."""
        t0 = time.time()
        while not ev.wait(0.1):
            if self.stopping:
                with self.lock:
                    self._unwait(item_id)
                return (503, "application/json", b'{"error":"renderpool is shutting down"}', {})
            if peer_closed(sock):
                with self.wake:
                    if self._unwait(item_id) == 0 and not ev.is_set():
                        row = self.db.execute("SELECT state, dkey FROM items WHERE id=?", (item_id,)).fetchone()
                        if row and row["state"] == "queued" and row["dkey"]:
                            pass   # keyed: keeps its place; collect it later (same key, or /renderpool/api/result/<id>)
                        elif row and row["state"] == "queued":
                            self._finish(item_id, "abandoned", error="caller disconnected while queued")
                        else:
                            self.dropped.add(item_id)
                    self.wake.notify_all()
                return None
            if max_wait is not None and time.time() - t0 >= max_wait:
                with self.wake:
                    row = self.db.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
                    if row and row["state"] == "queued" and not ev.is_set():
                        self._unwait(item_id)
                        return self.queued_reply(item_id)
                max_wait = None  # it has started: wait for the picture
        with self.lock:
            res = self.results.get(item_id)
            self._unwait(item_id)
        return res or (500, "application/json", b'{"error":"renderpool lost the result"}', {})

    def _finish(self, item_id, state, status=None, error="", output=None, ctype="application/json", body=None,
                headers=None):
        if body is None:
            body = jdump({"error": error or state}).encode()
        row = self.db.execute("SELECT dkey FROM items WHERE id=?", (item_id,)).fetchone()
        # replayable for a retry with the same key: pictures, and the caller's own 4xx (a 5xx may be retried)
        keep = bool(row and row["dkey"]) and status is not None and (
            state in ("done", "orphaned") or (state == "failed" and 400 <= status < 500))
        self.db.execute(
            "UPDATE items SET state=?, finished=?, http_status=?, error=?, output=COALESCE(?, output), body=NULL, "
            "resp_status=?, resp_ctype=?, resp_body=? WHERE id=?",
            (state, time.time(), status, error, output, status if keep else None, ctype if keep else None,
             sqlite3.Binary(body) if keep else None, item_id))
        ev = self.events.pop(item_id, None)
        if self.waiters.get(item_id, 0) > 0:
            self.results[item_id] = (status or 500, ctype, body, headers or {})
        self.dropped.discard(item_id)
        if ev is not None:
            ev.set()

    # -- routing ----------------------------------------------------------------------------------
    @staticmethod
    def file_on(b, model):
        """-> the file box b would run for cleared `model`, or None when it does not hold it.

        Only exact files: the box holds the cleared file itself, or the file its model_map names for it.
        A box whose inventory is unknown is never used."""
        if not model or b.models is None:
            return None
        if model in b.models:
            return model
        mapped = (b.rec.get("model_map") or {}).get(model)
        if mapped and mapped in b.models:
            return mapped
        return None

    def prompt_test_file(self, b, model):
        """-> the file box b runs for `model` as a prompt test (cfg prompt_test), or None."""
        pt = self.cfg.get("prompt_test") or {}
        if not pt or pt.get("box") != b.name or model not in (pt.get("for_models") or []):
            return None
        f = pt.get("run_model")
        return f if f and b.models is not None and f in b.models else None

    def model_for(self, item, b):
        """-> the model file to send to box b, or None when b cannot run this item (pin, inventory)."""
        if item["pin"] and item["pin"] != b.name:
            return None
        return self.file_on(b, item["model"]) or self.prompt_test_file(b, item["model"])

    def eligible(self, b, kind):
        """True when box b may be given a new job of this kind: checked in that pool, up, not needs-you,
        and (not a laptop, or on AC). Says nothing about whether it is busy."""
        return (b.in_pool(kind) and b.up and not b.needs_you and not b.relaunching
                and (not b.rec["laptop"] or b.on_ac is True))

    def holders(self, model, pin=""):
        """-> boxes able to do this model's kind that hold the model, checked in its pool or not."""
        kind = self.kind_of(model)
        return [b for b in self.boxes if kind in b.rec["abilities"] and (not pin or b.name == pin)
                and (self.file_on(b, model) is not None or self.prompt_test_file(b, model) is not None)]

    def expected_seconds(self, b, model, w, h):
        rows = self.db.execute(
            "SELECT seconds FROM runs WHERE backend=? AND model=? AND width IS ? AND height IS ? "
            "ORDER BY id DESC LIMIT 10", (b.name, model or "", w, h)).fetchall()
        if not rows:
            return b.speed_rank
        return sum(r[0] for r in rows) / len(rows)

    def runnable_items(self):
        paused_groups = set(self.get_setting("paused_groups", []))
        rows = self.db.execute("SELECT * FROM items WHERE state='queued' ORDER BY position, id").fetchall()
        return [r for r in rows if not r["paused"] and r["grp"] not in paused_groups]

    def _dispatch_once(self):
        if self.stopping or self.get_setting("paused", False):
            return
        idle = [b for b in self.boxes if b.current is None]   # one job per box, whatever the pool
        if not idle:
            return
        for item in self.runnable_items():
            if not idle:
                break
            kind = self.kind_of(item["model"])
            if kind is None:
                continue  # an old item whose model is no longer cleared: never run
            ready = [b for b in idle if self.eligible(b, kind)]
            if not ready:
                continue
            aff = self._affinity_backend(item)
            if aff is not None:
                aff_model = self.model_for(item, aff)
                if aff in ready and aff_model is not None:
                    idle.remove(aff)
                    self._assign(item, aff, aff_model,
                                 self.expected_seconds(aff, aff_model, item["width"], item["height"]))
                    continue
                if (aff_model is not None and self.eligible(aff, kind) and aff.current is not None
                        and self._remaining(aff) < float(self.cfg["affinity_max_wait_s"])):
                    continue  # wait a little for the backend this group ran on (one model, one machine)
            choices = []
            for b in ready:
                m = self.model_for(item, b)
                if m is not None:
                    choices.append((self.expected_seconds(b, m, item["width"], item["height"]), b.speed_rank, b, m))
            if not choices:
                continue  # waits for a busy holder; later items may still use other idle boxes
            choices.sort(key=lambda c: (c[0], c[1]))
            expect, _, b, model = choices[0]
            idle.remove(b)
            self._assign(item, b, model, expect)

    def _affinity_backend(self, item):
        """-> the Backend this item's group last ran on, when group_affinity applies, else None."""
        if not self.cfg.get("group_affinity") or not item["grp"] or item["pin"]:
            return None
        name = self.group_backend.get(item["grp"])
        if name is None:
            row = self.db.execute("SELECT backend FROM items WHERE grp=? AND backend IS NOT NULL AND started IS NOT NULL "
                                  "ORDER BY started DESC LIMIT 1", (item["grp"],)).fetchone()
            name = row["backend"] if row else None
        return self.box(name) if name else None

    def _remaining(self, b):
        if b.current_started is None:
            return 0.0
        return max(0.0, b.current_expect - (time.time() - b.current_started))

    def _assign(self, item, b, model, expect=0.0):
        data = item["body"].encode("utf-8")
        d = json.loads(item["body"])
        fixed = (self.cleared.get(item["model"]) or {}).get("fixed") or {}
        # Draw Things answers 422 "More than one key for guidance" when a body carries both
        # guidance_scale and cfg_scale, so a fixed guidance key drops the caller's alias.
        alias_clash = [k for k in GUIDANCE_KEYS if k in d and k not in fixed
                       and any(f in GUIDANCE_KEYS for f in fixed)]
        if d.get("model") != model or alias_clash or any(d.get(k) != v for k, v in fixed.items()):
            d["model"] = model      # a mapped file, or the default model for a job that named none
            for k in alias_clash:
                d.pop(k, None)
            d.update(fixed)         # params this model always runs at (e.g. klein's guidance 1.0)
            data = json.dumps(d).encode("utf-8")
        now = time.time()
        self.db.execute(
            "UPDATE items SET state='running', backend=?, run_model=?, started=?, attempts=attempts+1 WHERE id=?",
            (b.name, model or "", now, item["id"]))
        b.current = item["id"]
        b.current_started = now
        b.current_expect = float(expect)
        if item["grp"]:
            self.group_backend[item["grp"]] = b.name
        endpoint = item["endpoint"]
        if self.prompt_test_file(b, item["model"]) == model and self.file_on(b, item["model"]) is None:
            # prompt test: the text only - no start picture, no strength - at the box's own size; _run stretches it back
            for k in ("init_images", "mask", "denoising_strength", "image_cfg_scale", "inpainting_fill"):
                d.pop(k, None)
            d["model"] = model
            data = json.dumps(d).encode("utf-8")
            endpoint = "txt2img"
        timeout = float(self.cfg["run_timeout_s"][self.kind_of(item["model"]) or "image"])
        t = threading.Thread(target=self._run, args=(item["id"], b, endpoint, data,
                                                     model or "", item["width"], item["height"], timeout), daemon=True)
        t.start()

    def _dispatcher(self):
        with self.wake:
            while not self.stopping:
                try:
                    self._dispatch_once()
                except Exception as e:  # noqa: BLE001 - the dispatcher must survive anything
                    self.log("dispatch error: %r" % e)
                self.wake.wait(1.0)

    # -- running ----------------------------------------------------------------------------------
    def _run(self, item_id, b, endpoint, data, model, w, h, timeout):
        t0 = time.time()
        status, ctype, body, down_reason, says_down = None, "application/json", b"", "", False
        try:
            status, ctype, body = b.engine.run(endpoint, data, timeout)
        except EngineTimeout:
            status, body = 504, jdump({"error": "RenderPool: box %s timed out" % b.name}).encode()
        except EngineDown as e:
            down_reason = str(e) or "down"
            says_down = isinstance(e, BoxSaysDown)
        secs = time.time() - t0
        prompt_test = bool(self.cfg.get("prompt_test")) and (self.cfg["prompt_test"].get("run_model") == model) \
            and (self.cfg["prompt_test"].get("box") == b.name)
        if status == 200 and prompt_test and w and h:
            try:
                body = stretch_images(body, int(w), int(h))
            except Exception as e:  # noqa: BLE001 - an unstretched preview is still a preview
                self.log("prompt test: stretching item %d failed: %r" % (item_id, e))
        saved = (None, None, None)
        if status == 200:
            try:
                saved = self._save_images(item_id, body)
            except Exception as e:  # noqa: BLE001 - a full disk must not lose the caller's answer
                self.log("saving item %d's pictures failed: %r" % (item_id, e))
        with self.wake:
            b.current = None
            b.current_started = None
            if self.stopping:
                return
            row = self.db.execute("SELECT attempts, has_caller FROM items WHERE id=?", (item_id,)).fetchone()
            gone = item_id in self.dropped and self.waiters.get(item_id, 0) == 0
            headers = {"X-RenderPool-Backend": b.name, "X-RenderPool-Model": model or "", "X-RenderPool-Item": str(item_id)}
            if prompt_test:
                headers["X-RenderPool-Prompt-Test"] = "1"
            if down_reason:
                b.up = False
                b.last_error = down_reason
                # a box that SAID it is down sits out one retry period (its quick probe may pass while the
                # app is still wedged); a box that went silent is probed again at once
                b.next_probe = time.time() + float(self.cfg["down_retry_s"]) if says_down else 0.0
                if gone:
                    self._finish(item_id, "abandoned", error="backend down; caller gone")
                elif row["attempts"] >= int(self.cfg["max_attempts"]):
                    self._finish(item_id, "failed", status=502,
                                 error="renderpool: backend %s down after %d attempts (%s)" % (b.name, row["attempts"], down_reason))
                else:
                    self.db.execute("UPDATE items SET state='queued', backend=NULL, run_model=NULL, started=NULL, "
                                    "error=? WHERE id=?", ("requeued: %s down" % b.name, item_id))
            else:
                if status == 200:
                    self.db.execute("INSERT INTO runs(backend, model, width, height, seconds, finished) "
                                    "VALUES(?,?,?,?,?,?)", (b.name, model, w, h, secs, time.time()))
                state = "done" if status == 200 else "failed"
                err = "" if status == 200 else body[:300].decode("utf-8", "replace")
                if gone:
                    state = "orphaned"
                if saved[1] or saved[2]:
                    self.db.execute("UPDATE items SET thumb=?, frames=? WHERE id=?", (saved[1], saved[2], item_id))
                self._finish(item_id, state, status=status, error=err, output=saved[0],
                             ctype=ctype, body=body, headers=headers)
            self.wake.notify_all()

    def _save_images(self, item_id, body):
        """Every finished picture gets a small thumbnail for History (a video: of its first frame).
        A full copy is kept only for keep_copies_for sources (a video's frames go in one folder).
        -> (full copy file | None, thumbnail file | None, frame count for a video | None)."""
        with self.lock:
            row = self.db.execute("SELECT source, model FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None, None, None
        try:
            images = json.loads(body.decode("utf-8")).get("images") or []
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None, None, None
        raws = []
        for im in images:
            if not isinstance(im, str):
                continue
            if im.startswith("data:") and "," in im:
                im = im.split(",", 1)[1]
            try:
                raws.append(base64.b64decode(im))
            except (ValueError, TypeError):
                continue
        if not raws:
            return None, None, None
        video = self.kind_of(row["model"]) == "video"
        out_dir = self.cfg["outputs_dir"]
        stamp = "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), item_id)
        full = None
        if row["source"] in (self.cfg.get("keep_copies_for") or []):   # e.g. customer pictures: never copied
            if video:
                folder = stamp + "-frames"
                os.makedirs(os.path.join(out_dir, folder), exist_ok=True)
                for i, raw in enumerate(raws):
                    with open(os.path.join(out_dir, folder, "f%04d.png" % (i + 1)), "wb") as f:
                        f.write(raw)
                full = folder + "/f0001.png"
            else:
                for i, raw in enumerate(raws):
                    name = "%s%s.png" % (stamp, "" if i == 0 else "-%d" % i)
                    with open(os.path.join(out_dir, name), "wb") as f:
                        f.write(raw)
                    full = full or name
        thumb = self.make_thumb(raws[0], out_dir, stamp + "-thumb", int(self.cfg.get("thumb_px") or 256))
        return full, thumb, (len(raws) if video else None)

    # -- history clearing -------------------------------------------------------------------------
    def _remove_output(self, name):
        """Delete one stored file (or a video's frames folder) inside outputs_dir; never anything outside it."""
        if not name or not FILE_RE.match(name) or ".." in name:
            return
        base = os.path.realpath(self.cfg["outputs_dir"])
        target = os.path.realpath(os.path.join(base, name.split("/")[0]))
        if os.path.dirname(target) != base:
            return
        try:
            if os.path.isdir(target):
                for f in os.listdir(target):
                    os.remove(os.path.join(target, f))
                os.rmdir(target)
            elif os.path.isfile(target):
                os.remove(target)
        except OSError as e:
            self.log("could not delete %s: %s" % (name, type(e).__name__))

    def clear_history(self, older_than=None):
        """Delete finished jobs (done, failed, canceled, abandoned, orphaned) and their thumbnails and
        copies; older_than (epoch seconds) limits it to jobs finished before then. Queued and running jobs
        are never touched. -> number of jobs deleted."""
        with self.lock:
            sql = "SELECT id, output, thumb FROM items WHERE state IN (%s)" % ",".join("?" * len(TERMINAL))
            args = list(TERMINAL)
            if older_than is not None:
                sql += " AND COALESCE(finished, created) < ?"
                args.append(older_than)
            rows = self.db.execute(sql, args).fetchall()
            for r in rows:
                self._remove_output(r["output"])
                self._remove_output(r["thumb"])
                self.db.execute("DELETE FROM items WHERE id=?", (r["id"],))
            return len(rows)

    def trim_pool_log(self, now=None):
        """Drop pool-log lines older than pool_log_keep_days (rewritten via a temp file + rename). -> lines dropped."""
        path, days = self.cfg.get("pool_log_path"), float(self.cfg.get("pool_log_keep_days") or 0)
        if not path or days <= 0 or not os.path.isfile(path):
            return 0
        cut = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((now or time.time()) - days * 86400))
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        keep = [l for l in lines if l[:19] >= cut]
        if len(keep) == len(lines):
            return 0
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
        return len(lines) - len(keep)

    def autoclear(self):
        try:
            self.trim_pool_log()
        except OSError as e:
            self.log("pool log trim failed: %r" % e)
        days = float(self.cfg.get("history_keep_days") or 0)
        if days > 0 and not self.stopping:
            n = self.clear_history(time.time() - days * 86400)
            if n:
                self.log("autoclear: deleted %d finished job(s) older than %g days" % (n, days))

    # -- backend health / inventory / tunnels -----------------------------------------------------
    def probe(self, b):
        """GET / through the engine. When the API is not answering and the box's app can be checked:
        app closed -> relaunch it (once per outage); app running but the API stays down for relaunch_wait_s
        -> "needs you" (its API Server was switched off; only a person can turn it on) and notify once."""
        ok = b.engine.is_up(float(self.cfg["probe_timeout_s"]))
        running = None
        cmds = self.box_commands(b)
        if not ok and cmds["app_running"]:
            rc, _ = self.box_cmd(b.name, "app_running", cmds["app_running"], 15)
            running = True if rc == 0 else (False if rc == 1 else None)   # pgrep: 0 found, 1 none; else unknown
        notify = False
        with self.wake:
            now = time.time()
            if ok:
                if not b.up:
                    self.log("box %s up" % b.name)
                if b.needs_you:
                    self.log("box %s no longer needs you" % b.name)
                b.up = True
                b.relaunched = False
                b.needs_you = False
                b.api_down_since = None
                b.next_probe = now + float(self.cfg["probe_interval_s"])
                self.wake.notify_all()
                return True
            b.up = False
            b.next_probe = now + float(self.cfg["down_retry_s"])
            if running is True:
                if b.api_down_since is None:
                    b.api_down_since = now
                if not b.needs_you and now - b.api_down_since >= float(self.cfg["relaunch_wait_s"]):
                    b.needs_you = True
                    notify = True
                    self.log("box %s needs you: API Server off" % b.name)
            else:
                b.api_down_since = None
                if running is False and cmds["relaunch"] and not b.relaunched:
                    b.relaunched = True
                    b.relaunching = True
                    threading.Thread(target=self._relaunch, args=(b,), daemon=True).start()
        if notify:
            self._notify("RenderPool: %s needs you - turn on API Server in Draw Things" % b.name)
        return False

    def _notify(self, message):
        """Run notify_cmd (an argv from config.json only, never from HTTP) once, with the message last."""
        cmd = list(self.cfg.get("notify_cmd") or [])
        if not cmd:
            return

        def go():
            try:
                subprocess.run(cmd + [message], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
            except Exception as e:  # noqa: BLE001
                self.log("notify failed: %s" % type(e).__name__)
        threading.Thread(target=go, daemon=True).start()

    def _relaunch(self, b):
        self.log("box %s down; relaunching its app" % b.name)
        rc, _ = self.box_cmd(b.name, "relaunch", self.box_commands(b)["relaunch"], 60)
        if rc != 0:
            b.last_error = "relaunch failed (exit %d)" % rc
        deadline = time.time() + float(self.cfg["relaunch_wait_s"])
        ok = False
        while time.time() < deadline and not self.stopping:
            if b.engine.is_up(float(self.cfg["probe_timeout_s"])):
                ok = True
                break
            self.stop_event.wait(2)
        with self.wake:
            b.relaunching = False
            b.up = ok
            b.next_probe = time.time() + float(self.cfg["probe_interval_s"] if ok else self.cfg["down_retry_s"])
            self.wake.notify_all()

    def refresh_models(self, b):
        """Ask the engine which model files the box holds. When it cannot tell, the last known list is kept
        (a box whose list was never known is never used)."""
        models = b.engine.list_models()
        with self.wake:
            if models is None:
                b.models_error = b.engine.last_error or "model list unknown"
            else:
                b.models = models
                b.models_error = ""
            b.models_checked = time.time()
            self.wake.notify_all()

    def check_power(self, b):
        """Re-read a laptop's power source. Anything unreadable counts as battery (out of the pool)."""
        cmd = self.box_commands(b)["on_ac"]
        on_ac = False
        if cmd:
            rc, out = self.box_cmd(b.name, "on_ac", cmd, 15)
            on_ac = boxlib.parse_on_ac(b.rec["os"], rc, out)
        with self.wake:
            if on_ac != b.on_ac:
                self.log("box %s %s" % (b.name, "on AC" if on_ac else "on battery or unreadable"))
            b.on_ac = on_ac
            b.power_checked = time.time()
            self.wake.notify_all()

    def _tunnel_check(self, b):
        """Keep an ssh box's tunnel up while it is in a pool OR still running a job; never cut a running job.
        Runs under the lock, so a box edit cannot swap the record between building the argv and spawning."""
        with self.lock:
            if b not in self.boxes:
                return
            cmd = self.box_commands(b)["tunnel"]
            if not cmd:
                self._stop_tunnel(b)
                return
            if not b.enabled and b.current is None:
                self._stop_tunnel(b)
                return
            if (b.tunnel is None or b.tunnel.poll() is not None) and time.time() - b.tunnel_started > 10:
                b.tunnel_started = time.time()
                try:
                    b.tunnel = self.spawn(cmd)
                except OSError as e:
                    b.last_error = "tunnel failed to start (%s)" % type(e).__name__

    def _stop_tunnel(self, b):
        if b.tunnel is not None and b.tunnel.poll() is None:
            b.tunnel.terminate()
            try:
                b.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                b.tunnel.kill()
        b.tunnel = None

    def purge_replies(self):
        """Stored responses are kept only for the key replay window."""
        with self.lock:
            if not self.stopping:
                self.db.execute("UPDATE items SET resp_body=NULL, resp_status=NULL, resp_ctype=NULL "
                                "WHERE resp_body IS NOT NULL AND finished<?",
                                (time.time() - float(self.cfg["key_replay_s"]),))

    def _monitor(self):
        last_purge = 0.0
        last_clear = time.time()      # start() ran the first autoclear
        while not self.stop_event.is_set():
            now = time.time()
            if now - last_clear >= 3600:
                last_clear = now
                try:
                    self.autoclear()
                except Exception as e:  # noqa: BLE001
                    self.log("autoclear error: %r" % e)
            if now - last_purge >= 30:
                last_purge = now
                try:
                    self.purge_replies()
                except Exception as e:  # noqa: BLE001
                    self.log("purge error: %r" % e)
            for b in list(self.boxes):
                if self.stopping:
                    return
                try:
                    self._tunnel_check(b)
                    if b.rec["laptop"] and b.enabled and now - b.power_checked >= float(self.cfg["power_interval_s"]):
                        self.check_power(b)   # also while busy: a box on battery finishes, then gets nothing new
                    # a failed model check (e.g. at start, before the box's tunnel is up) retries on the probe
                    # interval instead of waiting a whole inventory_refresh_s with the box's list unknown
                    every = float(self.cfg["inventory_refresh_s"] if not b.models_error else self.cfg["probe_interval_s"])
                    if now - b.models_checked >= every:
                        self.refresh_models(b)
                    if b.enabled and b.current is None and not b.relaunching and now >= b.next_probe:
                        self.probe(b)
                except Exception as e:  # noqa: BLE001
                    self.log("monitor error on %s: %r" % (b.name, e))
            self.stop_event.wait(0.1)

    # -- controls ---------------------------------------------------------------------------------
    def pool_log(self, box, kind, on, who, note=""):
        """One line per pool change (and per start) in pool_log_path: when, box, pool, on/off, who, note.
        Never raises: a log that cannot be written must not stop a pool change."""
        path = self.cfg.get("pool_log_path")
        if not path:
            return
        state = "-" if on is None else ("ON" if on else "OFF")
        line = "%s\t%s\t%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), box, kind, state, who or "?", note)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            self.log("pool log not written: %r" % e)

    def control(self, verb, a, who=""):
        """-> (http status, JSON-able object). Same verbs for the GUI and the CLI. who = the caller, for the pool log."""
        with self.wake:
            self._who = who
            try:
                res = self._control(verb, a or {})
            except (KeyError, ValueError, TypeError) as e:
                return 400, {"error": "bad request: %s" % e}
            self.wake.notify_all()
            return res

    # The Settings tab: each key is read live by the code
    # that uses it, so a change takes effect at once; it is also saved to config.json and logged with who.
    SETTINGS = ("history_keep_days", "history_limit", "pool_log_keep_days", "keep_copies_for", "default_max_wait_s")

    @staticmethod
    def _setting_value(key, v):
        """-> the cleaned value, or raise ValueError with a plain-language reason."""
        if key == "keep_copies_for":
            if isinstance(v, str):
                v = v.split(",")
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                raise ValueError("keep_copies_for must be a list of source names")
            out = []
            for x in v:
                x = x.strip().lower()
                if x and x not in out:
                    out.append(x)
            return out
        if isinstance(v, bool):
            raise ValueError("%s must be a number" % key)
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % key)
        if n != n or n in (float("inf"), float("-inf")):
            raise ValueError("%s must be a number" % key)
        if key in ("history_keep_days", "pool_log_keep_days"):
            if not 0 <= n <= 3650:
                raise ValueError("%s must be 0 (keep forever) to 3650 days" % key)
            return n
        if key == "history_limit":
            if n != int(n) or not 1 <= n <= 1000:
                raise ValueError("history_limit must be a whole number from 1 to 1000")
            return int(n)
        if not 60 <= n <= 86400:   # default_max_wait_s
            raise ValueError("default_max_wait_s must be 60 to 86400 seconds")
        return n

    def settings(self):
        return dict((k, self.cfg.get(k)) for k in self.SETTINGS)

    def _settings(self, a):
        """Validate every given key first (all or nothing), save config.json, then apply live and log."""
        new = {}
        for k, v in (a or {}).items():
            if k not in self.SETTINGS:
                return 400, {"error": "%r is not a setting (settings: %s)" % (k, ", ".join(self.SETTINGS))}
            try:
                new[k] = self._setting_value(k, v)
            except ValueError as e:
                return 400, {"error": str(e)}
        changed = dict((k, v) for k, v in new.items() if self.cfg.get(k) != v)
        path = self.cfg.get("_config_path")
        if not changed:
            return 200, {"ok": True, "changed": {}, "settings": self.settings(), "saved_to": path}
        if path:
            try:
                with open(path) as f:
                    raw = json.load(f)
                raw.update(changed)
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(raw, f, indent=1)
                    f.write("\n")
                shutil.copymode(path, tmp)              # os.replace would give config.json the temp file's mode
                shutil.copy2(path, path + ".prev")      # one previous copy, overwritten each save
                os.replace(tmp, path)
            except (OSError, ValueError) as e:
                return 500, {"error": "settings not saved to %s (%s); nothing changed" % (path, e)}
        who = getattr(self, "_who", "")
        for k, v in changed.items():
            self.pool_log("-", "setting", None, who, "%s %s -> %s" % (k, json.dumps(self.cfg.get(k)), json.dumps(v)))
            self.cfg[k] = v
        return 200, {"ok": True, "changed": changed, "settings": self.settings(), "saved_to": path}

    def _item(self, a):
        item_id = int(a["item"])
        row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("no item %d" % item_id)
        return row

    def _move(self, item_id, pos):
        ids = self.queued_ids()
        if item_id not in ids:
            return 409, {"error": "item %d is not queued" % item_id}
        ids.remove(item_id)
        pos = max(1, min(int(pos), len(ids) + 1))
        ids.insert(pos - 1, item_id)
        self._renumber(ids)
        return 200, {"ok": True, "item": item_id, "position": pos}

    BUMP_KEYS = {"seat": "seat", "source": "source", "model": "model", "group": "grp"}

    def _bump(self, a):
        """Move every queued item matching ONE of seat/source/model/group to the front (to='front', default) or
        the back (to='back') of the queue, keeping their order among themselves. -> how many moved."""
        keys = [k for k in self.BUMP_KEYS if a.get(k) not in (None, "")]
        if len(keys) != 1:
            return 400, {"error": "bump needs exactly one of: %s" % ", ".join(self.BUMP_KEYS)}
        to = a.get("to") or "front"
        if to not in ("front", "back"):
            return 400, {"error": "to must be front or back"}
        col, val = self.BUMP_KEYS[keys[0]], str(a[keys[0]])
        rows = self.db.execute("SELECT id, COALESCE(%s, '') AS v FROM items WHERE state='queued' "
                               "ORDER BY position, id" % col).fetchall()
        hit = [r["id"] for r in rows if r["v"] == val]
        rest = [r["id"] for r in rows if r["v"] != val]
        self._renumber(hit + rest if to == "front" else rest + hit)
        return 200, {"ok": True, "moved": len(hit), "to": to, keys[0]: val}

    def result(self, item_id):
        """GET /renderpool/api/result/<id>: collect a picture later. 200 + the engine's stored reply (items sent with an
        X-RenderPool-Key keep it for key_replay_s after they finish); 202 + state/position while queued or running;
        410 when it finished but no reply is kept; 404 for an unknown item."""
        with self.lock:
            row = self.db.execute("SELECT id, state, resp_status, resp_ctype, resp_body, backend, run_model "
                                  "FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return 404, "application/json", jdump({"error": "no item %d" % item_id}).encode(), {}
            if row["state"] in ("queued", "running"):
                ids = self.queued_ids()
                body = {"item": item_id, "state": row["state"],
                        "position": ids.index(item_id) + 1 if item_id in ids else None}
                return 202, "application/json", jdump(body).encode(), {"Retry-After": str(int(self.cfg["retry_after_s"]))}
            if row["resp_body"] is None:
                body = {"item": item_id, "state": row["state"],
                        "error": "finished, but no reply is kept (send an X-RenderPool-Key; kept %ss)" % self.cfg["key_replay_s"]}
                return 410, "application/json", jdump(body).encode(), {}
            return (row["resp_status"], row["resp_ctype"] or "application/json", bytes(row["resp_body"]),
                    {"X-RenderPool-Item": str(item_id), "X-RenderPool-Backend": row["backend"] or "",
                     "X-RenderPool-Model": row["run_model"] or "", "X-RenderPool-Replay": "1"})

    def _control(self, verb, a):
        if verb in ("pause", "resume"):
            flag = verb == "pause"
            if a.get("item") is not None:
                row = self._item(a)
                self.db.execute("UPDATE items SET paused=? WHERE id=?", (1 if flag else 0, row["id"]))
                return 200, {"ok": True, "item": row["id"], "paused": flag}
            if a.get("group"):
                groups = set(self.get_setting("paused_groups", []))
                (groups.add if flag else groups.discard)(a["group"])
                self.set_setting("paused_groups", sorted(groups))
                return 200, {"ok": True, "group": a["group"], "paused": flag}
            self.set_setting("paused", flag)
            return 200, {"ok": True, "paused": flag}
        if verb == "front":
            return self._move(self._item(a)["id"], 1)
        if verb == "bump":
            return self._bump(a)
        if verb == "move":
            return self._move(self._item(a)["id"], a["pos"])
        if verb in ("up", "down"):
            item_id = self._item(a)["id"]
            ids = self.queued_ids()
            if item_id not in ids:
                return 409, {"error": "item %d is not queued" % item_id}
            return self._move(item_id, ids.index(item_id) + (0 if verb == "up" else 2))
        if verb == "cancel":
            if a.get("group"):
                rows = self.db.execute("SELECT id FROM items WHERE state='queued' AND grp=?", (a["group"],)).fetchall()
                for r in rows:
                    self._finish(r[0], "canceled", status=409, error="canceled in RenderPool")
                return 200, {"ok": True, "canceled": [r[0] for r in rows]}
            row = self._item(a)
            if row["state"] != "queued":
                return 409, {"error": "item %d is %s; only queued items can be canceled "
                                      "(a running picture is never interrupted)" % (row["id"], row["state"])}
            self._finish(row["id"], "canceled", status=409, error="canceled in RenderPool")
            return 200, {"ok": True, "canceled": [row["id"]]}
        if verb == "prompt":
            # One item's full prompt, fetched only when a person opens that row or picture. /state never
            # carries an API job's prompt (customer scene text), so seats that poll /state never see it.
            row = self._item(a)
            try:
                p = json.loads(row["params"] or "{}")
            except ValueError:
                p = {}
            return 200, {"ok": True, "item": row["id"], "prompt": str(p.get("prompt") or ""),
                         "negative_prompt": str(p.get("negative_prompt") or "")}
        if verb == "pin":
            row = self._item(a)
            name = a.get("backend") or a.get("box") or ""
            if name and not self.box(name):
                return 400, {"error": "unknown box %r" % name}
            self.db.execute("UPDATE items SET pin=? WHERE id=?", (name, row["id"]))
            return 200, {"ok": True, "item": row["id"], "pin": name}
        if verb == "backend":  # the old on/off switch = the image pool
            return self._control("pool", {"box": a["name"], "kind": "image", "on": a["enabled"]})
        if verb == "pool":
            b = self.box(a["box"])
            if b is None:
                return 400, {"error": "unknown box %r" % a["box"]}
            kind = a["kind"]
            if kind not in boxlib.KINDS:
                return 400, {"error": "kind must be image or video"}
            on = bool(a["on"])
            if on and kind not in b.rec["abilities"]:
                return 400, {"error": "box %s has no %s ability, so it cannot join the %s pool" % (b.name, kind, kind)}
            was_out = not b.enabled
            if bool(b.rec["pools"].get(kind)) != on:
                self.pool_log(b.name, kind, on, getattr(self, "_who", ""))
            b.rec["pools"][kind] = on
            self._save_boxes()
            if on:
                b.next_probe = 0.0
                b.relaunched = False
                if was_out and b.current is None:
                    # nothing re-read this box while it was in no pool: probe it (and read its power) before use
                    b.up = False
                    b.on_ac = None
                    b.power_checked = 0.0
            return 200, {"ok": True, "box": b.name, "pools": dict(b.rec["pools"])}
        if verb in ("box-add", "box-edit"):
            return self._box_save(verb, a)
        if verb == "box-remove":
            b = self.box(a["name"])
            bad = self._invalid_named(a["name"])
            if b is None and bad is not None:
                self.invalid.remove(bad)
                self._save_boxes()
                return 200, {"ok": True, "removed": a["name"]}
            if b is None:
                return 404, {"error": "unknown box %r" % a["name"]}
            if b.current is not None:
                return 409, {"error": "box %s is running item %d; uncheck it, then remove it when idle" % (b.name, b.current)}
            self._stop_tunnel(b)
            self.boxes.remove(b)
            self._save_boxes()
            return 200, {"ok": True, "removed": b.name}
        if verb == "refresh-models":
            for b in self.boxes:
                b.models_checked = 0.0
            return 200, {"ok": True}
        if verb == "submit":
            return self._submit(a)
        if verb == "settings":
            return self._settings(a)
        if verb == "clear-history":
            return 200, {"ok": True, "deleted": self.clear_history()}
        return 404, {"error": "unknown verb %r" % verb}

    def _box_save(self, verb, a):
        rec = boxlib.normalize(a)
        errs = boxlib.validate(rec, ENGINE_NAMES, self.cleared)
        existing = self.box(rec.get("name")) if isinstance(rec.get("name"), str) else None
        bad = self._invalid_named(rec.get("name"))
        if verb == "box-edit" and existing is None and bad is None and not errs:
            return 404, {"error": "unknown box %r" % rec.get("name")}
        if verb == "box-add" and (existing is not None or bad is not None):
            errs.append("a box named %s already exists" % rec.get("name"))
        for o in self.boxes:
            if o is not existing and not errs and boxlib.ports(o.rec) & boxlib.ports(rec):
                errs.append("port %s is already used by box %s" % (
                    ", ".join(str(x) for x in sorted(boxlib.ports(o.rec) & boxlib.ports(rec))), o.name))
        if errs:
            return 400, {"error": "box not saved: " + "; ".join(errs), "errors": errs}
        existing_pools = dict(existing.rec["pools"]) if existing is not None else {}
        if existing is None:
            if bad is not None:          # an edit that fixes an invalid stored record makes it a box again
                self.invalid.remove(bad)
            b = Box(rec, self._make_engine(rec))
            self.boxes.append(b)
        else:
            b = existing
            if b.current is not None:
                return 409, {"error": "box %s is running item %d; edit it when it is idle" % (b.name, b.current)}
            transport = ("where", "ssh_host", "ssh_key", "remote_port", "local_port", "api_port", "engine", "app", "os")
            if any(b.rec.get(k) != rec.get(k) for k in transport):
                self._stop_tunnel(b)        # restarted by the monitor with the new fields, at once
                b.tunnel_started = 0.0
                b.up = False
            b.rec = rec
            b.engine = self._make_engine(rec)
        old_pools = {} if existing is None else dict(existing_pools)
        for k in boxlib.KINDS:
            if bool(old_pools.get(k)) != bool(rec["pools"].get(k)):
                self.pool_log(rec["name"], k, bool(rec["pools"].get(k)), getattr(self, "_who", ""), verb)
        b.models_checked = 0.0
        b.next_probe = 0.0
        b.relaunched = False
        self._save_boxes()
        return 200, {"ok": True, "box": rec["name"]}

    def boxes_detail(self):
        """The Boxes screen's records (the only place an ssh host is shown), with each box's model files
        marked cleared (a cleared file, or the file its model_map runs for one) or not."""
        with self.lock:
            out = []
            for b in self.boxes:
                d = dict(b.rec)
                mapped = dict((v, k) for k, v in (b.rec.get("model_map") or {}).items())
                d["models_held"] = [{"file": m, "cleared": m in self.cleared, "mapped_to": mapped.get(m),
                                     "role": ("cleared" if m in self.cleared else "mapped" if mapped.get(m) else
                                              {"image": "test", "video": "test", "support": "support"}.get(
                                                  classify_file(m, self.cfg.get("test_kinds")), "unknown"))}
                                    for m in sorted(b.models or [])]
                d["models_error"] = b.models_error
                d["invalid"] = []
                out.append(d)
            for x in self.invalid:
                d = dict(x["rec"]) if isinstance(x["rec"], dict) else {"name": repr(x["rec"])}
                d["invalid"] = list(x["errors"])
                d["models_held"] = []
                d["models_error"] = ""
                out.append(d)
            return {"boxes": out, "engines": list(ENGINE_NAMES), "kinds": list(boxlib.KINDS),
                    "apps": list(boxlib.APPS), "oses": list(boxlib.OSES),
                    "cleared_models": [dict(m) for m in self.cfg["cleared_models"]]}

    def _submit(self, a):
        prompt = str(a.get("prompt") or "").strip()
        if not prompt:
            return 400, {"error": "prompt is required"}
        count = max(1, min(int(a.get("count") or 1), 20))
        seed = a.get("seed")
        seed = int(seed) if seed not in (None, "") else None
        model = a.get("model") or ""
        if not isinstance(model, str):
            return 400, {"error": "RenderPool: model must be a string"}
        base = {"model": model, "prompt": prompt,
                "negative_prompt": a.get("negative_prompt") or "",
                "width": int(a.get("width") or 1024), "height": int(a.get("height") or 1024),
                "steps": int(a.get("steps") or 12), "guidance_scale": float(a.get("guidance_scale") or 1.0),
                "batch_count": 1, "batch_size": 1, "strength": 1.0}
        if self.kind_of(model or self.cfg["default_model"]) == "video":
            for k, lo, hi in (("num_frames", 1, 201), ("fps", 1, 30)):   # Draw Things' own ranges
                if a.get(k) not in (None, ""):
                    base[k] = max(lo, min(int(a[k]), hi))
        base.update((self.cleared.get(model or self.cfg["default_model"]) or {}).get("fixed") or {})
        bodies = []
        for i in range(count):
            body = dict(base)
            body["seed"] = seed + i if seed is not None else random.randint(0, 2 ** 31 - 1)
            bodies.append(body)
        ids = []
        for body in bodies:
            item_id, err = self.enqueue("txt2img", json.dumps(body).encode(), source=a.get("source") or "gui",
                                        group=a.get("group") or "", label=a.get("label") or prompt[:60],
                                        pin=a.get("backend") or "", has_caller=False,
                                        seat=clean_seat(a.get("seat")) or "page")
            if err:
                return err[0], {"error": err[1]}
            ids.append(item_id)
        return 200, {"ok": True, "ids": ids}

    # -- state ------------------------------------------------------------------------------------
    def _summary(self, r, paused_groups, now):
        p = {}
        try:
            p = json.loads(r["params"] or "{}")
        except ValueError:
            pass
        if r["has_caller"] or r["source"] in (self.cfg.get("hide_prompt_for") or []):
            p["prompt"] = ""  # API jobs (e.g. customer scene text) show their label only; page submits show it
        out = {
            "id": r["id"], "state": r["state"], "source": r["source"], "group": r["grp"], "label": r["label"],
            "seat": r["seat"] or "", "priority": _prio(r["priority"]),
            "kind": self.kind_of(r["model"]) or classify_file(r["model"] or "", self.cfg.get("test_kinds")),
            "test": bool(r["model"]) and r["model"] not in self.cleared,
            "endpoint": r["endpoint"], "model": r["model"], "run_model": r["run_model"],
            "width": r["width"], "height": r["height"], "steps": r["steps"], "seed": p.get("seed"),
            "prompt": (p.get("prompt") or "")[:300], "paused": bool(r["paused"]),
            "group_paused": bool(r["grp"]) and r["grp"] in paused_groups, "pin": r["pin"], "backend": r["backend"],
            "created": r["created"], "started": r["started"], "finished": r["finished"],
            "attempts": r["attempts"], "http_status": r["http_status"], "error": r["error"],
            "thumb": ("/renderpool/img/" + (r["thumb"] or r["output"])) if (r["thumb"] or r["output"]) else None,
            "full": ("/renderpool/img/" + r["output"]) if r["output"] else None,
            "frames": r["frames"],
        }
        if r["started"]:
            out["seconds"] = round((r["finished"] or now) - r["started"], 2)
        kind = self.kind_of(r["model"])
        if r["state"] == "queued" and kind:
            holders = self.holders(r["model"], r["pin"])
            checked = [b for b in holders if b.in_pool(kind)]
            if not holders:
                out["note"] = "waiting: no box holds this model"
            elif not checked:
                out["note"] = "waiting: only unchecked boxes hold this model"
            elif not any(self.eligible(b, kind) for b in checked):
                out["note"] = "waiting: the checked boxes that hold this model are offline, on battery or need you"
        return out

    def state(self):
        with self.lock:
            now = time.time()
            pg = set(self.get_setting("paused_groups", []))
            q = self.db.execute("SELECT * FROM items WHERE state='queued' ORDER BY position, id").fetchall()
            run = self.db.execute("SELECT * FROM items WHERE state='running' ORDER BY started").fetchall()
            hist = self.db.execute("SELECT * FROM items WHERE state IN (%s) ORDER BY finished DESC LIMIT ?"
                                   % ",".join("?" * len(TERMINAL)),
                                   TERMINAL + (int(self.cfg["history_limit"]),)).fetchall()
            roles = self.model_roles()
            boxes = []
            for b in self.boxes:   # no ssh host or key here: those are on the Boxes screen only
                r = b.rec
                boxes.append({
                    "name": b.name, "pools": dict(r["pools"]), "abilities": list(r["abilities"]),
                    "where": r["where"], "engine": r["engine"], "app": r["app"], "os": r["os"], "laptop": r["laptop"],
                    "note": r.get("note", ""), "enabled": b.enabled, "up": b.up, "relaunching": b.relaunching,
                    "on_ac": (b.on_ac is True) if r["laptop"] else None, "needs_you": b.needs_you,
                    "state_text": b.state_text(self),
                    "busy": b.current is not None, "current": b.current,
                    "elapsed": round(now - b.current_started, 1) if b.current_started else None,
                    "models": sorted(b.models) if b.models is not None else None,
                    "models_error": b.models_error, "last_error": b.last_error, "speed_rank": b.speed_rank,
                })
            return {
                "service": "renderpool", "version": VERSION, "now": now,
                "paused": bool(self.get_setting("paused", False)), "paused_groups": sorted(pg),
                "boxes": boxes,
                "invalid_boxes": [{"name": (x["rec"].get("name") if isinstance(x["rec"], dict) else None),
                                   "errors": list(x["errors"])} for x in self.invalid],
                "cleared_models": [dict(m) for m in self.cfg["cleared_models"]],
                "default_model": self.cfg["default_model"],
                "history_keep_days": self.cfg["history_keep_days"],
                "settings": self.settings(), "settings_saved_to": self.cfg.get("_config_path"),
                "test_sources": list(self.cfg["test_sources"]),
                "test_models": roles["test"], "unknown_models": roles["unknown"],
                "running": [self._summary(r, pg, now) for r in run],
                "queue": [dict(self._summary(r, pg, now), position=i + 1) for i, r in enumerate(q)],
                "history": [self._summary(r, pg, now) for r in hist],
            }

    def group_status(self, group):
        with self.lock:
            ids = self.queued_ids()
            rows = self.db.execute("SELECT id, state FROM items WHERE grp=? AND state IN ('queued','running')",
                                   (group,)).fetchall()
            queued = [r["id"] for r in rows if r["state"] == "queued"]
            positions = [ids.index(i) + 1 for i in queued if i in ids]
            return {"group": group, "paused": group in set(self.get_setting("paused_groups", [])),
                    "all_paused": bool(self.get_setting("paused", False)),
                    "queued": len(queued), "running": sum(1 for r in rows if r["state"] == "running"),
                    "position_of_first": min(positions) if positions else None}

    def liveness(self):
        """GET /: 200 whenever RenderPool is up, whatever the boxes are doing. Callers read 200 as "up";
        "no box can run this" is a 422 on the POST, never a down root."""
        with self.lock:
            boxes = [{"name": b.name, "pools": dict(b.rec["pools"]), "up": b.up and not b.relaunching,
                      "busy": b.current is not None, "state_text": b.state_text(self)} for b in self.boxes]
            queued = len(self.queued_ids())
        up = [b["name"] for b in boxes if any(b["pools"].values()) and b["up"]]
        return 200, {"service": "renderpool", "version": VERSION, "boxes_up": up, "boxes": boxes, "queued": queued}

    def log(self, msg):
        if self.cfg.get("verbose"):
            sys.stderr.write("renderpool %s %s\n" % (time.strftime("%H:%M:%S"), msg))


# ---------------------------------------------------------------------------------------------- HTTP

def _server_class(ip):
    if ":" not in ip:
        return ThreadingHTTPServer

    class V6(ThreadingHTTPServer):
        address_family = socket.AF_INET6
    return V6


class Handler(BaseHTTPRequestHandler):
    server_version = "RenderPool/" + VERSION
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        if self.server.broker.cfg.get("verbose"):
            sys.stderr.write("renderpool http %s\n" % (fmt % args))

    def _send(self, status, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = jdump(body).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        body = bytes(body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _origin_ok(self):
        """Refuse cross-site browser POSTs (a web page aiming at RenderPool). Non-browser callers send no Origin.
        Allowed, host AND port: loopback or a listen address on RenderPool's own port, or an allowed_origins entry
        ("name" = that name on RenderPool's port, "name:port" = exactly that)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        u = urllib.parse.urlparse(origin)
        host = (u.hostname or "").lower()
        try:
            port = u.port
        except ValueError:
            return False
        if port is None:
            port = 443 if u.scheme == "https" else 80
        broker = self.server.broker
        if (host in LOOPBACK or host in broker.cfg["listen"]) and port == broker.port:
            return True
        for entry in broker.cfg["allowed_origins"]:
            name, _, eport = entry.partition(":")
            if name == host and port == (int(eport) if eport.isdigit() else broker.port):
                return True
        return False

    OPEN_PATHS = ("/", "/renderpool", "/renderpool/", "/renderpool/api/login")   # health, the page itself, login

    def _cookie_value(self, pw):
        """The login cookie holds a keyed hash of the password, never the password."""
        return hmac.new(pw.encode("utf-8"), b"renderpool-login-cookie", hashlib.sha256).hexdigest()

    def _auth_ok(self, path):
        """No password set -> everything is open. Set -> every path but OPEN_PATHS needs one of:
        Authorization: Bearer <password>, X-RenderPool-Password: <password>, or the page's login cookie."""
        pw = self.server.broker.cfg.get("password") or ""
        if not pw or path in self.OPEN_PATHS:
            return True
        auth = self.headers.get("Authorization") or ""
        given = auth[7:].strip() if auth[:7].lower() == "bearer " else (self.headers.get("X-RenderPool-Password") or "")
        if given and hmac.compare_digest(given.encode("utf-8"), pw.encode("utf-8")):
            return True
        want = self._cookie_value(pw)
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "renderpool_login" and hmac.compare_digest(v.encode("utf-8"), want.encode("utf-8")):
                return True
        return False

    def _refuse_auth(self):
        return self._send(401, {"error": "password required (Authorization: Bearer <password>)"},
                          headers={"WWW-Authenticate": 'Bearer realm="renderpool"'})

    def _login(self, data):
        pw = self.server.broker.cfg.get("password") or ""
        try:
            given = str((json.loads(data.decode("utf-8")) or {}).get("password") or "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            given = ""
        if not pw:
            return self._send(200, {"ok": True, "password": False})
        if not given or not hmac.compare_digest(given.encode("utf-8"), pw.encode("utf-8")):
            time.sleep(0.5)    # slows guessing
            return self._send(401, {"error": "wrong password"})
        return self._send(200, {"ok": True}, headers={
            "Set-Cookie": "renderpool_login=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                          % self._cookie_value(pw)})

    def _host_ok(self):
        """Refuse a request whose Host is not one of ours (DNS rebinding: a web page re-pointing its own name
        at this machine). Host names compare without the port. No Host header (non-browser) is fine."""
        h = self.headers.get("Host")
        if h is None:
            return True
        host = h.strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        cfg = self.server.broker.cfg
        return (host in LOOPBACK or host in cfg["listen"]
                or host in [e.partition(":")[0] for e in cfg["allowed_origins"]])

    def do_GET(self):
        broker = self.server.broker
        if not self._host_ok():
            return self._send(421, {"error": "misdirected request: unknown Host"})
        path = urllib.parse.urlparse(self.path).path
        if not self._auth_ok(path):
            return self._refuse_auth()
        if path == "/":
            status, obj = broker.liveness()
            return self._send(status, obj)
        if path.startswith("/renderpool/api/result/"):
            try:
                item_id = int(path[len("/renderpool/api/result/"):])
            except ValueError:
                return self._send(400, {"error": "result needs an item number"})
            st, ctype, body, headers = broker.result(item_id)
            return self._send(st, body, ctype, headers)
        if path.startswith("/renderpool/api/group/"):
            group = urllib.parse.unquote(path[len("/renderpool/api/group/"):])
            return self._send(200, broker.group_status(group))
        if path == "/renderpool":
            self.send_response(302)
            self.send_header("Location", "/renderpool/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/renderpool/":
            try:
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "static/index.html missing"})
        if path == "/renderpool/api/boxes":
            return self._send(200, broker.boxes_detail())
        if path in ("/renderpool/api/state", "/renderpool/api/list", "/renderpool/api/status"):
            return self._send(200, broker.state())
        if path.startswith("/renderpool/img/"):
            name = urllib.parse.unquote(path[len("/renderpool/img/"):])
            ctype = {".png": "image/png", ".jpg": "image/jpeg"}.get(os.path.splitext(name)[1])
            base = os.path.realpath(broker.cfg["outputs_dir"])
            full = os.path.realpath(os.path.join(base, name))
            if (not ctype or not FILE_RE.match(name) or ".." in name or name.startswith(".")
                    or not full.startswith(base + os.sep) or not os.path.isfile(full)):
                return self._send(404, {"error": "no such image"})
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)
        return self._send(404, {"error": "not found"})

    def _read_body(self):
        n = self.headers.get("Content-Length")
        if n is None:
            return None
        return self.rfile.read(int(n))

    def do_POST(self):
        broker = self.server.broker
        path = urllib.parse.urlparse(self.path).path
        if not self._host_ok():
            return self._send(421, {"error": "misdirected request: unknown Host"})
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin request refused"})
        if not self._auth_ok(path):
            return self._refuse_auth()
        data = self._read_body()
        if data is None:
            return self._send(411, {"error": "Content-Length required"})
        if path == "/renderpool/api/login":
            return self._login(data)
        if path in ("/sdapi/v1/txt2img", "/sdapi/v1/img2img"):
            h = self.headers
            try:
                max_wait = float(h.get("X-RenderPool-Max-Wait") or broker.cfg["default_max_wait_s"])
            except ValueError:
                max_wait = float(broker.cfg["default_max_wait_s"])
            try:
                priority = parse_priority(h.get("X-RenderPool-Priority"))
            except ValueError:
                return self._send(400, {"error": "X-RenderPool-Priority must be high, normal, low or -9..9"})
            kind, val = broker.api_submit(path.rsplit("/", 1)[1], data,
                                          source=(h.get("X-RenderPool-Source") or "api").strip().lower(),
                                          group=h.get("X-RenderPool-Group") or "", label=h.get("X-RenderPool-Label") or "",
                                          pin=h.get("X-RenderPool-Backend") or "", key=(h.get("X-RenderPool-Key") or "").strip(),
                                          seat=clean_seat(h.get("X-RenderPool-Seat")), priority=priority)
            if kind == "reply":
                res = val
            else:
                res = broker.wait_result(val[0], val[1], self.connection, max_wait if max_wait > 0 else None)
                if res is None:
                    return None  # caller is gone; nothing to answer
            return self._send(res[0], res[2], res[1], res[3])
        if path.startswith("/renderpool/api/"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                return self._send(415, {"error": "Content-Type must be application/json"})
            try:
                args = json.loads(data.decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "body is not JSON"})
            ua = self.headers.get("User-Agent") or ""
            who = "%s %s" % (self.client_address[0], (self.headers.get("X-RenderPool-Who") or "").strip()
                             or ("ctl" if ua.startswith("Python-urllib") else "browser"))
            status, obj = broker.control(path[len("/renderpool/api/"):], args, who=who)
            return self._send(status, obj)
        return self._send(404, {"error": "not found"})


# ---------------------------------------------------------------------------------------------- CLI

def _auth_headers():
    """ctl sends the password from RENDERPOOL_PASSWORD when the broker has one."""
    pw = os.environ.get("RENDERPOOL_PASSWORD")
    return {"Authorization": "Bearer " + pw} if pw else {}


def api(url, verb, args=None):
    if verb == "state":
        req = urllib.request.Request(url + "/renderpool/api/state", headers=_auth_headers())
    else:
        h = {"Content-Type": "application/json", "X-RenderPool-Who": "ctl %s" % (os.environ.get("USER") or "?")}
        h.update(_auth_headers())
        req = urllib.request.Request(url + "/renderpool/api/" + verb, data=jdump(args or {}).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _fmt_item(it):
    flags = []
    if it.get("paused"):
        flags.append("paused")
    if it.get("group_paused"):
        flags.append("group-paused")
    if it.get("pin"):
        flags.append("pin=" + it["pin"])
    if it.get("note"):
        flags.append(it["note"])
    size = "%sx%s" % (it.get("width"), it.get("height"))
    if it.get("priority"):
        flags.append("prio=%+d" % it["priority"])
    return "%5s  %-8s %-12s %-10s %-14s %-28s %-10s %s %s" % (
        it["id"], it["state"], (it.get("seat") or "?")[:12], it["source"], it["group"] or "-", (it["model"] or "-")[:28], size,
        (it["label"] or it["prompt"] or "")[:40], ("[" + ", ".join(flags) + "]") if flags else "")


def ctl_main(argv):
    p = argparse.ArgumentParser(prog="renderpool.py ctl", description="Control a running RenderPool.")
    p.add_argument("--url", help="broker base URL (default: from --config, else http://127.0.0.1:7875)")
    p.add_argument("--config")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="verb")
    sub.required = True
    sub.add_parser("list")
    sub.add_parser("status")
    for v in ("pause", "resume"):
        sp = sub.add_parser(v)
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--group")
        g.add_argument("--item", type=int)
    for v in ("front", "up", "down"):
        sub.add_parser(v).add_argument("item", type=int)
    sp = sub.add_parser("move")
    sp.add_argument("item", type=int)
    sp.add_argument("pos", type=int)
    sp = sub.add_parser("cancel")
    sp.add_argument("item", type=int, nargs="?")
    sp.add_argument("--group")
    sp = sub.add_parser("pin")
    sp.add_argument("item", type=int)
    sp.add_argument("backend", nargs="?", default="", help="omit to unpin")
    sp = sub.add_parser("backend", help="same as: pool NAME image on|off")
    sp.add_argument("name")
    sp.add_argument("onoff", choices=("on", "off"))
    sp = sub.add_parser("pool")
    sp.add_argument("box")
    sp.add_argument("kind", choices=("image", "video"))
    sp.add_argument("onoff", choices=("on", "off"))
    sub.add_parser("refresh-models")
    sub.add_parser("group").add_argument("name")
    sp = sub.add_parser("bump", help="move every queued job with this seat/source/model/group to the front (or --to back)")
    g = sp.add_mutually_exclusive_group(required=True)
    for k in ("seat", "source", "model", "group"):
        g.add_argument("--" + k)
    sp.add_argument("--to", choices=("front", "back"), default="front")
    a = p.parse_args(argv)

    url = a.url
    if not url:
        cfg = load_config(a.config) if a.config else DEFAULTS
        url = "http://127.0.0.1:%d" % int(cfg["port"])
    url = url.rstrip("/")

    try:
        if a.verb in ("list", "status"):
            code, st = api(url, "state")
        elif a.verb in ("pause", "resume"):
            args = {}
            if a.group:
                args["group"] = a.group
            if a.item is not None:
                args["item"] = a.item
            code, st = api(url, a.verb, args)
        elif a.verb in ("front", "up", "down"):
            code, st = api(url, a.verb, {"item": a.item})
        elif a.verb == "move":
            code, st = api(url, "move", {"item": a.item, "pos": a.pos})
        elif a.verb == "cancel":
            if a.item is None and not a.group:
                p.error("cancel needs an item number or --group")
            code, st = api(url, "cancel", {"group": a.group} if a.group else {"item": a.item})
        elif a.verb == "pin":
            code, st = api(url, "pin", {"item": a.item, "backend": a.backend})
        elif a.verb == "bump":
            args = dict((k, getattr(a, k)) for k in ("seat", "source", "model", "group") if getattr(a, k))
            args["to"] = a.to
            code, st = api(url, "bump", args)
        elif a.verb == "group":
            req = urllib.request.Request(url + "/renderpool/api/group/" + urllib.parse.quote(a.name, safe=""),
                                         headers=_auth_headers())
            with urllib.request.urlopen(req, timeout=10) as r:
                code, st = r.status, json.loads(r.read().decode("utf-8"))
        elif a.verb == "backend":
            code, st = api(url, "pool", {"box": a.name, "kind": "image", "on": a.onoff == "on"})
        elif a.verb == "pool":
            code, st = api(url, "pool", {"box": a.box, "kind": a.kind, "on": a.onoff == "on"})
        else:
            code, st = api(url, a.verb, {})
    except (urllib.error.URLError, OSError) as e:
        sys.stderr.write("renderpool: no broker answering at %s (%s)\n" % (url, e))
        return 2

    if a.json or a.verb not in ("list", "status"):
        print(json.dumps(st, indent=2))
        return 0 if code == 200 else 1
    print("RenderPool %s  %s%s" % (st["version"], "PAUSED" if st["paused"] else "running",
                                ("  paused groups: " + ", ".join(st["paused_groups"])) if st["paused_groups"] else ""))
    for b in st["boxes"]:
        pools = ",".join(k for k in ("image", "video") if b["pools"].get(k)) or "no pool"
        print("  box %-10s %-12s %-4s %s%s" % (
            b["name"], pools, "up" if b["up"] else "DOWN",
            ("drawing #%s (%ss)" % (b["current"], b["elapsed"])) if b["busy"] else "idle",
            ("  models: %d" % len(b["models"])) if b["models"] is not None else "  models: any"))
    if a.verb == "status":
        print("  queued %d  running %d" % (len(st["queue"]), len(st["running"])))
        return 0
    if st["running"]:
        print("running:")
        for it in st["running"]:
            print(_fmt_item(it) + "  on %s %ss" % (it["backend"], it.get("seconds")))
    print("queue (%d):" % len(st["queue"]))
    for it in st["queue"]:
        print(_fmt_item(it))
    return 0


OPEN_FILES_WANTED = 8192


def raise_open_file_limit(wanted=OPEN_FILES_WANTED):
    """Every waiting caller holds one open connection until its picture is done, and launchd starts jobs with a
    soft open-file limit of 256; a caller that queues a whole round (e.g. 200 at once) would hit it. Raise the soft
    limit toward `wanted` (never above the hard limit). Returns (before, after)."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass
    return soft, resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def serve_main(argv):
    nofile = raise_open_file_limit()
    sys.stderr.write("renderpool open-file limit %s -> %s\n" % nofile)
    p = argparse.ArgumentParser(prog="renderpool.py serve", description="Run the RenderPool broker.")
    p.add_argument("--config", help="JSON config (see config.example.json)")
    p.add_argument("--port", type=int)
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    ov = {}
    if a.port is not None:
        ov["port"] = a.port
    if a.verbose:
        ov["verbose"] = True
    cfg = load_config(a.config, ov)
    broker = Broker(cfg).start()
    sys.stderr.write("renderpool %s listening on %s port %d  (page: /renderpool/)\n" % (VERSION, ", ".join(cfg["listen"]), broker.port))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        broker.stop()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: renderpool.py serve [--config FILE] [--port N] [--verbose]\n"
              "       renderpool.py ctl [--url URL] list|status|pause|resume|front|up|down|move|cancel|pin|backend|"
              "refresh-models ...\n"
              "       renderpool.py --version")
        return 0
    if argv[0] == "--version":
        print(VERSION)
        return 0
    if argv[0] == "serve":
        return serve_main(argv[1:])
    if argv[0] == "ctl":
        return ctl_main(argv[1:])
    sys.stderr.write("renderpool: unknown command %r (try serve or ctl)\n" % argv[0])
    return 2


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt()


def cli():
    """Console entry point (`renderpool`): the same as running renderpool.py, including clean shutdown on SIGTERM."""
    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)
    return main()


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)
    sys.exit(main())
