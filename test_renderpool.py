#!/usr/bin/env python3
"""Tests for renderpool.py. Standard library only; fake Draw Things backends on ephemeral loopback ports.

Run:  python3 -m unittest -v test_renderpool.py   (from this folder, or pass the path)
"""
import base64
import json
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boxes  # noqa: E402
import renderpool  # noqa: E402


def tiny_png():
    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_B64 = base64.b64encode(tiny_png()).decode()
TEST_MODELS = ("m1.ckpt", "m2.ckpt", "k_q6p.ckpt", "flux_2_klein_4b_q6p.ckpt")
EQUIV = {"k_q6p.ckpt": ["x.gguf", "k_q8p.ckpt"], "m2.ckpt": ["x.gguf"]}   # files a model_map may name
TEST_CLEARED = [dict({"file": m, "kind": "image", "label": m, "license": "MIT (test fixture)"},
                     equivalents=EQUIV.get(m, [])) for m in TEST_MODELS] + [
    {"file": "v1.ckpt", "kind": "video", "label": "a video model", "license": "MIT (test fixture)"}]
AC = (0, "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged;\n")
BATTERY = (0, "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging;\n")


class FakeShell(object):
    """Stands in for every command RenderPool runs for a box, so no test ever runs ssh, pmset, open or pgrep.

    handlers[(box, which)] or handlers[which] = (rc, stdout) | callable(argv) -> (rc, stdout) | "real".
    Anything without a handler fails (rc 1)."""

    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.lock = threading.Lock()

    def __call__(self, box, which, argv, timeout):
        with self.lock:
            self.calls.append((box, which, list(argv)))
        h = self.handlers.get((box, which), self.handlers.get(which))
        if h is None:
            return 1, ""
        if h == "real":
            return renderpool.run_box_cmd(box, which, argv, timeout)
        return h(argv) if callable(h) else h

    def count(self, box, which):
        with self.lock:
            return sum(1 for c in self.calls if c[0] == box and c[1] == which)


class FakeProc(object):
    def __init__(self, argv):
        self.argv = argv
        self.rc = None

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = -15

    def kill(self):
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


class FakeSpawn(object):
    """Stands in for the long-running ssh tunnel."""

    def __init__(self):
        self.procs = []

    def __call__(self, argv):
        p = FakeProc(list(argv))
        self.procs.append(p)
        return p


class FakeDT(object):
    """A fake Draw Things: GET / -> config JSON; POST /sdapi/v1/* -> sleeps `delay`, returns one image."""

    def __init__(self, delay=0.0, port=0):
        self.delay = delay
        self.port = port
        self.calls = []            # dicts: prompt, model, endpoint, t0, t1, body
        self.fail = None           # (status, bytes) to return instead of an image
        self.models = list(TEST_MODELS)   # served at GET /sdapi/v1/sd-models
        self.drop = False          # True: draw, then close the connection with no reply (the box fell off the network)
        self.images = None         # a list of base64 strings to return instead of one picture (e.g. video frames)
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.httpd = None

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/sdapi/v1/sd-models"):
                    return self._send(200, json.dumps([{"title": m, "model_name": m.rsplit(".", 1)[0],
                                                        "filename": "/models/" + m} for m in fake.models]).encode())
                self._send(200, b'{"model":"fake.ckpt","steps":8}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                rec = {"prompt": body.get("prompt"), "model": body.get("model"),
                       "endpoint": self.path.rsplit("/", 1)[1], "t0": time.time(), "body": body}
                with fake.lock:
                    fake.active += 1
                    fake.max_active = max(fake.max_active, fake.active)
                time.sleep(fake.delay)
                with fake.lock:
                    fake.active -= 1
                    rec["t1"] = time.time()
                    rec["dropped"] = fake.drop
                    fake.calls.append(rec)
                if fake.drop:
                    self.close_connection = True
                    return None
                if fake.fail:
                    self.send_response(fake.fail[0])
                    for k, v in (fake.fail[2] if len(fake.fail) > 2 else {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(fake.fail[1])))
                    self.end_headers()
                    self.wfile.write(fake.fail[1])
                    return None
                self._send(200, json.dumps({"images": [PNG_B64] if fake.images is None else fake.images}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def prompts(self):
        with self.lock:
            return [c["prompt"] for c in sorted(self.calls, key=lambda c: c["t0"])]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="renderpool-test-")
        self.fakes = []
        self.brokers = []
        self.sh = FakeShell()
        self.spawn = FakeSpawn()

    def tearDown(self):
        for b in self.brokers:
            if not b.stopping:
                b.stop()
        for f in self.fakes:
            f.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, delay=0.0, port=0):
        f = FakeDT(delay, port).start()
        self.fakes.append(f)
        return f

    def as_box(self, d):
        """Test shorthand {"name", "url", "models"?, "enabled"?, ...} -> a local box record on that fake."""
        d = dict(d)
        if "where" in d:
            return d
        rec = {"name": d.pop("name"), "where": "local", "engine": "a1111", "app": "other", "os": "macos",
               "abilities": ["image"]}
        rec["local_port"] = int(d.pop("url").rsplit(":", 1)[1])
        models = d.pop("models", None)
        for f in self.fakes:
            if f.port == rec["local_port"] and models is not None:
                f.models = list(models)
        rec["pools"] = {"image": bool(d.pop("enabled", True)), "video": False}
        rec.update(d)
        return rec

    def broker(self, backends, **kw):
        cfg = {"port": 0, "db_path": os.path.join(self.tmp, "state", "renderpool.db"),
               "outputs_dir": os.path.join(self.tmp, "outputs"), "probe_interval_s": 0.2,
               "pool_log_path": os.path.join(self.tmp, "state", "pool-changes.log"),
               "probe_timeout_s": 1, "down_retry_s": 0.2, "relaunch_wait_s": 1,
               "backends": [self.as_box(bk) for bk in backends],
               "cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"}
        cfg.update(kw)
        b = renderpool.Broker(renderpool.load_config(None, cfg), box_cmd=self.sh, spawn=self.spawn).start()
        self.brokers.append(b)
        self.base = "http://127.0.0.1:%d" % b.port
        return b

    def ctl(self, verb, **args):
        req = urllib.request.Request(self.base + "/renderpool/api/" + verb, data=json.dumps(args).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def state(self):
        with urllib.request.urlopen(self.base + "/renderpool/api/state", timeout=5) as r:
            return json.loads(r.read())

    def post(self, prompt, **kw):
        """Blocking POST like our callers do. -> (status, body bytes)."""
        return self.post_full(prompt, **kw)[:2]

    def post_full(self, prompt, model="m1.ckpt", endpoint="txt2img", headers=None, extra=None, timeout=30):
        """-> (status, body bytes, response headers)."""
        body = {"model": model, "prompt": prompt, "width": 512, "height": 512, "steps": 4, "seed": 1}
        body.update(extra or {})
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + "/sdapi/v1/" + endpoint, data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def raw_post(self, prompt, headers):
        """Send a POST on a raw socket (so the test can drop the connection). -> the socket."""
        body = json.dumps({"model": "m1.ckpt", "prompt": prompt, "width": 512, "height": 512}).encode()
        s = socket.create_connection(("127.0.0.1", self.brokers[-1].port))
        head = "".join("%s: %s\r\n" % kv for kv in headers.items())
        s.sendall(("POST /sdapi/v1/txt2img HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n%s"
                   "Content-Length: %d\r\n\r\n" % (head, len(body))).encode() + body)
        return s

    def post_bg(self, prompt, **kw):
        out = {}

        def run():
            out["result"] = self.post(prompt, **kw)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        out["thread"] = t
        return out

    def wait_until(self, cond, timeout=10, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if cond():
                    return
            except AssertionError:  # item not created yet
                pass
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def wait_queued(self, n):
        self.wait_until(lambda: len(self.state()["queue"]) == n, what="%d queued" % n)

    def enqueue_paused(self, prompts, headers=None):
        """Pause, POST each prompt in order (each confirmed queued before the next). -> {prompt: holder}."""
        self.ctl("pause")
        holders = {}
        for i, p in enumerate(prompts):
            hd = dict(headers or {})
            hd["X-RenderPool-Label"] = p
            holders[p] = self.post_bg(p, headers=hd)
            self.wait_queued(i + 1)
        return holders

    def item_id(self, label):
        st = self.state()
        for it in st["queue"] + st["running"] + st["history"]:
            if it["label"] == label:
                return it["id"]
        raise AssertionError("no item labeled %r" % label)

    def item(self, label):
        st = self.state()
        for it in st["queue"] + st["running"] + st["history"]:
            if it["label"] == label:
                return it
        raise AssertionError("no item labeled %r" % label)

    def join(self, holders, timeout=20):
        for h in holders.values():
            h["thread"].join(timeout)
            self.assertFalse(h["thread"].is_alive(), "caller still blocked")


class TestQueue(Base):
    def test_liveness_get_root(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        # exactly what l0-image-edit.py dt_up() does
        r = urllib.request.urlopen(self.base + "/", timeout=5)
        self.assertEqual(r.status, 200)
        d = json.loads(r.read())
        self.assertEqual(d["service"], "renderpool")
        self.assertEqual(d["boxes_up"], ["a"])

    def test_fifo(self):
        f = self.fake(0.05)
        self.broker([{"name": "a", "url": f.url}])
        h = self.enqueue_paused(["p1", "p2", "p3"])
        self.ctl("resume")
        self.join(h)
        self.assertEqual(f.prompts(), ["p1", "p2", "p3"])
        for v in h.values():
            self.assertEqual(v["result"][0], 200)
            self.assertEqual(json.loads(v["result"][1])["images"], [PNG_B64])

    def test_equal_priority_runs_in_arrival_order_and_a_bad_priority_is_refused(self):
        # priorities: equal ones keep arrival order, an unknown word is a 400 (never queued)
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        h = {}
        for i, label in enumerate(("j1", "j2", "j3")):
            hd = {"X-RenderPool-Label": label}
            if label == "j3":
                hd["X-RenderPool-Priority"] = "normal"
            h[label] = self.post_bg(label, headers=hd)
            self.wait_queued(i + 1)
        self.assertEqual(self.post("bad", headers={"X-RenderPool-Priority": "front"})[0], 400)
        self.assertEqual([q["label"] for q in self.state()["queue"]], ["j1", "j2", "j3"])
        self.ctl("resume")
        self.join(h)
        self.assertEqual(f.prompts(), ["j1", "j2", "j3"])

    def test_gui_submit_joins_end_of_queue(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        h = self.enqueue_paused(["long1", "long2"])
        code, res = self.ctl("submit", prompt="gui test", label="g", count=2, seed=10, width=512, height=512)
        self.assertEqual(code, 200)
        q = self.state()["queue"]
        self.assertEqual([x["label"] for x in q], ["long1", "long2", "g", "g"])
        self.assertEqual([x["position"] for x in q[2:]], [3, 4])
        self.assertEqual([x["seed"] for x in q[2:]], [10, 11])
        self.ctl("resume")
        self.join(h)

    def test_drag_move_up_down_front(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        h = self.enqueue_paused(["a", "b", "c", "d"])
        labels = lambda: [q["label"] for q in self.state()["queue"]]  # noqa: E731
        self.assertEqual(self.ctl("move", item=self.item_id("d"), pos=2)[0], 200)
        self.assertEqual(labels(), ["a", "d", "b", "c"])
        self.ctl("up", item=self.item_id("c"))
        self.assertEqual(labels(), ["a", "d", "c", "b"])
        self.ctl("down", item=self.item_id("a"))
        self.assertEqual(labels(), ["d", "a", "c", "b"])
        self.ctl("front", item=self.item_id("b"))
        self.assertEqual(labels(), ["b", "d", "a", "c"])
        self.ctl("resume")
        self.join(h)
        self.assertEqual(f.prompts(), ["b", "d", "a", "c"])

    def test_pause_all_lets_running_finish(self):
        f = self.fake(0.6)
        self.broker([{"name": "a", "url": f.url}])
        h = {"r1": self.post_bg("r1", headers={"X-RenderPool-Label": "r1"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="r1 running")
        h["r2"] = self.post_bg("r2", headers={"X-RenderPool-Label": "r2"})
        self.wait_queued(1)
        self.ctl("pause")
        h["r1"]["thread"].join(5)
        self.assertEqual(h["r1"]["result"][0], 200)          # the running picture finished
        time.sleep(0.5)
        self.assertEqual(f.prompts(), ["r1"])                 # nothing new started
        self.assertEqual(self.item("r2")["state"], "queued")
        self.ctl("resume")
        self.join(h)
        self.assertEqual(f.prompts(), ["r1", "r2"])

    def test_pause_group_and_resume(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause", group="storyapp-job10")
        h = {"g1": self.post_bg("g1", headers={"X-RenderPool-Group": "storyapp-job10", "X-RenderPool-Label": "g1"})}
        self.wait_queued(1)
        h["other"] = self.post_bg("other", headers={"X-RenderPool-Label": "other"})
        h["other"]["thread"].join(5)
        self.assertEqual(h["other"]["result"][0], 200)
        time.sleep(0.3)
        self.assertEqual(f.prompts(), ["other"])
        self.assertTrue(self.item("g1")["group_paused"])
        self.ctl("resume", group="storyapp-job10")
        self.join(h)
        self.assertEqual(f.prompts(), ["other", "g1"])

    def test_pause_item_and_resume(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        h = self.enqueue_paused(["x", "y"])
        self.ctl("pause", item=self.item_id("x"))
        self.ctl("resume")
        h["y"]["thread"].join(5)
        time.sleep(0.3)
        self.assertEqual(f.prompts(), ["y"])
        self.assertEqual(self.item("x")["state"], "queued")
        self.ctl("resume", item=self.item_id("x"))
        self.join(h)
        self.assertEqual(f.prompts(), ["y", "x"])

    def test_cancel_item_and_group(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        h = self.enqueue_paused(["keep", "drop"])
        h["g1"] = self.post_bg("g1", headers={"X-RenderPool-Group": "g", "X-RenderPool-Label": "g1"})
        h["g2"] = self.post_bg("g2", headers={"X-RenderPool-Group": "g", "X-RenderPool-Label": "g2"})
        self.wait_queued(4)
        self.assertEqual(self.ctl("cancel", item=self.item_id("drop"))[0], 200)
        code, res = self.ctl("cancel", group="g")
        self.assertEqual(len(res["canceled"]), 2)
        h["drop"]["thread"].join(5)
        self.assertEqual(h["drop"]["result"][0], 409)
        self.ctl("resume")
        self.join(h)
        self.assertEqual(f.prompts(), ["keep"])
        self.assertEqual(self.item("drop")["state"], "canceled")
        self.assertEqual(self.item("g2")["state"], "canceled")

    def test_cancel_running_is_refused(self):
        f = self.fake(0.5)
        self.broker([{"name": "a", "url": f.url}])
        h = {"r": self.post_bg("r", headers={"X-RenderPool-Label": "r"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        self.assertEqual(self.ctl("cancel", item=self.item_id("r"))[0], 409)
        self.join(h)
        self.assertEqual(h["r"]["result"][0], 200)

    def test_caller_disconnect_while_queued_is_abandoned(self):
        f = self.fake(0.02)
        b = self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        body = json.dumps({"model": "m1.ckpt", "prompt": "ghost", "width": 512, "height": 512}).encode()
        s = socket.create_connection(("127.0.0.1", b.port))
        s.sendall(b"POST /sdapi/v1/txt2img HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"X-RenderPool-Label: ghost\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        self.wait_queued(1)
        s.close()
        self.wait_until(lambda: self.item("ghost")["state"] == "abandoned", what="abandoned")
        self.ctl("resume")
        time.sleep(0.4)
        self.assertEqual(f.prompts(), [])

    def test_caller_disconnect_while_running_is_orphaned(self):
        f = self.fake(0.5)
        b = self.broker([{"name": "a", "url": f.url}])
        body = json.dumps({"model": "m1.ckpt", "prompt": "orphan", "width": 512, "height": 512}).encode()
        s = socket.create_connection(("127.0.0.1", b.port))
        s.sendall(b"POST /sdapi/v1/txt2img HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"X-RenderPool-Label: orphan\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        s.close()
        self.wait_until(lambda: self.item("orphan")["state"] == "orphaned", what="orphaned")
        it = self.item("orphan")
        self.assertEqual(len(f.calls), 1)  # it finished (the picture is not thrown away mid-draw)
        self.assertIsNone(it["full"])      # source "api" is not in keep_copies_for (a thumbnail only)

    def test_two_backends_in_parallel(self):
        fa, fb = self.fake(0.5), self.fake(0.5)
        self.broker([{"name": "a", "url": fa.url}, {"name": "b", "url": fb.url}])
        h = self.enqueue_paused(["p1", "p2"])
        t0 = time.time()
        self.ctl("resume")
        self.join(h)
        elapsed = time.time() - t0
        self.assertEqual(len(fa.calls), 1)
        self.assertEqual(len(fb.calls), 1)
        self.assertLess(elapsed, 0.9, "two pictures on two backends should overlap")
        self.assertLess(abs(fa.calls[0]["t0"] - fb.calls[0]["t0"]), 0.2)

    def test_model_aware_routing_no_head_of_line_blocking(self):
        fa, fb = self.fake(0.6), self.fake(0.05)
        self.broker([{"name": "a", "url": fa.url, "models": ["m1.ckpt", "m2.ckpt"]},
                     {"name": "b", "url": fb.url, "models": ["m2.ckpt"]}])
        self.ctl("pause")
        h = {}
        for label, model in (("i1", "m1.ckpt"), ("i2", "m1.ckpt"), ("i3", "m2.ckpt")):
            h[label] = self.post_bg(label, model=model, headers={"X-RenderPool-Label": label})
            self.wait_queued(len(h))
        self.ctl("resume")
        self.join(h)
        self.assertEqual([c["prompt"] for c in fa.calls], ["i1", "i2"])  # i2 waited for the only m1 holder
        self.assertEqual([c["prompt"] for c in fb.calls], ["i3"])        # i3 did not wait behind i2
        i1 = fa.calls[0]
        self.assertLess(fb.calls[0]["t0"], i1["t1"], "i3 should run while i1 is still drawing")
        self.assertEqual(self.item("i3")["backend"], "b")

    def test_pin_backend_header(self):
        fa, fb = self.fake(0.02), self.fake(0.02)
        self.broker([{"name": "a", "url": fa.url, "speed_rank": 1}, {"name": "b", "url": fb.url, "speed_rank": 50}])
        st, _ = self.post("pinned", headers={"X-RenderPool-Backend": "b"})
        self.assertEqual(st, 200)
        self.assertEqual(fb.prompts(), ["pinned"])
        self.assertEqual(self.post("bad", headers={"X-RenderPool-Backend": "nope"})[0], 400)

    def test_box_model_map_used(self):
        f = self.fake(0.02)
        self.broker([{"name": "b", "url": f.url, "models": ["x.gguf"], "model_map": {"k_q6p.ckpt": "x.gguf"}}])
        st, body = self.post("mapped", model="k_q6p.ckpt", extra={"guidance_scale": 2.5})
        self.assertEqual(st, 200)
        call = f.calls[0]
        self.assertEqual(call["model"], "x.gguf")
        self.assertEqual(call["body"]["guidance_scale"], 2.5)   # rest of the body untouched
        self.assertEqual(call["body"]["prompt"], "mapped")
        it = self.item("")  # API items without X-RenderPool-Label have an empty label
        self.assertEqual(it["model"], "k_q6p.ckpt")
        self.assertEqual(it["run_model"], "x.gguf")
        self.assertEqual(it["backend"], "b")

    def test_box_without_map_never_gets_other_quant(self):
        fa, fb = self.fake(0.01), self.fake(0.05)
        self.broker([{"name": "a", "url": fa.url, "speed_rank": 1, "models": ["flux_2_klein_4b_q8p.ckpt"]},
                     {"name": "b", "url": fb.url, "speed_rank": 50, "models": ["flux_2_klein_4b_q6p.ckpt"]}])
        for i in range(3):
            st, _, hd = self.post_full("q%d" % i, model="flux_2_klein_4b_q6p.ckpt")
            self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "b"))
        self.assertEqual(fa.calls, [])
        self.assertEqual([c["model"] for c in fb.calls], ["flux_2_klein_4b_q6p.ckpt"] * 3)

    def test_missing_model_gets_default_model(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}], default_model="m2.ckpt")
        body = json.dumps({"prompt": "no model", "width": 512, "height": 512}).encode()
        req = urllib.request.Request(self.base + "/sdapi/v1/txt2img", data=body,
                                     headers={"Content-Type": "application/json", "X-RenderPool-Label": "nm"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(f.calls[0]["model"], "m2.ckpt")
        self.assertEqual(self.item("nm")["model"], "m2.ckpt")
        # a page submit with no model gets it too
        code, res = self.ctl("submit", prompt="page", label="pg", width=512, height=512)
        self.assertEqual(code, 200)
        self.wait_until(lambda: self.item("pg")["state"] == "done", what="page item done")
        self.assertEqual(f.calls[1]["model"], "m2.ckpt")

    def test_default_model_must_be_cleared(self):
        with self.assertRaises(SystemExit):
            renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "other.ckpt"})
        with self.assertRaises(SystemExit):
            renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt",
                                   "model_aliases": [["a", "b"]]})

    def test_uncleared_model_refused_422(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url, "models": list(TEST_MODELS) + ["nope.ckpt"]}])
        st, body = self.post("bad", model="nope.ckpt")
        self.assertEqual(st, 422)
        self.assertEqual(json.loads(body), {"error": "RenderPool: model nope.ckpt is not on the cleared list"})
        code, res = self.ctl("submit", prompt="bad", model="nope.ckpt")
        self.assertEqual(code, 422)
        st = self.state()
        self.assertEqual((st["queue"], st["running"], st["history"]), ([], [], []))
        self.assertEqual(f.calls, [])

    def test_drawthings_local_models_listed(self):
        mdir = os.path.join(self.tmp, "models")
        os.makedirs(mdir)
        for n in ("a.ckpt", "b.ckpt", "notes.txt"):
            open(os.path.join(mdir, n), "w").close()
        f = self.fake()
        self.sh.handlers["models"] = "real"     # a plain local `ls` of the folder
        self.broker([{"name": "a", "where": "local", "local_port": f.port, "app": "drawthings"}],
                    drawthings_models_dir=mdir)
        self.assertEqual(self.state()["boxes"][0]["models"], ["a.ckpt", "b.ckpt"])
        self.assertEqual(self.sh.calls[0][2], ["ls", mdir])

    def test_422_passed_through_verbatim(self):
        f = self.fake(0.01)
        raw = b'{"detail":"init_images height doesn\'t match 1024 from the parameter 768"}'
        f.fail = (422, raw)
        self.broker([{"name": "a", "url": f.url}])
        st, body = self.post("bad size", endpoint="img2img", extra={"init_images": [PNG_B64], "strength": 0.6})
        self.assertEqual(st, 422)
        self.assertEqual(body, raw)
        self.assertEqual(f.calls[0]["endpoint"], "img2img")
        self.assertEqual(self.state()["history"][0]["state"], "failed")

    def test_backend_down_requeues_and_runs_when_back(self):
        port = free_port()
        f = self.fake(0.02, port=port)
        self.broker([{"name": "a", "url": f.url}])
        f.stop()
        h = {"wait": self.post_bg("wait", headers={"X-RenderPool-Label": "wait"})}
        time.sleep(1.0)
        it = self.item("wait")
        self.assertEqual(it["state"], "queued")       # not failed while the backend is down
        self.assertFalse(self.state()["boxes"][0]["up"])
        f2 = self.fake(0.02, port=port)
        self.join(h)
        self.assertEqual(h["wait"]["result"][0], 200)
        self.assertEqual(f2.prompts(), ["wait"])

    def test_backend_dies_at_dispatch_item_requeued(self):
        # probe_interval is long, so the broker still believes the backend is up and dispatches into a dead port
        port = free_port()
        f = self.fake(0.02, port=port)
        self.broker([{"name": "a", "url": f.url}], probe_interval_s=60)
        f.stop()
        h = {"x": self.post_bg("x", headers={"X-RenderPool-Label": "x"})}
        self.wait_until(lambda: self.item("x")["attempts"] >= 1 and self.item("x")["state"] == "queued",
                        what="requeued after a failed dispatch")
        self.assertIn("requeued", self.item("x")["error"])
        f2 = self.fake(0.02, port=port)
        self.join(h)
        self.assertEqual(h["x"]["result"][0], 200)
        self.assertEqual(f2.prompts(), ["x"])
        self.assertEqual(self.item("x")["state"], "done")

    def test_relaunch_runs_once_when_down(self):
        port = free_port()
        self.sh.handlers["models"] = (0, "m1.ckpt\n")
        self.sh.handlers["app_running"] = (1, "")   # Draw Things is not running
        self.sh.handlers["relaunch"] = (0, "")
        self.broker([{"name": "a", "where": "local", "local_port": port, "app": "drawthings"}], relaunch_wait_s=0.5)
        self.wait_until(lambda: self.sh.count("a", "relaunch") >= 1, what="relaunch ran")
        time.sleep(1.0)
        self.assertEqual(self.sh.count("a", "relaunch"), 1)   # once per down episode, not every probe
        self.assertIn(["open", "-g", "-a", "Draw Things"], [c[2] for c in self.sh.calls if c[1] == "relaunch"])
        self.fake(0.0, port=port)
        self.wait_until(lambda: self.state()["boxes"][0]["up"], what="box up")

    def test_backend_disable_enable(self):
        fa, fb = self.fake(0.02), self.fake(0.02)
        self.broker([{"name": "a", "url": fa.url}, {"name": "b", "url": fb.url, "enabled": False}])
        self.ctl("backend", name="a", enabled=False)
        self.ctl("backend", name="b", enabled=True)
        self.wait_until(lambda: self.state()["boxes"][1]["up"], what="b up")
        self.assertEqual(self.post("onb")[0], 200)
        self.assertEqual(fb.prompts(), ["onb"])
        self.assertEqual(fa.prompts(), [])

    def test_restart_recovery(self):
        f = self.fake(0.02)
        b1 = self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        h = {"api": self.post_bg("api", headers={"X-RenderPool-Label": "api"})}
        self.wait_queued(1)
        code, res = self.ctl("submit", prompt="gui picture", model="m1.ckpt", width=512, height=512, steps=4,
                             label="gui", count=1)
        self.assertEqual(code, 200)
        self.wait_queued(2)
        b1.stop()
        h["api"]["thread"].join(5)
        b2 = self.broker([{"name": "a", "url": f.url}])
        st = self.state()
        self.assertTrue(st["paused"])                       # pause survives a restart
        self.assertEqual([q["label"] for q in st["queue"]], ["gui"])
        self.assertEqual(self.item("api")["state"], "abandoned")
        self.ctl("resume")
        self.wait_until(lambda: self.item("gui")["state"] == "done", what="gui done")
        self.assertEqual(f.prompts(), ["gui picture"])
        full = self.item("gui")["full"]
        with urllib.request.urlopen(self.base + full, timeout=5) as r:
            self.assertEqual(r.read(), tiny_png())
        self.assertIsNotNone(b2)

    def test_gui_page_and_cross_origin(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        with urllib.request.urlopen(self.base + "/renderpool/", timeout=5) as r:
            self.assertIn(b"RenderPool", r.read())
        req = urllib.request.Request(self.base + "/renderpool/api/pause", data=b"{}",
                                     headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)
        self.assertFalse(self.state()["paused"])

    # ------------------------------------------------------------------ v2 review fixes (spec section 8)

    def test_fix1_max_wait_503_keeps_item_queued(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}], retry_after_s=7)
        self.ctl("pause", group="g")
        t0 = time.time()
        st, body, hd = self.post_full("slow", headers={"X-RenderPool-Max-Wait": "0.4", "X-RenderPool-Group": "g",
                                                       "X-RenderPool-Label": "slow", "X-RenderPool-Key": "k-slow"})
        self.assertEqual(st, 503)
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(hd["Retry-After"], "7")
        d = json.loads(body)
        self.assertEqual((d["renderpool"], d["position"], d["group_paused"]), ("queued", 1, True))
        self.assertEqual(self.item("slow")["state"], "queued")  # kept its place
        # a later submit with the same key re-attaches and gets the picture; painted once
        self.ctl("resume", group="g")
        st, body = self.post("slow", headers={"X-RenderPool-Group": "g", "X-RenderPool-Key": "k-slow"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["images"], [PNG_B64])
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(len([h for h in self.state()["history"] if h["label"] == "slow"]), 1)

    def test_fix1_max_wait_does_not_fire_once_started(self):
        f = self.fake(0.8)
        self.broker([{"name": "a", "url": f.url}])
        st, _ = self.post("long", headers={"X-RenderPool-Max-Wait": "0.2"})
        self.assertEqual(st, 200)

    def test_fix2_same_key_attaches_one_picture_two_waiters(self):
        f = self.fake(0.05)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        h = {"w1": self.post_bg("dup", headers={"X-RenderPool-Key": "k1", "X-RenderPool-Label": "dup"})}
        self.wait_queued(1)
        h["w2"] = self.post_bg("dup", headers={"X-RenderPool-Key": "k1", "X-RenderPool-Label": "dup"})
        time.sleep(0.3)
        self.assertEqual(len(self.state()["queue"]), 1)
        self.ctl("resume")
        self.join(h)
        self.assertEqual(h["w1"]["result"], h["w2"]["result"])
        self.assertEqual(h["w1"]["result"][0], 200)
        self.assertEqual(len(f.calls), 1)

    def test_fix2_key_replay_within_window_then_expires(self):
        f = self.fake(0.02)
        b = self.broker([{"name": "a", "url": f.url}], key_replay_s=0.6)
        st1, body1, h1 = self.post_full("once", headers={"X-RenderPool-Key": "k2"})
        st2, body2, h2 = self.post_full("once", headers={"X-RenderPool-Key": "k2"})
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(body1, body2)
        self.assertEqual(h2["X-RenderPool-Replay"], "1")
        self.assertEqual(h2["X-RenderPool-Backend"], "a")
        self.assertEqual(len(f.calls), 1)
        time.sleep(0.7)
        b.purge_replies()
        self.assertEqual(self.post("once", headers={"X-RenderPool-Key": "k2"})[0], 200)
        self.assertEqual(len(f.calls), 2)  # outside the window it is a new picture

    def test_fix2_no_key_still_paints_twice(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.post("x")
        self.post("x")
        self.assertEqual(len(f.calls), 2)

    def test_fix8_reconnect_with_same_key_after_drop_while_running(self):
        f = self.fake(0.8)
        self.broker([{"name": "a", "url": f.url}])
        s = self.raw_post("drop", {"X-RenderPool-Key": "k3", "X-RenderPool-Label": "drop"})
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        s.close()
        time.sleep(0.3)
        st, body = self.post("drop", headers={"X-RenderPool-Key": "k3", "X-RenderPool-Label": "drop"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["images"], [PNG_B64])
        self.assertEqual(len(f.calls), 1)                  # painted once
        self.assertEqual(self.item("drop")["state"], "done")  # someone did get it: not orphaned

    def test_fix8_reconnect_with_same_key_after_orphaned(self):
        f = self.fake(0.4)
        self.broker([{"name": "a", "url": f.url}])
        s = self.raw_post("late", {"X-RenderPool-Key": "k4", "X-RenderPool-Label": "late"})
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        s.close()
        self.wait_until(lambda: self.item("late")["state"] == "orphaned", what="orphaned")
        st, body = self.post("late", headers={"X-RenderPool-Key": "k4"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["images"], [PNG_B64])
        self.assertEqual(len(f.calls), 1)

    def test_root_200_even_with_all_boxes_out(self):
        port = free_port()
        fb = self.fake(0.0)
        self.broker([{"name": "down", "url": "http://127.0.0.1:%d" % port},
                     {"name": "off", "url": fb.url, "enabled": False}])
        r = urllib.request.urlopen(self.base + "/", timeout=5)      # callers read 200 here as "it is up"
        self.assertEqual(r.status, 200)
        d = json.loads(r.read())
        self.assertEqual(d["service"], "renderpool")
        self.assertEqual(d["boxes_up"], [])
        self.assertEqual(sorted(b["name"] for b in d["boxes"]), ["down", "off"])
        self.assertEqual(self.post("nothing can run this", model="m2.ckpt",
                                   headers={"X-RenderPool-Max-Wait": "0.2"})[0], 503)   # held, never a down root
        self.ctl("backend", name="off", enabled=True)
        self.wait_until(lambda: json.loads(urllib.request.urlopen(self.base + "/", timeout=5).read())["boxes_up"]
                        == ["off"], what="off up")

    def _root_ok(self):
        try:
            return urllib.request.urlopen(self.base + "/", timeout=5).status == 200
        except urllib.error.HTTPError:
            return False

    def test_fix4_group_status_endpoint(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        h = {"o": self.post_bg("o", headers={"X-RenderPool-Label": "o"})}
        self.wait_queued(1)
        for i in range(2):
            h["j%d" % i] = self.post_bg("j%d" % i, headers={"X-RenderPool-Group": "storyapp-job10", "X-RenderPool-Label": "j%d" % i})
            self.wait_queued(i + 2)
        self.ctl("pause", group="storyapp-job10")
        with urllib.request.urlopen(self.base + "/renderpool/api/group/storyapp-job10", timeout=5) as r:
            d = json.loads(r.read())
        self.assertEqual(d, {"group": "storyapp-job10", "paused": True, "all_paused": True, "queued": 2, "running": 0,
                             "position_of_first": 2})
        self.ctl("resume", group="storyapp-job10")
        self.ctl("resume")
        self.join(h)
        with urllib.request.urlopen(self.base + "/renderpool/api/group/storyapp-job10", timeout=5) as r:
            d = json.loads(r.read())
        self.assertEqual((d["queued"], d["running"], d["position_of_first"]), (0, 0, None))

    def test_fix5_response_headers_name_backend_and_model(self):
        f = self.fake(0.02)
        self.broker([{"name": "studio", "url": f.url, "models": ["k_q8p.ckpt"], "model_map": {"k_q6p.ckpt": "k_q8p.ckpt"}}])
        st, _, hd = self.post_full("h", model="k_q6p.ckpt")
        self.assertEqual(st, 200)
        self.assertEqual(hd["X-RenderPool-Backend"], "studio")
        self.assertEqual(hd["X-RenderPool-Model"], "k_q8p.ckpt")
        f.fail = (422, b'{"detail":"size"}')
        st, body, hd = self.post_full("h2", model="k_q6p.ckpt")
        self.assertEqual((st, body, hd["X-RenderPool-Backend"]), (422, b'{"detail":"size"}', "studio"))

    def test_fix5_group_affinity_prefers_last_backend(self):
        fa, fb = self.fake(0.02), self.fake(0.5)
        self.broker([{"name": "fast", "url": fa.url, "speed_rank": 0.1},
                     {"name": "other", "url": fb.url, "speed_rank": 5}])
        # first picture of the group is pinned to "other"; the next one follows it there (expected wait < 120 s)
        h = {"a": self.post_bg("a", headers={"X-RenderPool-Group": "job", "X-RenderPool-Backend": "other", "X-RenderPool-Label": "a"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="a running")
        st, _, hd = self.post_full("b", headers={"X-RenderPool-Group": "job", "X-RenderPool-Label": "b"})
        self.join(h)
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "other"))
        self.assertEqual(fa.prompts(), [])
        # an ungrouped picture still takes the fast idle backend
        self.assertEqual(self.post_full("c")[2]["X-RenderPool-Backend"], "fast")

    def test_fix5_group_affinity_gives_up_past_max_wait(self):
        fa, fb = self.fake(0.02), self.fake(0.8)
        self.broker([{"name": "fast", "url": fa.url, "speed_rank": 1},
                     {"name": "other", "url": fb.url, "speed_rank": 500}], affinity_max_wait_s=120)
        h = {"a": self.post_bg("a", headers={"X-RenderPool-Group": "job", "X-RenderPool-Backend": "other", "X-RenderPool-Label": "a"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="a running")
        t0 = time.time()
        st, _, hd = self.post_full("b", headers={"X-RenderPool-Group": "job"})
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "fast"))   # 500 s expected > 120 s: don't wait
        self.assertLess(time.time() - t0, 0.5)
        self.join(h)

    def test_fix5_group_affinity_off(self):
        fa, fb = self.fake(0.02), self.fake(0.5)
        self.broker([{"name": "fast", "url": fa.url, "speed_rank": 1},
                     {"name": "other", "url": fb.url, "speed_rank": 5}], group_affinity=False)
        h = {"a": self.post_bg("a", headers={"X-RenderPool-Group": "job", "X-RenderPool-Backend": "other", "X-RenderPool-Label": "a"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="a running")
        self.assertEqual(self.post_full("b", headers={"X-RenderPool-Group": "job"})[2]["X-RenderPool-Backend"], "fast")
        self.join(h)

    def test_fix7_copies_only_for_kept_sources_and_label_not_prompt(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.post("secret scene text", headers={"X-RenderPool-Source": "storyapp", "X-RenderPool-Label": "job10 shot 3"})
        self.post("my own picture", headers={"X-RenderPool-Source": "me", "X-RenderPool-Label": "mine"})
        app, mine = self.item("job10 shot 3"), self.item("mine")
        self.assertTrue(app["thumb"])                      # every picture gets a History thumbnail ...
        self.assertIsNone(app["full"])                     # ... but a full copy only for kept sources
        self.assertTrue(mine["full"])
        full = [n for n in os.listdir(os.path.join(self.tmp, "outputs")) if "-thumb" not in n]
        self.assertEqual(full, [mine["full"].rsplit("/", 1)[1]])
        self.assertEqual(app["prompt"], "")                # no scene text in the API or GUI
        self.assertEqual(mine["prompt"], "")               # every API job hides its prompt
        with urllib.request.urlopen(self.base + "/renderpool/api/state", timeout=5) as r:
            self.assertNotIn(b"secret scene text", r.read())

    def test_fix9a_cancel_group_never_touches_running(self):
        f = self.fake(0.6)
        self.broker([{"name": "a", "url": f.url}])
        h = {"r": self.post_bg("r", headers={"X-RenderPool-Group": "storyapp-job3", "X-RenderPool-Label": "r"})}
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        h["q"] = self.post_bg("q", headers={"X-RenderPool-Group": "storyapp-job3", "X-RenderPool-Label": "q"})
        self.wait_queued(1)
        code, res = self.ctl("cancel", group="storyapp-job3")
        self.assertEqual(res["canceled"], [self.item_id("q")])
        self.join(h)
        self.assertEqual(h["r"]["result"][0], 200)          # a body, never a dropped socket
        self.assertEqual(h["q"]["result"][0], 409)
        self.assertEqual(self.item("r")["state"], "done")

    def test_added_latency_under_50ms(self):
        f = self.fake(0.0)
        self.broker([{"name": "a", "url": f.url}])
        body = json.dumps({"model": "m1.ckpt", "prompt": "t", "width": 512, "height": 512}).encode()

        def timed(url, n=20):
            ts = []
            for _ in range(n):
                req = urllib.request.Request(url + "/sdapi/v1/txt2img", data=body,
                                             headers={"Content-Type": "application/json"})
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                ts.append(time.time() - t0)
            ts.sort()
            return ts[len(ts) // 2]

        timed(self.base, 3)  # warm up
        direct = timed(f.url)
        via = timed(self.base)
        added = via - direct
        sys.stderr.write("\n  median direct %.1f ms, via queue %.1f ms, added %.1f ms\n"
                         % (direct * 1000, via * 1000, added * 1000))
        self.assertLess(added, 0.050)

    # ------------------------------------------------------------------ Task 6: two pools, power, routing

    def mk(self, name, f, abilities=("image",), pools=None, models=None, **kw):
        """A full local box record on fake f."""
        if models is not None:
            f.models = list(models)
        rec = {"name": name, "where": "local", "local_port": f.port, "app": "other", "abilities": list(abilities),
               "pools": pools if pools is not None else dict((k, k in abilities) for k in ("image", "video"))}
        rec.update(kw)
        return rec

    def test_image_and_video_pools_route_separately(self):
        fa, fb = self.fake(0.05), self.fake(0.05)
        both = ["m1.ckpt", "v1.ckpt"]
        self.broker([self.mk("a", fa, ("image", "video"), {"image": True, "video": False}, both, speed_rank=1),
                     self.mk("b", fb, ("image", "video"), {"image": False, "video": True}, both, speed_rank=50)])
        st, _, hd = self.post_full("pic", model="m1.ckpt")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "a"))
        st, _, hd = self.post_full("clip", model="v1.ckpt")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "b"))   # a is idle and faster, but not in the video pool
        self.assertEqual(fa.prompts(), ["pic"])
        self.assertEqual(fb.prompts(), ["clip"])

    def test_box_in_both_pools_never_double_booked(self):
        fa, fb = self.fake(1.0), self.fake(0.05)
        self.broker([self.mk("a", fa, ("image", "video"), None, ["m1.ckpt", "v1.ckpt"], speed_rank=1),
                     self.mk("b", fb, ("image",), None, ["m1.ckpt"], speed_rank=50)])
        h = {"video": self.post_bg("video", model="v1.ckpt", headers={"X-RenderPool-Label": "video"})}
        self.wait_until(lambda: self.item("video")["state"] == "running", what="video running on a")
        for i in range(3):
            h["i%d" % i] = self.post_bg("i%d" % i, headers={"X-RenderPool-Label": "i%d" % i})
        for i in range(3):
            self.wait_until(lambda i=i: self.item("i%d" % i)["state"] == "done", what="image %d done" % i)
        self.assertEqual(self.item("video")["state"], "running")      # the images did not wait for a
        self.assertEqual(sorted(fb.prompts()), ["i0", "i1", "i2"])
        self.join(h)
        self.assertEqual(fa.prompts(), ["video"])
        self.assertEqual(fa.max_active, 1)

    def test_uncheck_busy_box_finishes_then_stops(self):
        fa, fb = self.fake(0.6), self.fake(0.05)
        self.broker([self.mk("a", fa, ("image", "video"), None, ["m1.ckpt", "v1.ckpt"], speed_rank=1),
                     self.mk("b", fb, ("image",), None, ["m1.ckpt"], speed_rank=50)])
        h = {"x1": self.post_bg("x1", headers={"X-RenderPool-Label": "x1"})}
        self.wait_until(lambda: self.item("x1")["state"] == "running", what="x1 running")
        self.assertEqual(self.ctl("pool", box="a", kind="image", on=False)[0], 200)
        self.join(h)
        self.assertEqual(h["x1"]["result"][0], 200)                    # the running picture finished on a
        self.assertEqual(self.item("x1")["backend"], "a")
        st, _, hd = self.post_full("x2")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "b"))       # a is idle but out of the image pool
        st, _, hd = self.post_full("clip", model="v1.ckpt")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "a"))       # still in the video pool
        self.assertEqual(fa.prompts(), ["x1", "clip"])

    def test_pool_changes_are_logged_with_who(self):
        fa = self.fake(0.05)
        self.broker([self.mk("a", fa, ("image", "video"), None, ["m1.ckpt", "v1.ckpt"])])
        self.assertEqual(self.ctl("pool", box="a", kind="image", on=False)[0], 200)
        self.assertEqual(self.ctl("pool", box="a", kind="image", on=False)[0], 200)   # no change: no line
        self.assertEqual(self.ctl("pool", box="a", kind="video", on=True)[0], 200)    # already on: no line
        with open(os.path.join(self.tmp, "state", "pool-changes.log")) as f:
            lines = [l.rstrip("\n").split("\t") for l in f]
        self.assertEqual(lines[0][1:4], ["-", "-", "-"])                  # the start line lists every box
        self.assertIn("a=image,video", lines[0][5])
        changes = lines[1:]
        self.assertEqual([c[1:4] for c in changes], [["a", "image", "OFF"]])
        self.assertTrue(changes[0][4].startswith("127.0.0.1 "))           # who: the caller's address + kind

    def test_pool_log_trims_lines_older_than_keep_days(self):
        fa = self.fake(0.05)
        b = self.broker([self.mk("a", fa)], pool_log_keep_days=14)
        path = os.path.join(self.tmp, "state", "pool-changes.log")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 15 * 86400))
        with open(path) as f:
            fresh = f.read()
        with open(path, "w") as f:
            f.write(old + "\ta\timage\tOFF\tx\t\n" + fresh)
        self.assertEqual(b.trim_pool_log(), 1)
        with open(path) as f:
            self.assertEqual(f.read(), fresh)                         # the 15-day-old line went, today's stayed
        self.assertEqual(b.trim_pool_log(), 0)

    def test_prompt_verb_returns_an_api_jobs_hidden_prompt(self):
        fa = self.fake(0.05)
        self.broker([self.mk("a", fa)])
        st, _, _ = self.post_full("a lighthouse at dawn", headers={"X-RenderPool-Label": "lh"},
                                  extra={"negative_prompt": "blurry"})
        self.assertEqual(st, 200)
        it = self.item("lh")
        self.assertEqual(it["prompt"], "")                              # /state still hides an API job's prompt
        code, j = self.ctl("prompt", item=it["id"])
        self.assertEqual((code, j["prompt"], j["negative_prompt"]), (200, "a lighthouse at dawn", "blurry"))
        self.assertEqual(self.ctl("prompt", item=99999)[0], 400)        # unknown item: a clean 400

    @unittest.skipUnless(shutil.which("sips"), "stretching uses macOS sips")
    def test_prompt_test_box_takes_img2img_as_text_only_and_stretches(self):
        fa, fn = self.fake(0.05), self.fake(0.05)
        b = self.broker([self.mk("studio", fa, pools={"image": False, "video": False}),
                         self.mk("npu", fn, models=["npu_model"])],
                    prompt_test={"box": "npu", "for_models": ["m1.ckpt"], "run_model": "npu_model"})
        st, body, hd = self.post_full("scene one", endpoint="img2img",
                                      extra={"init_images": [PNG_B64], "denoising_strength": 0.6,
                                             "width": 640, "height": 384})
        self.assertEqual((st, hd["X-RenderPool-Backend"], hd.get("X-RenderPool-Prompt-Test")), (200, "npu", "1"))
        call = fn.calls[-1]
        self.assertEqual((call["endpoint"], call["model"], call["prompt"]), ("txt2img", "npu_model", "scene one"))
        self.assertNotIn("init_images", call["body"])
        self.assertNotIn("denoising_strength", call["body"])
        png = base64.b64decode(json.loads(body)["images"][0])
        self.assertEqual(struct.unpack(">II", png[16:24]), (640, 384))   # stretched to the size asked for
        self.assertEqual(fa.calls, [])
        npu = b.box("npu")
        self.assertIsNone(b.prompt_test_file(npu, "m2.ckpt"))           # not in for_models: npu never takes it
        self.assertEqual(b.prompt_test_file(npu, "m1.ckpt"), "npu_model")

    def test_laptop_on_battery_finishes_then_stops(self):
        fa, fb = self.fake(0.8), self.fake(1.0)   # b is slower, so lap wins again once it is back on AC
        power = {"now": AC}
        self.sh.handlers[("lap", "on_ac")] = lambda argv: power["now"]
        self.broker([self.mk("lap", fa, laptop=True, speed_rank=1), self.mk("b", fb, speed_rank=50)],
                    power_interval_s=0.1)
        self.wait_until(lambda: self.box_state("lap")["on_ac"] is True, what="lap on AC")
        h = {"x1": self.post_bg("x1", headers={"X-RenderPool-Label": "x1"})}
        self.wait_until(lambda: self.item("x1")["state"] == "running", what="x1 running")
        self.assertEqual(self.item("x1")["backend"], "lap")
        power["now"] = BATTERY
        self.wait_until(lambda: self.box_state("lap")["on_ac"] is False, what="lap on battery")
        self.join(h)
        self.assertEqual(h["x1"]["result"][0], 200)                    # finished where it was, not drawn twice
        self.assertEqual(fa.prompts(), ["x1"])
        self.assertEqual(fb.prompts(), [])
        st, _, hd = self.post_full("x2")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "b"))
        power["now"] = AC
        self.wait_until(lambda: self.box_state("lap")["on_ac"] is True, what="lap back on AC")
        st, _, hd = self.post_full("x3")
        self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "lap"))     # rejoins when plugged back in

    def test_power_unreadable_means_out_of_pool(self):
        for answer in ((255, ""), (0, "something odd"), (-1, "")):
            fa, fb = self.fake(0.01), self.fake(0.01)
            self.sh.handlers[("lap", "on_ac")] = answer
            self.broker([self.mk("lap", fa, laptop=True, speed_rank=1), self.mk("b", fb, speed_rank=50)],
                        power_interval_s=0.1)
            for i in range(2):
                st, _, hd = self.post_full("p%d" % i)
                self.assertEqual((st, hd["X-RenderPool-Backend"]), (200, "b"), answer)
            self.assertIs(self.box_state("lap")["on_ac"], False)
            self.assertEqual(self.box_state("lap")["state_text"], "on battery")
            self.assertEqual(fa.calls, [])
            self.brokers[-1].stop()
            os.remove(os.path.join(self.tmp, "state", "renderpool.db"))

    def test_box_drops_offline_mid_job_rerun_elsewhere_one_reply(self):
        fa, fb = self.fake(0.4), self.fake(0.6)
        fa.drop = True
        self.broker([self.mk("lap", fa, speed_rank=1), self.mk("studio", fb, speed_rank=50)], probe_interval_s=60)
        h = {"first": self.post_bg("x", headers={"X-RenderPool-Key": "k-drop", "X-RenderPool-Label": "x"})}
        self.wait_until(lambda: self.item("x")["backend"] == "lap", what="x on lap")
        fa.stop()                                                      # the lid closes: the tunnel is gone
        self.wait_until(lambda: self.item("x")["backend"] == "studio" and self.item("x")["state"] == "running",
                        what="x re-run on studio")
        h["retry"] = self.post_bg("x", headers={"X-RenderPool-Key": "k-drop", "X-RenderPool-Label": "x"})
        self.join(h)
        self.assertEqual(h["first"]["result"][0], 200)
        self.assertEqual(h["first"]["result"], h["retry"]["result"])
        self.assertEqual(len(fb.calls), 1)                             # one picture
        self.assertEqual(len([c for c in fa.calls if not c["dropped"]]), 0)
        self.assertEqual(self.item("x")["attempts"], 2)

    def test_box_that_says_down_mid_job_requeues_elsewhere_and_sits_out(self):
        # dt-grpc-bridge answers 503 + X-Box-Down when Draw Things stalls; its quick probe still passes,
        # so the job must go to the other box, not straight back
        fa, fb = self.fake(0.05), self.fake(0.3)
        fa.fail = (503, b'{"error":"Draw Things went silent"}', {"X-Box-Down": "1"})
        self.broker([self.mk("lap", fa, speed_rank=1), self.mk("mac", fb, speed_rank=50)], probe_interval_s=60,
                    down_retry_s=60)
        st, body = self.post("y", headers={"X-RenderPool-Label": "y"})[:2]
        self.assertEqual(st, 200)
        self.assertEqual(len(fa.calls), 1)                             # tried once on lap, not bounced back
        self.assertEqual(len(fb.calls), 1)
        self.assertEqual((self.item("y")["backend"], self.item("y")["attempts"]), ("mac", 2))
        self.assertIs(self.box_state("lap")["up"], False)

    def test_plain_503_without_box_down_is_the_jobs_answer(self):
        f = self.fake(0.01)
        f.fail = (503, b'{"error":"busy"}')
        self.broker([self.mk("a", f)])
        st = self.post("z", headers={"X-RenderPool-Label": "z"})[0]
        self.assertEqual(st, 503)
        self.assertEqual(len(f.calls), 1)

    def test_keyless_caller_box_drops_rerun_inside_same_request(self):
        fa, fb = self.fake(0.4), self.fake(0.05)
        fa.drop = True
        self.broker([self.mk("lap", fa, speed_rank=1), self.mk("studio", fb, speed_rank=50)], probe_interval_s=60)
        h = {"app": self.post_bg("scene", headers={"X-RenderPool-Label": "scene"})}   # a caller that sends no X-RenderPool-* header
        self.wait_until(lambda: self.item("scene")["backend"] == "lap", what="on lap")
        fa.stop()
        self.join(h)
        st, body = h["app"]["result"]
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["images"], [PNG_B64])
        self.assertEqual(len(fb.calls), 1)
        self.assertEqual(self.item("scene")["state"], "done")
        self.assertEqual(self.item("scene")["backend"], "studio")

    def test_max_wait_default_1800_and_reply_when_exceeded(self):
        self.assertEqual(renderpool.DEFAULTS["default_max_wait_s"], 1800)
        f = self.fake(0.01)
        b = self.broker([{"name": "a", "url": f.url}])
        self.assertEqual(b.cfg["default_max_wait_s"], 1800)
        self.assertEqual(b.cfg["run_timeout_s"], {"image": 900, "video": 3600})
        self.ctl("pause")
        st, body, hd = self.post_full("late", headers={"X-RenderPool-Max-Wait": "0.3", "X-RenderPool-Label": "late"})
        self.assertEqual(st, 503)
        self.assertEqual(json.loads(body)["error"], "RenderPool: still queued")
        self.assertEqual(self.item("late")["state"], "queued")
        b.cfg["default_max_wait_s"] = 0.3                              # the default applies with no header
        self.assertEqual(self.post("late2")[0], 503)

    def test_no_box_in_pool_holds_model_refused_422(self):
        fa, fb = self.fake(0.01), self.fake(0.01)
        self.broker([self.mk("a", fa, models=["m1.ckpt"]), self.mk("b", fb, pools={"image": False, "video": False},
                                                                    models=["m1.ckpt"])])
        st, body = self.post("x", model="m2.ckpt")
        self.assertEqual(st, 422)
        self.assertEqual(json.loads(body), {"error": "RenderPool: no box that can do image holds model m2.ckpt"})
        st, body = self.post("y", model="v1.ckpt")                    # no box can do video at all
        self.assertEqual(st, 422)
        self.assertEqual(self.ctl("submit", prompt="z", model="m2.ckpt")[0], 422)
        self.assertEqual(self.state()["queue"], [])
        self.assertEqual(self._root_status(), 200)                    # RenderPool itself is still up

    def test_failed_model_check_retries_on_probe_interval_not_inventory_interval(self):
        # a remote box's first check after a restart can hit "connection refused" (tunnel not up),
        # and its list stayed unknown for the whole 300 s inventory interval
        fa = self.fake(0.01)
        br = self.broker([self.mk("a", fa, models=["m1.ckpt"])], inventory_refresh_s=3600, probe_interval_s=0.2)
        b = [x for x in br.boxes if x.name == "a"][0]
        deadline = time.time() + 5
        while b.models is None and time.time() < deadline:
            time.sleep(0.05)
        with br.wake:                       # a check that just failed
            b.models, b.models_error, b.models_checked = None, "URLError: refused", time.time()
        deadline = time.time() + 3
        while b.models is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(sorted(b.models or []), ["m1.ckpt"])
        self.assertEqual(b.models_error, "")
        with br.wake:                       # a check that succeeded is not repeated before inventory_refresh_s
            b.models, b.models_checked = {"stale.ckpt"}, time.time()
        time.sleep(1.0)
        self.assertEqual(b.models, {"stale.ckpt"})

    def _settings_broker(self, fa, **kw):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            json.dump({"history_limit": 50, "keep_copies_for": ["me"], "other_key": "kept"}, f)
        os.chmod(path, 0o640)
        cfg = {"port": 0, "db_path": os.path.join(self.tmp, "state", "renderpool.db"),
               "outputs_dir": os.path.join(self.tmp, "outputs"), "probe_interval_s": 0.2,
               "pool_log_path": os.path.join(self.tmp, "state", "pool-changes.log"),
               "probe_timeout_s": 1, "down_retry_s": 0.2, "relaunch_wait_s": 1,
               "backends": [self.as_box(self.mk("a", fa, models=["m1.ckpt"]))],
               "cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"}
        cfg.update(kw)
        b = renderpool.Broker(renderpool.load_config(path, cfg), box_cmd=self.sh, spawn=self.spawn).start()
        self.brokers.append(b)
        self.base = "http://127.0.0.1:%d" % b.port
        return b, path

    def test_settings_apply_live_save_to_config_and_log_who(self):
        fa = self.fake(0.01)
        br, path = self._settings_broker(fa)
        for i in range(3):
            self.assertEqual(self.post("p%d" % i)[0], 200)
        self.assertEqual(len(self.state()["history"]), 3)
        st, j = self.ctl("settings", history_limit=2, keep_copies_for="Me, GUI ,me", default_max_wait_s=900)
        self.assertEqual(st, 200, j)
        self.assertEqual(j["settings"]["keep_copies_for"], ["me", "gui"])
        self.assertEqual(len(self.state()["history"]), 2)            # consumed live, no restart
        self.assertEqual(br.cfg["default_max_wait_s"], 900.0)
        with open(path) as f:
            saved = json.load(f)
        self.assertEqual(saved["history_limit"], 2)
        self.assertEqual(saved["other_key"], "kept")                  # the rest of config.json is untouched
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)        # mode kept across the replace
        self.assertTrue(os.path.exists(path + ".prev"))
        with open(br.cfg["pool_log_path"]) as f:
            log = f.read()
        self.assertIn("history_limit 50 -> 2", log)
        self.assertIn("setting", log)
        self.assertEqual(self.state()["settings"]["history_limit"], 2)

    def test_settings_reject_bad_values_all_or_nothing(self):
        fa = self.fake(0.01)
        br, path = self._settings_broker(fa)
        with open(path) as f:
            before = f.read()
        for args in ({"history_limit": 5, "history_keep_days": -1}, {"history_limit": 0}, {"history_limit": 2.5},
                     {"default_max_wait_s": 5}, {"pool_log_keep_days": "x"}, {"history_limit": True},
                     {"listen": ["0.0.0.0"]}, {"keep_copies_for": [1]}):
            st, j = self.ctl("settings", **args)
            self.assertEqual(st, 400, (args, j))
        self.assertEqual(br.cfg["history_limit"], 50)
        with open(path) as f:
            self.assertEqual(f.read(), before)
        st, j = self.ctl("settings", history_keep_days=0)              # 0 = keep forever is allowed
        self.assertEqual(st, 200, j)
        self.assertEqual(br.cfg["history_keep_days"], 0.0)

    def test_only_unchecked_boxes_hold_model_waits_with_note(self):
        fa, fb = self.fake(0.01), self.fake(0.01)
        self.broker([self.mk("a", fa, models=["m1.ckpt"]),
                     self.mk("b", fb, pools={"image": False, "video": False}, models=["m2.ckpt"])])
        h = {"w": self.post_bg("w", model="m2.ckpt", headers={"X-RenderPool-Label": "w"})}
        self.wait_queued(1)
        self.assertEqual(self.item("w")["note"], "waiting: only unchecked boxes hold this model")
        self.ctl("pool", box="b", kind="image", on=True)
        self.join(h)
        self.assertEqual(h["w"]["result"][0], 200)
        self.assertEqual(fb.prompts(), ["w"])

    # ------------------------------------------------------------------ Task 7: "needs you" (API switched off)

    def dt_box(self, port, notify_file=None, **kw):
        """A local Draw Things box on `port`, with a notify command that appends its message to notify_file."""
        self.sh.handlers["models"] = (0, "m1.ckpt\n")
        cfg = dict(relaunch_wait_s=0.3, down_retry_s=0.1, probe_interval_s=0.1)
        if notify_file:
            cfg["notify_cmd"] = [sys.executable, "-c",
                                 "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')", notify_file]
        cfg.update(kw)
        return self.broker([{"name": "dt", "where": "local", "local_port": port, "app": "drawthings"}], **cfg)

    def notes(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def test_api_off_app_running_needs_you_notifies_once(self):
        port, note = free_port(), os.path.join(self.tmp, "notified")
        self.sh.handlers["app_running"] = (0, "4242\n")      # Draw Things is open, its API Server is off
        self.dt_box(port, note)
        self.wait_until(lambda: self.box_state("dt")["needs_you"], what="needs you")
        st = self.box_state("dt")
        self.assertEqual(st["state_text"], "needs you: turn on API Server in Draw Things")
        cycles = self.sh.count("dt", "app_running")
        self.wait_until(lambda: self.sh.count("dt", "app_running") >= cycles + 3, what="3 more probe cycles")
        self.wait_until(lambda: len(self.notes(note)) >= 1, what="notified")
        self.assertEqual(self.notes(note), ["RenderPool: dt needs you - turn on API Server in Draw Things"])
        self.assertEqual(self.sh.count("dt", "relaunch"), 0)     # reopening a running app would not help
        self.assertEqual(self.post("waits", headers={"X-RenderPool-Max-Wait": "0.3"})[0], 503)

    def test_app_closed_relaunches_not_needs_you(self):
        port, note = free_port(), os.path.join(self.tmp, "notified")
        self.sh.handlers["app_running"] = (1, "")             # pgrep found nothing: the app is closed
        self.sh.handlers["relaunch"] = (0, "")
        self.dt_box(port, note)
        self.wait_until(lambda: self.sh.count("dt", "relaunch") == 1, what="relaunched")
        time.sleep(1.0)
        self.assertEqual(self.sh.count("dt", "relaunch"), 1)
        self.assertFalse(self.box_state("dt")["needs_you"])
        self.assertEqual(self.notes(note), [])

    def test_needs_you_clears_when_api_returns(self):
        port, note = free_port(), os.path.join(self.tmp, "notified")
        self.sh.handlers["app_running"] = (0, "4242\n")
        self.dt_box(port, note)
        self.wait_until(lambda: self.box_state("dt")["needs_you"], what="needs you")
        h = {"w": self.post_bg("w", headers={"X-RenderPool-Label": "w"})}
        self.fake(0.01, port=port)                            # someone turns the API Server back on
        self.wait_until(lambda: not self.box_state("dt")["needs_you"], what="needs you cleared")
        self.join(h)
        self.assertEqual(h["w"]["result"][0], 200)
        self.assertEqual(self.box_state("dt")["state_text"], "idle")
        self.assertEqual(len(self.notes(note)), 1)

    # ------------------------------------------------------------------ Task 8: tailnet access

    def test_listen_0000_refused(self):
        for ip in ("0.0.0.0", "::"):
            with self.assertRaises(SystemExit):
                renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt", "listen": [ip]})

    def test_listen_lan_ip_refused(self):
        for ip in ("192.168.1.20", "8.8.8.8", "100.128.0.1", "localhost"):
            with self.assertRaises(SystemExit):
                renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt",
                                       "listen": ["127.0.0.1", ip]})
        cfg = renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt",
                                     "listen": ["127.0.0.1", "100.64.0.1", "100.127.255.254", "::1"]})
        self.assertEqual(len(cfg["listen"]), 4)
        self.assertEqual(renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"})["listen"],
                         ["127.0.0.1"])

    def test_allow_lan_opens_private_lan_only(self):
        base = {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"}
        for ip in ("10.7.4.64", "192.168.1.20", "172.16.0.5"):
            cfg = renderpool.load_config(None, dict(base, allow_lan=True, listen=["127.0.0.1", ip]))
            self.assertIn(ip, cfg["listen"])
        for ip in ("0.0.0.0", "::", "8.8.8.8", "169.254.1.1", "fd00::1"):   # still never every address / public / link-local
            with self.assertRaises(SystemExit):
                renderpool.load_config(None, dict(base, allow_lan=True, listen=[ip]))
        with self.assertRaises(SystemExit):                                  # anything but literal true keeps it shut
            renderpool.load_config(None, dict(base, allow_lan="yes", listen=["10.7.4.64"]))

    def test_origin_tailnet_ip_allowed_evil_refused(self):
        f = self.fake()
        b = self.broker([{"name": "a", "url": f.url}], allowed_origins=["renderpool-host.example.ts.net", "other.example.ts.net:8443"])
        b.cfg["listen"].append("100.100.1.2")   # as if also bound on the tailnet IP (tests cannot bind one)
        P = b.port

        def post(origin):
            req = urllib.request.Request(self.base + "/renderpool/api/pause", data=b"{}",
                                         headers={"Content-Type": "application/json", "Origin": origin})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(post("http://100.100.1.2:%d" % P), 200)
        self.assertEqual(post("http://renderpool-host.example.ts.net:%d" % P), 200)   # no port in the entry = RenderPool's port
        self.assertEqual(post("https://other.example.ts.net:8443"), 200)       # an entry with its own port
        self.assertEqual(post("http://127.0.0.1:%d" % P), 200)
        self.assertEqual(post("http://localhost:%d" % P), 200)
        self.assertEqual(post("http://evil.example"), 403)
        self.assertEqual(post("http://100.100.1.3:%d" % P), 403)               # a tailnet IP we do not listen on
        self.assertEqual(post("http://renderpool-host.example.ts.net.evil.example:%d" % P), 403)

    def test_origin_on_another_port_refused(self):
        f = self.fake()
        b = self.broker([{"name": "a", "url": f.url}], allowed_origins=["renderpool-host.example.ts.net"])
        for origin in ("http://127.0.0.1:%d" % (b.port + 1), "http://localhost:9999", "http://127.0.0.1",
                       "http://renderpool-host.example.ts.net:9999", "https://other.example.ts.net:8443"):
            req = urllib.request.Request(self.base + "/renderpool/api/pause", data=b"{}",
                                         headers={"Content-Type": "application/json", "Origin": origin})
            with self.assertRaises(urllib.error.HTTPError, msg=origin) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 403, origin)
        self.assertFalse(self.state()["paused"])

    def test_foreign_host_header_refused_421(self):
        f = self.fake()
        b = self.broker([{"name": "a", "url": f.url}], allowed_origins=["renderpool-host.example.ts.net"])

        def get(path, host):
            req = urllib.request.Request(self.base + path, headers={"Host": host})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        for path in ("/renderpool/api/boxes", "/renderpool/api/state", "/renderpool/", "/"):
            self.assertEqual(get(path, "evil.example"), 421, path)         # a DNS-rebound name
            self.assertEqual(get(path, "evil.example:%d" % b.port), 421, path)
            self.assertEqual(get(path, "127.0.0.1:%d" % b.port), 200, path)  # a caller's GET / and the page
            self.assertEqual(get(path, "localhost:%d" % b.port), 200, path)
            self.assertEqual(get(path, "renderpool-host.example.ts.net:%d" % b.port), 200, path)
        req = urllib.request.Request(self.base + "/renderpool/api/pause", data=b"{}",
                                     headers={"Content-Type": "application/json", "Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 421)
        self.assertFalse(self.state()["paused"])

    def test_listen_two_addresses_share_one_broker(self):
        try:
            s6 = socket.socket(socket.AF_INET6)
            s6.bind(("::1", 0))
            s6.close()
        except OSError:
            self.skipTest("no IPv6 loopback here")
        f = self.fake()
        b = self.broker([{"name": "a", "url": f.url}], listen=["127.0.0.1", "::1"])
        self.assertEqual(len(b.httpds), 2)
        self.ctl("pause")
        with urllib.request.urlopen("http://[::1]:%d/renderpool/api/state" % b.port, timeout=5) as r:
            self.assertTrue(json.loads(r.read())["paused"])               # one broker behind both

    # ------------------------------------------------------------------ Task 9: the page

    def test_page_has_pools_and_boxes_tabs(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        with urllib.request.urlopen(self.base + "/renderpool/", timeout=5) as r:
            page = r.read().decode("utf-8")
        for needle in ('id="tab-pools"', 'id="tab-boxes"', 'id="pools"', 'id="boxes"', "/renderpool/api/state",
                       "/renderpool/api/boxes", "box-add", "box-edit", "box-remove", 'name="ssh_host"', 'name="model_map"',
                       "not cleared", "mapped to", "invalid_boxes", "2000", 'name="viewport"'):
            self.assertIn(needle, page)
        for needle in ("priority", 'data-bump="seat"', 'data-bump="model"', '"bump"'):
            self.assertIn(needle, page)

    def test_state_includes_box_state_text_and_pools(self):
        fa, fb = self.fake(0.6), self.fake()
        port = free_port()
        self.sh.handlers[("lap", "on_ac")] = BATTERY
        self.broker([self.mk("a", fa, ("image", "video"), {"image": True, "video": True}, ["m1.ckpt", "v1.ckpt"],
                             speed_rank=1),
                     self.mk("lap", fb, laptop=True),
                     {"name": "off", "url": "http://127.0.0.1:%d" % port},
                     self.mk("idle2", self.fake(), pools={"image": False, "video": False})])
        h = {"x": self.post_bg("x", headers={"X-RenderPool-Label": "x"})}
        self.wait_until(lambda: self.box_state("a")["busy"], what="a busy")
        st = self.state()
        by = dict((b["name"], b) for b in st["boxes"])
        self.assertEqual(by["a"]["pools"], {"image": True, "video": True})
        self.assertEqual(by["a"]["state_text"], "drawing")
        self.assertEqual(by["lap"]["state_text"], "on battery")
        self.assertEqual(by["off"]["state_text"], "offline")
        self.assertEqual(by["idle2"]["state_text"], "unchecked")
        self.assertEqual(st["running"][0]["kind"], "image")
        self.assertEqual([m["file"] for m in st["cleared_models"]], [m["file"] for m in TEST_CLEARED])
        self.join(h)
        self.assertEqual(self.box_state("a")["state_text"], "idle")
        with urllib.request.urlopen(self.base + "/renderpool/api/boxes", timeout=5) as r:
            detail = json.loads(r.read())
        a = [b for b in detail["boxes"] if b["name"] == "a"][0]
        self.assertIn({"file": "v1.ckpt", "cleared": True, "mapped_to": None, "role": "cleared"}, a["models_held"])
        self.assertEqual(detail["engines"], ["a1111"])

    def test_box_models_not_cleared_flagged(self):
        f = self.fake()
        self.broker([self.mk("a", f, models=["m1.ckpt", "x.gguf", "secret_9b.ckpt"], model_map={"m2.ckpt": "x.gguf"})])
        with urllib.request.urlopen(self.base + "/renderpool/api/boxes", timeout=5) as r:
            a = json.loads(r.read())["boxes"][0]
        self.assertEqual(a["models_held"], [{"file": "m1.ckpt", "cleared": True, "mapped_to": None, "role": "cleared"},
                                            {"file": "secret_9b.ckpt", "cleared": False, "mapped_to": None,
                                             "role": "unknown"},
                                            {"file": "x.gguf", "cleared": False, "mapped_to": "m2.ckpt",
                                             "role": "mapped"}])

    def test_pool_toggle_via_api_round_trip(self):
        f = self.fake()
        self.broker([self.mk("a", f, ("image", "video"))])
        self.wait_until(lambda: self.box_state("a")["state_text"] == "idle", what="idle")
        self.assertEqual(self.ctl("pool", box="a", kind="image", on=False)[0], 200)
        self.assertEqual(self.box_state("a")["pools"], {"image": False, "video": True})
        self.assertEqual(self.ctl("pool", box="a", kind="video", on=False)[0], 200)
        self.assertEqual(self.box_state("a")["state_text"], "unchecked")
        self.assertEqual(self.ctl("pool", box="a", kind="image", on=True)[0], 200)
        self.wait_until(lambda: self.box_state("a")["state_text"] == "idle", what="idle again")
        self.assertEqual(self.post("after")[0], 200)

    def test_api_prompts_hidden_by_default_page_prompts_shown(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}], hide_prompt_for=["shy"])
        self.post("customer scene words", headers={"X-RenderPool-Label": "api"})        # a caller with no X-RenderPool-* header at all
        self.ctl("submit", prompt="typed on the page", label="page")
        self.ctl("submit", prompt="shy words", label="shy", source="shy")
        self.wait_until(lambda: self.item("shy")["state"] == "done", what="done")
        self.assertEqual(self.item("api")["prompt"], "")
        self.assertEqual(self.item("page")["prompt"], "typed on the page")
        self.assertEqual(self.item("shy")["prompt"], "")
        with urllib.request.urlopen(self.base + "/renderpool/api/state", timeout=5) as r:
            raw = r.read()
        self.assertNotIn(b"customer scene words", raw)
        self.assertNotIn(b"shy words", raw)

    # ------------------------------------------------------------------ re-check reads fresh state

    def test_recheck_after_battery_while_unchecked_stays_out(self):
        fa = self.fake(0.01)
        power = {"now": AC}
        self.sh.handlers[("lap", "on_ac")] = lambda argv: power["now"]
        self.broker([self.mk("lap", fa, laptop=True)], power_interval_s=60)
        self.wait_until(lambda: self.box_state("lap")["on_ac"] is True, what="on AC")
        self.ctl("pool", box="lap", kind="image", on=False)
        power["now"] = BATTERY                               # unplugged while unchecked: nobody re-reads it
        h = {"w": self.post_bg("w", headers={"X-RenderPool-Label": "w", "X-RenderPool-Max-Wait": "1.5"})}
        self.wait_queued(1)
        self.ctl("pool", box="lap", kind="image", on=True)
        self.join(h)
        self.assertEqual(h["w"]["result"][0], 503)            # still queued: never sent to a laptop on battery
        self.assertEqual(fa.calls, [])
        self.assertIs(self.box_state("lap")["on_ac"], False)

    def test_recheck_after_going_offline_while_unchecked_probes_first(self):
        port = free_port()
        fa = self.fake(0.01, port=port)
        self.broker([self.mk("a", fa)], probe_interval_s=60)
        self.wait_until(lambda: self.box_state("a")["up"], what="up")
        self.ctl("pool", box="a", kind="image", on=False)
        fa.stop()                                            # went offline while unchecked
        h = {"w": self.post_bg("w", headers={"X-RenderPool-Label": "w", "X-RenderPool-Max-Wait": "1.5"})}
        self.wait_queued(1)
        self.ctl("pool", box="a", kind="image", on=True)
        self.join(h)
        self.assertEqual(h["w"]["result"][0], 503)
        self.assertEqual(self.item("w")["attempts"], 0)       # probed first, never dispatched into a dead port
        self.assertFalse(self.box_state("a")["up"])

    # ------------------------------------------------------------------ review fixes 4, 7: request bodies

    def test_other_model_bearing_keys_refused_422(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        cases = {"refiner_model": "secret_9b.ckpt", "loras": [{"file": "x_lora.safetensors", "weight": 1}],
                 "controls": [{"file": "ctl.ckpt"}], "upscaler": "realesrgan_x4.ckpt",
                 "face_restoration": "restore.ckpt", "image_prior_model": "prior.ckpt"}
        for key, value in cases.items():
            st, body = self.post("k", extra={key: value})
            self.assertEqual(st, 422, key)
            self.assertEqual(json.loads(body), {"error": "RenderPool: %s is not supported" % key})
        self.assertEqual(f.calls, [])
        self.assertEqual(self.state()["queue"], [])

    def test_empty_model_bearing_keys_pass_like_real_callers_send(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        # exactly the extras scripts/l0-image.sh sends today
        st, _ = self.post("storyapp", extra={"loras": [], "upscaler": None, "hires_fix": False, "refiner_model": "",
                                        "controls": [], "face_restoration": None})
        self.assertEqual(st, 200)

    def test_non_string_model_is_400(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        for bad in (["m1.ckpt"], {"f": 1}, 7, True):
            st, body = self.post("bad", model=bad)
            self.assertEqual((st, json.loads(body)), (400, {"error": "RenderPool: model must be a string"}), bad)
        self.assertEqual(self.ctl("submit", prompt="p", model=["m1.ckpt"])[0], 400)
        self.assertEqual(self.post("ok")[0], 200)            # and the handler is still alive

    def box_state(self, name):
        for b in self.state()["boxes"]:
            if b["name"] == name:
                return b
        raise AssertionError("no box %r" % name)

    def _root_status(self):
        try:
            return urllib.request.urlopen(self.base + "/", timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code


class TestHistory(Base):
    """Thumbnails for every finished job, Clear history, autoclear, fixed params, video jobs."""

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def outputs(self):
        d = os.path.join(self.tmp, "outputs")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def video_box(self, f, **kw):
        d = {"name": "v", "url": f.url, "abilities": ["image", "video"], "pools": {"image": True, "video": True}}
        d.update(kw)
        return d

    def test_api_job_gets_thumbnail_that_serves_and_prompt_stays_hidden(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        self.assertEqual(self.post("customer words", headers={"X-RenderPool-Source": "storyapp", "X-RenderPool-Label": "t1"})[0], 200)
        it = self.item("t1")
        self.assertTrue(it["thumb"].startswith("/renderpool/img/"))
        self.assertIsNone(it["full"])
        st, ctype, data = self.get(it["thumb"])
        self.assertEqual(st, 200)
        self.assertIn(ctype, ("image/jpeg", "image/png"))
        self.assertTrue(data[:3] == b"\xff\xd8\xff" or data[:8] == renderpool.PNG_MAGIC)
        self.assertEqual(it["prompt"], "")
        with urllib.request.urlopen(self.base + "/renderpool/api/state", timeout=5) as r:
            self.assertNotIn(b"customer words", r.read())

    def test_page_job_thumbnail_and_full_copy(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("submit", prompt="a cat", label="page")
        self.wait_until(lambda: self.item("page")["state"] == "done", what="done")
        it = self.item("page")
        self.assertTrue(it["thumb"] and it["full"] and it["thumb"] != it["full"])
        st, ctype, data = self.get(it["full"])
        self.assertEqual((st, ctype, data), (200, "image/png", tiny_png()))

    def test_thumb_falls_back_to_stored_picture_when_sips_cannot(self):
        d = os.path.join(self.tmp, "o")
        os.makedirs(d)
        self.assertIsNone(renderpool.make_thumb(b"not a picture", d, "x-thumb", 256))
        name = renderpool.make_thumb(tiny_png(), d, "y-thumb", 256)
        self.assertIn(name, ("y-thumb.jpg", "y-thumb.png"))
        self.assertEqual(sorted(os.listdir(d)), [name])          # the temporary source file is gone

    def test_failed_job_has_no_thumbnail(self):
        f = self.fake(0.01)
        f.fail = (500, b'{"error":"boom"}')
        self.broker([{"name": "a", "url": f.url}])
        self.post("p", headers={"X-RenderPool-Label": "bad"})
        self.assertIsNone(self.item("bad")["thumb"])
        self.assertEqual(self.outputs(), [])

    def test_img_refuses_paths_outside_outputs(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        with open(os.path.join(self.tmp, "secret.png"), "wb") as fh:
            fh.write(tiny_png())
        for path in ("/renderpool/img/../secret.png", "/renderpool/img/%2e%2e/secret.png", "/renderpool/img/..%2fsecret.png",
                     "/renderpool/img/x/../../secret.png", "/renderpool/img/.hidden.png", "/renderpool/img/a.txt"):
            self.assertEqual(self.get(path)[0], 404, path)

    # ---------------------------------------------------------------- clear history / autoclear

    def test_clear_history_deletes_finished_and_files_never_queued_or_running(self):
        f = self.fake(0.01)
        b = self.broker([{"name": "a", "url": f.url}])
        self.ctl("submit", prompt="one", label="one")
        self.ctl("submit", prompt="two", label="two")
        self.wait_until(lambda: self.item("two")["state"] == "done" and self.item("one")["state"] == "done", what="done")
        self.assertTrue(len(self.outputs()) >= 4)               # 2 copies + 2 thumbnails
        f.delay = 1.5
        self.ctl("submit", prompt="slow", label="slow")
        self.wait_until(lambda: self.item("slow")["state"] == "running", what="running")
        self.ctl("pause")
        self.ctl("submit", prompt="waiting", label="waiting")
        st, res = self.ctl("clear-history")
        self.assertEqual((st, res["deleted"]), (200, 2))
        s = self.state()
        self.assertEqual(s["history"], [])
        self.assertEqual([i["label"] for i in s["running"]], ["slow"])
        self.assertEqual([i["label"] for i in s["queue"]], ["waiting"])
        self.assertEqual(self.outputs(), [])
        self.wait_until(lambda: self.item("slow")["state"] == "done", what="slow done")
        self.assertEqual(len(self.outputs()), 2)                 # the running job still saved its picture
        self.assertEqual(b.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_autoclear_deletes_only_finished_jobs_older_than_keep_days(self):
        f = self.fake(0.01)
        b = self.broker([{"name": "a", "url": f.url}], history_keep_days=7)
        self.ctl("submit", prompt="old", label="old")
        self.ctl("submit", prompt="new", label="new")
        self.wait_until(lambda: self.item("new")["state"] == "done" and self.item("old")["state"] == "done", what="done")
        old = self.item("old")
        self.ctl("pause")
        self.ctl("submit", prompt="queued long ago", label="q")
        week = time.time() - 8 * 86400
        b.db.execute("UPDATE items SET finished=? WHERE label='old'", (week,))
        b.db.execute("UPDATE items SET created=? WHERE label='q'", (week,))
        b.autoclear()
        labels = [i["label"] for i in self.state()["history"]]
        self.assertEqual(labels, ["new"])
        self.assertEqual([i["label"] for i in self.state()["queue"]], ["q"])
        names = self.outputs()
        for url in (old["thumb"], old["full"]):
            self.assertNotIn(url.rsplit("/", 1)[1], names)
        self.assertEqual(len(names), 2)                          # "new": copy + thumbnail

    def test_autoclear_runs_on_start_and_zero_keeps_everything(self):
        f = self.fake(0.01)
        b = self.broker([{"name": "a", "url": f.url}], history_keep_days=0)
        self.post("p", headers={"X-RenderPool-Label": "x"})
        b.db.execute("UPDATE items SET finished=?", (time.time() - 400 * 86400,))
        b.autoclear()
        self.assertEqual(len(self.state()["history"]), 1)      # 0 = keep forever
        b.stop()
        self.broker([{"name": "a", "url": f.url}], history_keep_days=7)
        self.assertEqual(self.state()["history"], [])          # the start-up pass

    def test_history_keep_days_must_be_a_non_negative_number(self):
        for bad in (-1, "soon"):
            with self.assertRaises(SystemExit):
                renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt",
                                       "history_keep_days": bad})
        self.assertEqual(renderpool.load_config(None, {"cleared_models": TEST_CLEARED,
                                                "default_model": "m1.ckpt"})["history_keep_days"], 7)

    def test_page_has_clear_history_button_and_plain_hints(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}])
        page = self.get("/renderpool/")[2].decode()
        for want in ("Clear history", "confirm(", "how strictly the picture follows your words", "fixed",
                     "num_frames"):
            self.assertTrue(want in page, "page lacks %r" % want)
        self.assertFalse("<h2>New picture</h2>" in page, "old section name still there")

    # ---------------------------------------------------------------- fixed params

    def cleared_fixed(self, fixed):
        c = [dict(m) for m in TEST_CLEARED]
        c[0]["fixed"] = fixed                                    # m1.ckpt
        return c

    def test_fixed_params_override_api_and_page_values(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}], cleared_models=self.cleared_fixed({"guidance_scale": 1.0}))
        self.post("api", extra={"guidance_scale": 7.5})
        self.post("api2", model="m2.ckpt", extra={"guidance_scale": 7.5})   # a model without fixed: untouched
        self.ctl("submit", prompt="page", model="m1.ckpt", guidance_scale=4, label="page")
        self.wait_until(lambda: self.item("page")["state"] == "done", what="done")
        by = dict((c["prompt"], c["body"]) for c in f.calls)
        self.assertEqual(by["api"]["guidance_scale"], 1.0)
        self.assertEqual(by["api2"]["guidance_scale"], 7.5)
        self.assertEqual(by["page"]["guidance_scale"], 1.0)
        st = self.state()
        self.assertEqual([m.get("fixed") for m in st["cleared_models"] if m["file"] == "m1.ckpt"],
                         [{"guidance_scale": 1.0}])

    def test_fixed_guidance_drops_the_callers_alias(self):
        # Draw Things 422s "More than one key for guidance" on guidance_scale + cfg_scale.
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}], cleared_models=self.cleared_fixed({"guidance_scale": 1.0}))
        self.post("alias", extra={"cfg_scale": 1.0})        # already equal to the fixed value: still dropped
        self.post("alias2", model="m2.ckpt", extra={"cfg_scale": 3.0})   # no fixed: untouched
        self.wait_until(lambda: len(f.calls) >= 2, what="both sent")
        by = dict((c["prompt"], c["body"]) for c in f.calls)
        self.assertNotIn("cfg_scale", by["alias"])
        self.assertEqual(by["alias"]["guidance_scale"], 1.0)
        self.assertEqual(by["alias2"].get("cfg_scale"), 3.0)
        self.assertNotIn("guidance_scale", by["alias2"])

    def test_fixed_must_be_a_dict_of_simple_values(self):
        for bad in ([1], {"model": "x.ckpt"}, {"cfg_scale": [1]}, {"": 1}):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                renderpool.load_config(None, {"cleared_models": self.cleared_fixed(bad), "default_model": "m1.ckpt"})

    # ---------------------------------------------------------------- video

    def test_video_job_passes_through_and_gets_first_frame_thumbnail(self):
        f = self.fake(0.01)
        frames = [PNG_B64] * 5
        f.images = frames
        self.broker([self.video_box(f, models=list(TEST_MODELS) + ["v1.ckpt"])])
        status, body, hdrs = self.post_full("a clip", model="v1.ckpt", headers={"X-RenderPool-Label": "clip"},
                                            extra={"num_frames": 9, "fps": 24})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"images": frames})   # the engine's answer, unchanged
        sent = f.calls[-1]["body"]
        self.assertEqual((sent["num_frames"], sent["fps"], sent["model"]), (9, 24, "v1.ckpt"))
        it = self.item("clip")
        self.assertEqual((it["kind"], it["frames"]), ("video", 5))
        self.assertTrue(it["thumb"])
        self.assertIsNone(it["full"])                             # API source: no frames kept

    def test_video_page_job_keeps_frames_folder_and_clear_removes_it(self):
        f = self.fake(0.01)
        f.images = [PNG_B64] * 3
        self.broker([self.video_box(f, models=list(TEST_MODELS) + ["v1.ckpt"])])
        self.ctl("submit", prompt="clip", model="v1.ckpt", num_frames=9, fps=500, label="clip")
        self.wait_until(lambda: self.item("clip")["state"] == "done", what="done")
        it = self.item("clip")
        self.assertTrue(it["full"].endswith("-frames/f0001.png"))
        self.assertEqual(self.get(it["full"])[:2], (200, "image/png"))
        folder = it["full"].split("/")[-2]
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "outputs", folder))),
                         ["f0001.png", "f0002.png", "f0003.png"])
        sent = f.calls[-1]["body"]
        self.assertEqual((sent["num_frames"], sent["fps"]), (9, 30))   # clamped to Draw Things' range
        self.ctl("clear-history")
        self.assertEqual(self.outputs(), [])

    def test_video_without_pictures_gets_no_thumbnail(self):
        f = self.fake(0.01)
        f.images = []
        self.broker([self.video_box(f, models=list(TEST_MODELS) + ["v1.ckpt"])])
        self.assertEqual(self.post_full("x", model="v1.ckpt", headers={"X-RenderPool-Label": "v"})[0], 200)
        it = self.item("v")
        self.assertEqual((it["kind"], it["thumb"], it["frames"]), ("video", None, None))

    def test_video_run_timeout_is_the_video_clock(self):
        f = self.fake(0.01)
        b = self.broker([self.video_box(f, models=list(TEST_MODELS) + ["v1.ckpt"])],
                        run_timeout_s={"image": 900, "video": 3600})
        seen = []
        real = b.boxes[0].engine.run

        def spy(endpoint, body, timeout):
            seen.append(timeout)
            return real(endpoint, body, timeout)
        b.boxes[0].engine.run = spy
        self.post_full("x", model="v1.ckpt")
        self.post_full("y")
        self.assertEqual(seen, [3600.0, 900.0])

    def test_example_config_loads_and_clears_video_models_with_licences(self):
        cfg = renderpool.load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json"),
                              {"db_path": os.path.join(self.tmp, "x.db"), "outputs_dir": os.path.join(self.tmp, "o")})
        kinds = dict((m["file"], m["kind"]) for m in cfg["cleared_models"])
        self.assertEqual(kinds.get("ltx_2.3_22b_distilled_1.1_q6p.ckpt"), "video")
        self.assertEqual(kinds.get("minimax_h3_fl2va_i8x.ckpt"), "video")
        klein = [m for m in cfg["cleared_models"] if m["file"] == "flux_2_klein_4b_q6p.ckpt"][0]
        self.assertEqual(klein.get("fixed"), {"guidance_scale": 1.0})


STUDIO_FILES = ["flux_2_klein_4b_q6p.ckpt", "flux_2_vae_f16.ckpt", "gemma_3_12b_it_qat_q8p.ckpt",
             "ltx_2.3_22b_distilled_1.1_q6p.ckpt", "ltx_2.3_audio_video_vae_f16.ckpt",
             "ltx_2.3_spatial_upscaler_x1.5_f16.ckpt", "ltx_2.3_spatial_upscaler_x2_1.1_f16.ckpt",
             "minimax_h3_fl2va_i8x.ckpt", "minimax_h3_vae_f16.ckpt", "qwen_3_4b_q8p.ckpt", "qwen_3_vl_32b_50_i8x.ckpt",
             "qwen_3_vl_8b_instruct_i8x.ckpt", "qwen_image_2.1_turbo_i8x.ckpt", "qwen_image_2.1_vae_f16.ckpt"]
LAPTOP_FILES = ["flux_2_klein_4b_q6p.ckpt", "flux_2_vae_f16.ckpt", "qwen_3_4b_q8p.ckpt",
                 "qwen_3_vl_8b_instruct_i8x.ckpt", "qwen_image_2.1_turbo_q8p.ckpt", "qwen_image_2.1_vae_f16.ckpt"]


class TestTestTier(Base):
    """Every generation model a box holds that is not cleared can run as a TEST from RenderPool's own sources."""

    QWEN = "qwen_image_2.1_turbo_i8x.ckpt"

    def test_classify_real_draw_things_file_names(self):
        gen = dict((f, renderpool.classify_file(f)) for f in sorted(set(STUDIO_FILES + LAPTOP_FILES))
                   if renderpool.classify_file(f) != "support")
        self.assertEqual(gen, {"flux_2_klein_4b_q6p.ckpt": "image", "ltx_2.3_22b_distilled_1.1_q6p.ckpt": "video",
                               "minimax_h3_fl2va_i8x.ckpt": "video", "qwen_image_2.1_turbo_i8x.ckpt": "image",
                               "qwen_image_2.1_turbo_q8p.ckpt": "image"})
        self.assertIsNone(renderpool.classify_file("dreamshaper_8.ckpt"))            # the rule cannot tell: unknown
        self.assertEqual(renderpool.classify_file("dreamshaper_8.ckpt", {"dreamshaper_8.ckpt": "image"}), "image")
        self.assertEqual(renderpool.classify_file("wan_v2.2_a14b_i2v_q6p.ckpt"), "video")
        self.assertEqual(renderpool.classify_file("flux_1_canny_lora_f16.ckpt"), "support")

    def tier_broker(self, **kw):
        f = self.fake(0.01)
        b = self.broker([{"name": "a", "url": f.url,
                          "models": ["m1.ckpt", self.QWEN, "qwen_3_4b_q8p.ckpt", "secret_9b.ckpt"]}],
                        test_notes={"qwen_image_2.1": "Qwen RESEARCH license, non-commercial (test only)"}, **kw)
        return f, b

    def test_test_model_runs_for_page_and_test_sources_only(self):
        f, b = self.tier_broker()
        st, res = self.ctl("submit", prompt="try it", model=self.QWEN, label="page")
        self.assertEqual(st, 200, res)
        self.wait_until(lambda: self.item("page")["state"] == "done", what="done")
        it = self.item("page")
        self.assertTrue(it["test"])
        self.assertEqual(it["kind"], "image")
        self.assertEqual(f.calls[-1]["model"], self.QWEN)
        for hdr in ({}, {"X-RenderPool-Source": "storyapp"}, {"X-RenderPool-Source": "api"}):
            status, body = self.post("prod", model=self.QWEN, headers=hdr)
            self.assertEqual(status, 422, hdr)
            self.assertIn(b"not on the cleared list", body)
        self.assertEqual(self.post("mine", model=self.QWEN, headers={"X-RenderPool-Source": "me"})[0], 200)
        self.assertFalse(self.item("page")["test"] is False)
        self.assertEqual(self.post("cleared", headers={"X-RenderPool-Label": "c"})[0], 200)
        self.assertFalse(self.item("c")["test"])

    def test_support_unknown_and_unheld_files_are_never_run(self):
        f, b = self.tier_broker()
        for m in ("qwen_3_4b_q8p.ckpt", "secret_9b.ckpt", "qwen_image_2.1_turbo_q8p.ckpt"):
            self.assertEqual(self.ctl("submit", prompt="x", model=m)[0], 422, m)
        st = self.state()
        self.assertEqual(st["test_models"], [{"file": self.QWEN, "kind": "image", "boxes": ["a"],
                                              "note": "Qwen RESEARCH license, non-commercial (test only)"}])
        self.assertEqual(st["unknown_models"], [{"file": "secret_9b.ckpt", "boxes": ["a"]}])
        self.assertEqual(st["test_sources"], ["me", "gui"])
        self.assertEqual(f.calls, [])

    def test_test_kinds_override_offers_an_unknown_file(self):
        f, b = self.tier_broker(test_kinds={"secret_9b.ckpt": "video"})
        self.assertEqual([m["file"] for m in self.state()["test_models"]], [self.QWEN, "secret_9b.ckpt"])
        self.assertEqual(self.state()["unknown_models"], [])

    def test_empty_test_sources_turns_the_tier_off(self):
        f, b = self.tier_broker(test_sources=[])
        self.assertEqual(self.ctl("submit", prompt="x", model=self.QWEN)[0], 422)

    def test_bad_test_config_refused(self):
        base = {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"}
        for bad in ({"test_sources": "me"}, {"test_kinds": {"a.ckpt": "audio"}}, {"test_notes": {"a": 1}}):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                renderpool.load_config(None, dict(base, **bad))

    def test_page_labels_test_models(self):
        f, b = self.tier_broker()
        with urllib.request.urlopen(self.base + "/renderpool/", timeout=5) as r:
            page = r.read().decode()
        for want in ("TEST ONLY - not cleared for production", "unknown type", "test_models"):
            self.assertTrue(want in page, "page lacks %r" % want)


class TestSeatPriorityBump(Base):
    """See which seat queued a job, re-sort by seat or model, priorities, collect a result later."""

    def labels(self):
        return [it["label"] for it in self.state()["queue"]]

    def test_seat_is_shown_on_queue_and_running_lines(self):
        f = self.fake(0.6)
        self.broker([{"name": "a", "url": f.url}])
        self.post_bg("x", headers={"X-RenderPool-Seat": "alice", "X-RenderPool-Label": "run"})
        self.wait_until(lambda: len(self.state()["running"]) == 1, what="running")
        self.post_bg("y", headers={"X-RenderPool-Seat": "batch-script <b>", "X-RenderPool-Label": "wait"})
        self.wait_queued(1)
        st = self.state()
        self.assertEqual(st["running"][0]["seat"], "alice")
        self.assertEqual(st["queue"][0]["seat"], "batch-script b")        # unsafe characters stripped
        self.post_bg("z", headers={"X-RenderPool-Label": "anon"})
        self.wait_queued(2)
        self.assertEqual(self.item("anon")["seat"], "")

    def test_priority_header_places_and_bad_value_is_400(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.enqueue_paused(["n1", "n2"])
        self.post_bg("lo", headers={"X-RenderPool-Priority": "low", "X-RenderPool-Label": "lo"})
        self.wait_queued(3)
        self.post_bg("n3", headers={"X-RenderPool-Label": "n3"})
        self.wait_queued(4)
        self.post_bg("hi", headers={"X-RenderPool-Priority": "high", "X-RenderPool-Label": "hi"})
        self.wait_queued(5)
        self.post_bg("hi2", headers={"X-RenderPool-Priority": "7", "X-RenderPool-Label": "hi2"})
        self.wait_queued(6)
        self.assertEqual(self.labels(), ["hi2", "hi", "n1", "n2", "n3", "lo"])
        self.assertEqual(self.item("hi2")["priority"], 7)
        self.assertEqual(self.post("bad", headers={"X-RenderPool-Priority": "urgent"})[0], 400)
        self.assertEqual(self.post("bad", headers={"X-RenderPool-Priority": "12"})[0], 400)
        self.assertEqual(len(self.state()["queue"]), 6)          # the bad ones were never queued

    def test_bump_by_seat_and_model_keeps_their_order(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.ctl("pause")
        for i, (lab, seat, model) in enumerate([("a1", "A", "m1.ckpt"), ("b1", "B", "m1.ckpt"),
                                                 ("a2", "A", "m2.ckpt"), ("b2", "B", "m2.ckpt")]):
            self.post_bg(lab, model=model, headers={"X-RenderPool-Seat": seat, "X-RenderPool-Label": lab})
            self.wait_queued(i + 1)
        self.assertEqual(self.ctl("bump", seat="B"), (200, {"ok": True, "moved": 2, "to": "front", "seat": "B"}))
        self.assertEqual(self.labels(), ["b1", "b2", "a1", "a2"])
        self.assertEqual(self.ctl("bump", model="m2.ckpt", to="back")[1]["moved"], 2)
        self.assertEqual(self.labels(), ["b1", "a1", "b2", "a2"])
        self.assertEqual(self.ctl("bump", seat="nobody")[1]["moved"], 0)
        self.assertEqual(self.labels(), ["b1", "a1", "b2", "a2"])
        self.assertEqual(self.ctl("bump", seat="A", model="m1.ckpt")[0], 400)   # exactly one filter
        self.assertEqual(self.ctl("bump", seat="A", to="middle")[0], 400)
        self.ctl("resume")
        self.wait_until(lambda: len(f.calls) == 4, what="all painted")
        self.assertEqual([c["prompt"] for c in sorted(f.calls, key=lambda c: c["t0"])], ["b1", "a1", "b2", "a2"])

    def test_keyed_job_survives_a_dropped_caller_and_is_collected_by_number(self):
        f = self.fake(0.05)
        self.broker([{"name": "a", "url": f.url}], key_replay_s=60)
        self.ctl("pause")
        s = self.raw_post("keep", {"X-RenderPool-Key": "k-keep", "X-RenderPool-Label": "keep"})
        self.wait_queued(1)
        item = self.item_id("keep")
        with urllib.request.urlopen(self.base + "/renderpool/api/result/%d" % item, timeout=5) as r:
            self.assertEqual((r.status, json.loads(r.read())["position"]), (202, 1))
        s.close()
        time.sleep(0.4)
        self.assertEqual(self.item("keep")["state"], "queued")   # keyed: NOT abandoned
        self.ctl("resume")
        self.wait_until(lambda: self.item("keep")["state"] in ("done", "orphaned"), what="painted")
        with urllib.request.urlopen(self.base + "/renderpool/api/result/%d" % item, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["images"], [PNG_B64])
            self.assertEqual(r.headers["X-RenderPool-Replay"], "1")
        self.assertEqual(len(f.calls), 1)

    def test_result_unknown_and_unkept(self):
        f = self.fake(0.02)
        self.broker([{"name": "a", "url": f.url}])
        self.post("nokey", headers={"X-RenderPool-Label": "nokey"})
        for path, want in (("999999", 404), (str(self.item_id("nokey")), 410), ("abc", 400)):
            try:
                urllib.request.urlopen(self.base + "/renderpool/api/result/" + path, timeout=5)
                got = 200
            except urllib.error.HTTPError as e:
                got = e.code
            self.assertEqual(got, want, path)


class TestLinuxBox(unittest.TestCase):
    def rec(self, **kw):
        r = dict(name="pc", where="local", local_port=7860, remote_port=7860, model_map={}, engine="a1111",
                 app="other", os="linux",
                 abilities=["image"], laptop=True, speed_rank=10, pools={"image": True, "video": False})
        r.update(kw)
        return r

    def test_linux_box_is_valid_but_draw_things_still_needs_macos(self):
        self.assertEqual(boxes.validate(self.rec()), [])
        self.assertTrue(boxes.validate(self.rec(app="drawthings")))
        self.assertTrue(boxes.validate(self.rec(os="beos")))

    def test_linux_power_check_reads_mains_adapters(self):
        argv = boxes.commands(self.rec())["on_ac"]
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertTrue(boxes.parse_on_ac("linux", 0, "1\n"))
        self.assertTrue(boxes.parse_on_ac("linux", 0, "0\n1\n"))
        self.assertFalse(boxes.parse_on_ac("linux", 0, "0\n"))
        self.assertFalse(boxes.parse_on_ac("linux", 0, ""))          # no mains adapter found: out of the pool
        self.assertFalse(boxes.parse_on_ac("linux", 1, "1\n"))
        # run the real shell snippet against a fake sysfs tree
        root = tempfile.mkdtemp()
        for name, typ, online in (("AC", "Mains", "1"), ("BAT0", "Battery", "1"), ("USB", "USB", "0")):
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "type"), "w") as f:
                f.write(typ + "\n")
            with open(os.path.join(root, name, "online"), "w") as f:
                f.write(online + "\n")
        sh = boxes.LINUX_ON_AC_SH.replace("/sys/class/power_supply", root)
        out = subprocess.run(["sh", "-c", sh], capture_output=True, text=True)
        self.assertTrue(boxes.parse_on_ac("linux", out.returncode, out.stdout), out)
        with open(os.path.join(root, "AC", "online"), "w") as f:
            f.write("0\n")
        out = subprocess.run(["sh", "-c", sh], capture_output=True, text=True)
        self.assertFalse(boxes.parse_on_ac("linux", out.returncode, out.stdout), out)

    def test_remote_linux_power_check_is_one_quoted_command(self):
        cmd = boxes.commands(self.rec(where="ssh", ssh_host="me@pc.local", ssh_key="id_ed25519",
                                      remote_port=7860, local_port=7862))["on_ac"]
        self.assertEqual(cmd[-1], "sh -c " + shlex.quote(boxes.LINUX_ON_AC_SH))


class TestUpCheck(unittest.TestCase):
    def serve(self, status):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def engine(self, port):
        from engines.a1111 import A1111Engine
        return A1111Engine("http://127.0.0.1:%d" % port, {"name": "x", "local_port": port, "app": "other"})

    def test_api_only_server_with_no_page_at_root_counts_as_up(self):
        self.assertIs(self.engine(self.serve(200)).is_up(2), True)
        self.assertIs(self.engine(self.serve(404)).is_up(2), True)     # answered: alive
        self.assertIs(self.engine(self.serve(503)).is_up(2), False)    # answered: not ready


class TestPassword(Base):
    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                   headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_no_password_everything_open(self):
        self.broker([{"name": "a", "url": self.fake().url}])
        self.assertEqual(self.req("/renderpool/api/state")[0], 200)

    def test_password_guards_api_and_drawing_but_not_health_or_page(self):
        f = self.fake(0.01)
        self.broker([{"name": "a", "url": f.url}], password="s3cret-pw")
        self.assertEqual(self.req("/renderpool/api/state")[0], 401)
        self.assertEqual(self.req("/renderpool/api/state", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.req("/renderpool/api/state", headers={"Authorization": "Bearer s3cret-pw"})[0], 200)
        self.assertEqual(self.req("/renderpool/api/state", headers={"X-RenderPool-Password": "s3cret-pw"})[0], 200)
        self.assertEqual(self.req("/renderpool/api/pause", data={})[0], 401)
        st = self.req("/sdapi/v1/txt2img", data={"prompt": "p", "model": "m1.ckpt"})[0]
        self.assertEqual((st, len(f.calls)), (401, 0))
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(self.req("/renderpool/")[0], 200)

    def test_page_login_sets_a_cookie_that_is_not_the_password(self):
        self.broker([{"name": "a", "url": self.fake().url}], password="s3cret-pw")
        self.assertEqual(self.req("/renderpool/api/login", data={"password": "nope"})[0], 401)
        st, hd, _ = self.req("/renderpool/api/login", data={"password": "s3cret-pw"})
        self.assertEqual(st, 200)
        cookie = hd["Set-Cookie"].split(";")[0]
        self.assertTrue(cookie.startswith("renderpool_login="))
        self.assertNotIn("s3cret-pw", hd["Set-Cookie"])
        self.assertIn("HttpOnly", hd["Set-Cookie"])
        self.assertEqual(self.req("/renderpool/api/state", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.req("/renderpool/api/state", headers={"Cookie": "renderpool_login=abc"})[0], 401)

    def test_environment_password_wins_and_ctl_sends_it(self):
        base = {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt"}
        old = os.environ.get("RENDERPOOL_PASSWORD")
        os.environ["RENDERPOOL_PASSWORD"] = "from-env"
        try:
            self.assertEqual(renderpool.load_config(None, dict(base, password="from-file"))["password"], "from-env")
            self.assertEqual(renderpool._auth_headers(), {"Authorization": "Bearer from-env"})
        finally:
            if old is None:
                os.environ.pop("RENDERPOOL_PASSWORD")
            else:
                os.environ["RENDERPOOL_PASSWORD"] = old
        with self.assertRaises(SystemExit):
            renderpool.load_config(None, dict(base, password=123))


if __name__ == "__main__":
    unittest.main()
