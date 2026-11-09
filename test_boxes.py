#!/usr/bin/env python3
"""Tests for boxes.py and the box controls in renderpool.py. Fake engines and fake commands only.

Run:  python3 -m unittest -v test_boxes
"""
import json
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boxes  # noqa: E402
from test_renderpool import Base  # noqa: E402

BAD = (";", "$(", "`", " ", "\n")


def ssh_box(**kw):
    rec = {"name": "lap", "where": "ssh", "ssh_host": "user@laptop.example", "ssh_key": "id_ed25519",
           "remote_port": 7860, "local_port": 7861, "engine": "a1111", "app": "drawthings", "os": "macos",
           "abilities": ["image", "video"], "laptop": True, "model_map": {}, "pools": {"image": True, "video": False}}
    rec.update(kw)
    return boxes.normalize(rec)


class TestValidate(unittest.TestCase):
    def test_good_record_has_no_errors(self):
        self.assertEqual(boxes.validate(ssh_box()), [])

    def test_validate_refuses_shell_metacharacters_in_records(self):
        for field, good in (("name", "lap"), ("ssh_host", "user@laptop.example"), ("ssh_key", "id_ed25519")):
            for bad in BAD:
                for value in (good + bad + "x", bad + good):
                    errs = boxes.validate(ssh_box(**{field: value}))
                    self.assertTrue(errs, "%s=%r was accepted" % (field, value))

    def test_option_injection_refused(self):
        self.assertTrue(boxes.validate(ssh_box(ssh_host="-oProxyCommand@x")))
        self.assertTrue(boxes.validate(ssh_box(ssh_key="..")))
        self.assertTrue(boxes.validate(ssh_box(ssh_key="../id")))
        self.assertTrue(boxes.validate(ssh_box(extra="x")))   # unknown fields refused

    def test_commands_are_argv_lists(self):
        cmds = boxes.commands(ssh_box())
        self.assertEqual(set(cmds), {"tunnel", "models", "relaunch", "on_ac", "app_running"})
        for which, argv in cmds.items():
            self.assertIsInstance(argv, list, which)
            self.assertTrue(all(isinstance(x, str) for x in argv), which)
            self.assertEqual(argv[0], "ssh", which)
            # the host is its own argv element, right after "--", so it can never be read as an option
            self.assertEqual(argv[argv.index("--") + 1], "user@laptop.example", which)
            self.assertIn(os.path.join(os.path.expanduser("~/.ssh"), "id_ed25519"), argv, which)
        self.assertIn("7861:127.0.0.1:7860", cmds["tunnel"])
        self.assertEqual(cmds["on_ac"][-1], "pmset -g batt")
        self.assertEqual(cmds["app_running"][-1], "pgrep -x DrawThings")
        self.assertEqual(cmds["relaunch"][-1], "open -g -a 'Draw Things'")
        self.assertTrue(cmds["models"][-1].startswith("ls ~/Library/Containers/"))
        local = boxes.commands(boxes.normalize({"name": "studio", "local_port": 7860}))
        self.assertIsNone(local["tunnel"])
        self.assertIsNone(local["on_ac"])            # not a laptop
        self.assertEqual(local["app_running"], ["pgrep", "-x", "DrawThings"])
        self.assertEqual(local["relaunch"], ["open", "-g", "-a", "Draw Things"])
        other = boxes.commands(boxes.normalize({"name": "sd", "local_port": 7862, "app": "other"}))
        self.assertEqual((other["models"], other["relaunch"], other["app_running"]), (None, None, None))
        win = boxes.commands(ssh_box(app="other", os="windows"))
        self.assertIn("Win32_Battery", win["on_ac"][-1])

    def test_parse_on_ac(self):
        self.assertTrue(boxes.parse_on_ac("macos", 0, "Now drawing from 'AC Power'\n -InternalBattery-0 100%"))
        self.assertFalse(boxes.parse_on_ac("macos", 0, "Now drawing from 'Battery Power'\n"))
        self.assertFalse(boxes.parse_on_ac("macos", 255, "Now drawing from 'AC Power'"))
        self.assertFalse(boxes.parse_on_ac("macos", 0, ""))
        self.assertTrue(boxes.parse_on_ac("windows", 0, "2\r\n"))
        self.assertFalse(boxes.parse_on_ac("windows", 0, "1\r\n"))
        self.assertFalse(boxes.parse_on_ac("windows", 0, "garbage"))


class TestExampleConfig(unittest.TestCase):
    def test_example_config_loads_and_is_publishable(self):
        import renderpool
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "config.example.json")
        cfg = renderpool.load_config(path)
        self.assertEqual(cfg["port"], 7875)
        self.assertEqual(len(cfg["backends"]), 2)
        # only models cleared for commercial use; Qwen-Image 2.1 is research-license, never cleared
        self.assertEqual([m["file"] for m in cfg["cleared_models"]],
                         ["flux_2_klein_4b_q6p.ckpt", "flux_3_klein_q8p.ckpt", "ltx_2.3_22b_distilled_1.1_q6p.ckpt",
                          "minimax_h3_fl2va_i8x.ckpt"])
        self.assertFalse(any(m["file"].startswith("qwen_image") for m in cfg["cleared_models"]))
        with open(path) as f:
            raw = f.read()
        import re
        self.assertEqual(set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", raw)), {"127.0.0.1"})  # loopback only
        self.assertEqual(set(re.findall(r"[\w.-]+@[\w.-]+", raw)), {"user@laptop.local"})       # placeholder only
        for shape in ("/Users/", "/home/", "ts.net"):
            self.assertNotIn(shape, raw)


class TestLicenceCitation(unittest.TestCase):
    def test_cleared_model_needs_a_licence_citation(self):
        import renderpool
        base = {"file": "a.ckpt", "kind": "image", "label": "A"}
        for bad in (dict(base), dict(base, license=""), dict(base, license="   "), dict(base, license=None)):
            with self.assertRaises(SystemExit) as cm:
                renderpool.load_config(None, {"cleared_models": [bad], "default_model": "a.ckpt"})
            self.assertEqual(str(cm.exception), "RenderPool: cleared model a.ckpt has no license citation")
        cfg = renderpool.load_config(None, {"cleared_models": [dict(base, license="Apache-2.0 (model card LICENSE)")],
                                     "default_model": "a.ckpt"})
        self.assertEqual(cfg["cleared_models"][0]["license"], "Apache-2.0 (model card LICENSE)")


class TestBoxControls(Base):
    def test_validate_refuses_shell_metacharacters(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        for field, good in (("name", "lap"), ("ssh_host", "user@laptop.example"), ("ssh_key", "id_ed25519")):
            for bad in BAD:
                rec = dict(ssh_box(), **{field: good + bad + "x"})
                code, res = self.ctl("box-add", **rec)
                self.assertEqual(code, 400, "%s=%r" % (field, rec[field]))
                self.assertTrue(res["errors"])
        self.assertEqual([b["name"] for b in self.state()["boxes"]], ["a"])   # nothing was saved
        self.assertEqual(self.spawn.procs, [])                                 # and nothing was run
        self.assertEqual([c for c in self.sh.calls if c[0] != "a"], [])

    def test_seed_from_config_once_then_db_wins(self):
        fa, fb = self.fake(), self.fake()
        b1 = self.broker([{"name": "a", "url": fa.url}])
        b1.stop()
        self.broker([{"name": "a", "url": fa.url}, {"name": "b", "url": fb.url}])
        self.assertEqual([b["name"] for b in self.state()["boxes"]], ["a"])
        # a box added on the page is kept across a restart too
        code, res = self.ctl("box-add", name="b", where="local", local_port=fb.port, app="other")
        self.assertEqual(code, 200, res)
        self.brokers[-1].stop()
        self.broker([{"name": "a", "url": fa.url}])
        self.assertEqual([b["name"] for b in self.state()["boxes"]], ["a", "b"])

    def test_pool_checkbox_survives_restart(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        code, res = self.ctl("pool", box="a", kind="image", on=False)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.state()["boxes"][0]["pools"], {"image": False, "video": False})
        self.brokers[-1].stop()
        self.broker([{"name": "a", "url": f.url}])
        self.assertEqual(self.state()["boxes"][0]["pools"], {"image": False, "video": False})
        self.ctl("pool", box="a", kind="image", on=True)
        self.brokers[-1].stop()
        self.broker([{"name": "a", "url": f.url}])
        self.assertEqual(self.state()["boxes"][0]["pools"]["image"], True)

    def test_video_pool_refused_without_video_ability(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        code, res = self.ctl("pool", box="a", kind="video", on=True)
        self.assertEqual(code, 400)
        self.assertIn("video", res["error"])
        code, res = self.ctl("box-add", name="v", where="local", local_port=7999, app="other",
                             abilities=["image"], pools={"image": True, "video": True})
        self.assertEqual(code, 400)
        self.assertEqual(self.state()["boxes"][0]["pools"]["video"], False)
        self.assertEqual(len(self.state()["boxes"]), 1)

    def test_edit_and_remove(self):
        fa, fb = self.fake(), self.fake()
        self.broker([{"name": "a", "url": fa.url}])
        self.assertEqual(self.ctl("box-add", name="b", where="local", local_port=fa.port, app="other")[0], 400)
        self.assertEqual(self.ctl("box-add", name="b", where="local", local_port=fb.port, app="other")[0], 200)
        code, res = self.ctl("box-edit", name="b", where="local", local_port=fb.port, app="other", speed_rank=5)
        self.assertEqual(code, 200, res)
        self.assertEqual(self.ctl("box-edit", name="zz", where="local", local_port=1234, app="other")[0], 404)
        self.assertEqual(self.ctl("box-remove", name="b")[0], 200)
        self.assertEqual([b["name"] for b in self.state()["boxes"]], ["a"])

    def test_state_never_carries_ssh_host_or_key(self):
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        code, res = self.ctl("box-add", **ssh_box())
        self.assertEqual(code, 200, res)
        raw = json.dumps(self.state())
        self.assertNotIn("laptop.example", raw)
        self.assertNotIn("id_ed25519", raw)
        self.assertNotIn(".ssh", raw)


class TestGrpcBridgePort(Base):
    """api_port = a dt-grpc-bridge on this machine in front of Draw Things' gRPC server (reached via local_port)."""

    def test_validate_api_port(self):
        self.assertEqual(boxes.validate(ssh_box(api_port=7864)), [])
        self.assertEqual(boxes.validate(ssh_box()), [])                       # default 0 = plain HTTP
        self.assertTrue(boxes.validate(ssh_box(api_port=7861)))               # same as local_port
        for bad in (-1, 70000, "7864", True, 1.5):
            self.assertTrue(boxes.validate(ssh_box(api_port=bad)), repr(bad))

    def test_http_goes_to_api_port_tunnel_stays_on_local_port(self):
        self.assertEqual(boxes.http_port(ssh_box()), 7861)
        self.assertEqual(boxes.http_port(ssh_box(api_port=7864)), 7864)
        self.assertEqual(boxes.ports(ssh_box(api_port=7864)), {7861, 7864})
        tun = boxes.commands(ssh_box(api_port=7864, remote_port=7859))["tunnel"]
        self.assertIn("7861:127.0.0.1:7859", tun)

    def test_broker_sends_pictures_to_the_bridge_port(self):
        bridge = self.fake()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        unused = s.getsockname()[1]
        s.close()
        self.broker([{"name": "g", "where": "local", "local_port": unused, "api_port": bridge.port, "engine": "a1111",
                      "app": "other", "os": "macos", "abilities": ["image"], "pools": {"image": True, "video": False}}])
        code, body, hdr = self.post_full("p")
        self.assertEqual(code, 200, body[:200])
        self.assertEqual(hdr.get("X-RenderPool-Backend"), "g")

    def test_api_port_clash_refused(self):
        fa, fb = self.fake(), self.fake()
        self.broker([{"name": "a", "url": fa.url}])
        code, res = self.ctl("box-add", name="b", where="local", local_port=fb.port, api_port=fa.port, app="other")
        self.assertEqual(code, 400, res)
        self.assertIn("already used by box a", res["error"])
        self.assertEqual(self.ctl("box-add", name="b", where="local", local_port=fb.port, api_port=0, app="other")[0], 200)


class TestTunnelSafety(Base):
    """Review fixes 1, 2, 9: a running job's tunnel is never cut."""

    def ssh_on_fake(self, f, **kw):
        rec = dict(ssh_box(app="other", laptop=False, abilities=["image"], local_port=f.port,
                           pools={"image": True, "video": False}), **kw)
        return rec

    def live_procs(self):
        return [p for p in self.spawn.procs if p.rc is None]

    def test_uncheck_busy_ssh_box_keeps_tunnel_until_job_done(self):
        f = self.fake(1.0)
        self.broker([self.ssh_on_fake(f)])
        self.wait_until(lambda: len(self.live_procs()) == 1, what="tunnel up")
        h = {"x": self.post_bg("x", headers={"X-RenderPool-Label": "x"})}
        self.wait_until(lambda: self.item("x")["state"] == "running", what="running")
        self.assertEqual(self.ctl("pool", box="lap", kind="image", on=False)[0], 200)
        time.sleep(0.5)                                      # the monitor has run several times
        self.assertEqual(len(self.live_procs()), 1, "tunnel cut under a running job")
        self.join(h)
        self.assertEqual(h["x"]["result"][0], 200)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(self.item("x")["attempts"], 1)
        self.wait_until(lambda: not self.live_procs(), what="tunnel stopped once idle and unchecked")

    def test_box_edit_refused_while_busy(self):
        f = self.fake(1.0)
        self.broker([self.ssh_on_fake(f)])
        self.wait_until(lambda: len(self.live_procs()) == 1, what="tunnel up")
        h = {"x": self.post_bg("x", headers={"X-RenderPool-Label": "x"})}
        self.wait_until(lambda: self.item("x")["state"] == "running", what="running")
        code, res = self.ctl("box-edit", **self.ssh_on_fake(f, ssh_host="user@other.example"))
        self.assertEqual(code, 409, res)
        self.assertEqual(len(self.live_procs()), 1)
        self.join(h)
        self.assertEqual(h["x"]["result"][0], 200)
        self.assertEqual(self.item("x")["attempts"], 1)

    def test_edit_restarts_tunnel_with_new_fields(self):
        f = self.fake()
        self.broker([self.ssh_on_fake(f)])
        self.wait_until(lambda: len(self.live_procs()) == 1, what="tunnel up")
        code, res = self.ctl("box-edit", **self.ssh_on_fake(f, ssh_host="user@other.example"))
        self.assertEqual(code, 200, res)
        self.wait_until(lambda: any("user@other.example" in p.argv for p in self.live_procs()),
                        timeout=3, what="tunnel restarted with the new host")
        self.assertEqual(len(self.live_procs()), 1)


class TestEquivalentsAndInvalid(Base):
    """Review fixes 6 and 10."""

    def test_model_map_value_must_be_a_listed_equivalent(self):
        from test_renderpool import free_port
        f = self.fake()
        self.broker([{"name": "a", "url": f.url}])
        ok = dict(name="b", where="local", local_port=free_port(), app="other")
        code, res = self.ctl("box-add", **dict(ok, model_map={"m1.ckpt": "x.gguf"}))       # m1 lists no equivalents
        self.assertEqual(code, 400, res)
        self.assertTrue(any("equivalent" in e for e in res["errors"]))
        code, res = self.ctl("box-add", **dict(ok, model_map={"not_cleared.ckpt": "x.gguf"}))
        self.assertEqual(code, 400, res)
        code, res = self.ctl("box-add", **dict(ok, model_map={"m2.ckpt": "x.gguf"}))       # listed in config
        self.assertEqual(code, 200, res)

    def test_seed_with_unlisted_mapping_exits(self):
        import renderpool
        from test_renderpool import TEST_CLEARED
        with self.assertRaises(SystemExit):
            renderpool.load_config(None, {"cleared_models": TEST_CLEARED, "default_model": "m1.ckpt",
                                   "backends": [{"name": "a", "local_port": 7860, "model_map": {"m1.ckpt": "any.gguf"}}]})

    def stored(self, recs):
        import sqlite3
        db = sqlite3.connect(os.path.join(self.tmp, "state", "renderpool.db"))
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('boxes', ?)", (json.dumps(recs),))
        db.commit()
        db.close()

    def detail(self):
        import urllib.request
        with urllib.request.urlopen(self.base + "/renderpool/api/boxes", timeout=5) as r:
            return json.loads(r.read())

    def test_invalid_stored_box_is_kept_and_shown_never_dropped(self):
        f = self.fake()
        b1 = self.broker([{"name": "a", "url": f.url}])
        good = b1.boxes[0].rec
        b1.stop()
        bad = {"name": "b", "where": "local", "local_port": 1234, "app": "other", "model_map": {"m1.ckpt": "any.gguf"}}
        self.stored([good, bad])
        self.broker([{"name": "a", "url": f.url}])
        d = dict((x["name"], x) for x in self.detail()["boxes"])
        self.assertIn("b", d)
        self.assertTrue(d["b"]["invalid"])
        self.assertEqual(self.state()["invalid_boxes"][0]["name"], "b")
        self.assertNotIn("b", [x["name"] for x in self.state()["boxes"]])     # never routed to
        self.ctl("pool", box="a", kind="image", on=False)                     # any save...
        self.brokers[-1].stop()
        self.broker([{"name": "a", "url": f.url}])
        self.assertIn("b", [x["name"] for x in self.detail()["boxes"]])       # ...keeps it
        code, res = self.ctl("box-add", **dict(bad, model_map={}))
        self.assertEqual(code, 400, res)                                      # the name is still taken
        code, res = self.ctl("box-edit", **dict(bad, model_map={}))          # fixing it makes it a box again
        self.assertEqual(code, 200, res)
        self.assertEqual(self.state()["invalid_boxes"], [])
        self.assertIn("b", [x["name"] for x in self.state()["boxes"]])

    def test_invalid_stored_box_can_be_removed(self):
        f = self.fake()
        b1 = self.broker([{"name": "a", "url": f.url}])
        good = b1.boxes[0].rec
        b1.stop()
        self.stored([good, {"name": "b", "where": "nowhere", "local_port": 1234}])
        self.broker([{"name": "a", "url": f.url}])
        self.assertEqual(self.ctl("box-remove", name="b")[0], 200)
        self.assertEqual([x["name"] for x in self.detail()["boxes"]], ["a"])


if __name__ == "__main__":
    unittest.main()
