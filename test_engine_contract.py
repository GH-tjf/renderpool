#!/usr/bin/env python3
"""The contract every RenderPool engine plug-in must pass, run against each engine with a fake server.

To check a new engine: write a make_engine(base_url, box) for it and call run_contract(self, make_engine,
start_fake) from a TestCase. Fake servers only; nothing here contacts a real engine.

Run:  python3 -m unittest -v test_engine_contract
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boxes  # noqa: E402
from engines import ENGINES, EngineDown, EngineTimeout  # noqa: E402
from test_renderpool import PNG_B64, FakeDT, free_port  # noqa: E402

BODY = json.dumps({"model": "m1.ckpt", "prompt": "contract", "width": 64, "height": 64, "seed": 1}).encode()


def run_contract(test, make_engine, start_fake, models=("m1.ckpt", "m2.ckpt")):
    """make_engine(base_url, box) -> engine.  start_fake(delay, models) -> (base_url, fake) for a running server."""
    url, fake = start_fake(0.0, list(models))
    eng = make_engine(url, {"name": "c"})
    # up / down
    test.assertIs(eng.is_up(2.0), True)
    dead = make_engine("http://127.0.0.1:%d" % free_port(), {"name": "d"})
    test.assertIs(dead.is_up(1.0), False)
    # the model list
    test.assertEqual(eng.list_models(), set(models))
    test.assertIsNone(dead.list_models())      # unknown -> None, never "any model"
    # a 200 run
    status, ctype, body = eng.run("txt2img", BODY, 10.0)
    test.assertEqual(status, 200)
    test.assertIn("json", ctype)
    test.assertEqual(json.loads(body)["images"], [PNG_B64])
    # a 422 is passed through verbatim
    raw = b'{"detail":"init_images height doesn\'t match"}'
    fake.fail = (422, raw)
    test.assertEqual(eng.run("img2img", BODY, 10.0)[0::2], (422, raw))
    fake.fail = None
    # EngineDown when the port is closed
    with test.assertRaises(EngineDown):
        dead.run("txt2img", BODY, 5.0)
    # EngineTimeout when the run outlives its timeout
    url2, _slow = start_fake(1.0, list(models))
    with test.assertRaises(EngineTimeout):
        make_engine(url2, {"name": "s"}).run("txt2img", BODY, 0.3)


class TestA1111Contract(unittest.TestCase):
    def setUp(self):
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.stop()

    def start_fake(self, delay, models):
        f = FakeDT(delay).start()
        f.models = models
        self.fakes.append(f)
        return f.url, f

    def test_registry(self):
        self.assertIn("a1111", ENGINES)

    def test_a1111_other_app_lists_models_over_http(self):
        def make(url, box):
            rec = boxes.normalize(dict(box, local_port=int(url.rsplit(":", 1)[1]), app="other"))
            return ENGINES["a1111"](url, rec)
        run_contract(self, make, self.start_fake)

    def test_a1111_drawthings_lists_models_with_the_built_command(self):
        calls = []

        def run_cmd(which, argv, timeout):
            calls.append((which, argv))
            return 0, "m1.ckpt\nm2.ckpt\nreadme.txt\n"

        def make(url, box):
            rec = boxes.normalize(dict(box, local_port=int(url.rsplit(":", 1)[1]), app="drawthings"))
            eng = ENGINES["a1111"](url, rec, run_cmd=run_cmd)
            if box["name"] == "d":       # a dead box: its model command fails too
                eng.run_cmd = lambda which, argv, timeout: (255, "")
            return eng
        run_contract(self, make, self.start_fake)
        self.assertEqual(calls[0][0], "models")
        self.assertEqual(calls[0][1][0], "ls")


if __name__ == "__main__":
    unittest.main()
