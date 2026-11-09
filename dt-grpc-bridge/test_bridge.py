#!/usr/bin/env python3
"""Unit tests for dt-grpc-bridge's pure parts + Bridge with a fake gRPC stub. No network.
Run: .venv/bin/python -I test_bridge.py"""
import base64
import io
import json
import os
import random
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grpc  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import bridge  # noqa: E402
import imageService_pb2 as pb  # noqa: E402
from flatbuffers import number_types as NT  # noqa: E402
from flatbuffers.table import Table  # noqa: E402


def read_config(buf):
    """Decode the GenerationConfiguration slots the bridge writes, with config.fbs defaults."""
    buf = bytearray(buf)
    t = Table(buf, NT.UOffsetTFlags.py_type(int.from_bytes(buf[0:4], "little")))

    def get(slot, flags, default):
        o = t.Offset(4 + 2 * slot)
        return t.Get(flags, o + t.Pos) if o else default

    o = t.Offset(4 + 2 * bridge.SLOT["model"])
    return dict(start_width=get(1, NT.Uint16Flags, 0), start_height=get(2, NT.Uint16Flags, 0),
                seed=get(3, NT.Uint32Flags, 0), steps=get(4, NT.Uint32Flags, 0),
                guidance_scale=get(5, NT.Float32Flags, 0.0), strength=get(6, NT.Float32Flags, 0.0),
                model=t.String(o + t.Pos).decode() if o else None, sampler=get(8, NT.Int8Flags, 0),
                batch_count=get(9, NT.Uint32Flags, 1), batch_size=get(10, NT.Uint32Flags, 1),
                shift=get(49, NT.Float32Flags, 1.0),
                resolution_dependent_shift=bool(get(71, NT.BoolFlags, True)))


def b64png(color=(255, 0, 0), size=(32, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def body(**kw):
    b = dict(model="m.ckpt", prompt="p", width=512, height=512, steps=4, seed=7, guidance_scale=1.0)
    b.update(kw)
    return b


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details="boom"):
        self._c, self._d = code, details

    def code(self):
        return self._c

    def details(self):
        return self._d


class FakeStub(object):
    def __init__(self, files=("m.ckpt",), n_per_tensor=1, tensors=None, gen_error=None, echo_error=None,
                 secret_missing=False):
        self.files, self.n_per_tensor, self.tensors = list(files), n_per_tensor, tensors
        self.gen_error, self.echo_error, self.secret_missing = gen_error, echo_error, secret_missing
        self.requests = []

    def Echo(self, req, timeout=None):
        if self.echo_error:
            raise self.echo_error
        return pb.EchoReply(message="hi", files=self.files, sharedSecretMissing=self.secret_missing)

    def GenerateImage(self, req, timeout=None):
        self.requests.append(req)
        if self.gen_error:
            raise self.gen_error
        cfg = read_config(req.configuration)
        w, h = cfg["start_width"] * 64, cfg["start_height"] * 64
        n = cfg["batch_size"] * cfg["batch_count"]
        yield pb.ImageGenerationResponse(currentSignpost=pb.ImageGenerationSignpostProto(
            sampling=pb.ImageGenerationSignpostProto.Sampling(step=1)))
        if self.tensors is not None:
            yield pb.ImageGenerationResponse(generatedImages=self.tensors)
            return
        imgs = [bridge.image_to_tensor(Image.new("RGB", (w, h), (0, 255, 0))) for _ in range(n)]
        yield pb.ImageGenerationResponse(generatedImages=imgs)


def mk(stub, **kw):
    return bridge.Bridge(stub, pb, **kw)


class TestTranslate(unittest.TestCase):
    def test_field_mapping_typical_caller_shape(self):
        p = bridge.translate("txt2img", body(negative_prompt="n", batch_count=2, batch_size=3))
        self.assertEqual((p["model"], p["prompt"], p["negative_prompt"]), ("m.ckpt", "p", "n"))
        self.assertEqual((p["width"], p["height"], p["steps"], p["seed"]), (512, 512, 4, 7))
        self.assertEqual((p["guidance_scale"], p["strength"]), (1.0, 1.0))
        self.assertEqual((p["batch_size"], p["batch_count"], p["n_expected"]), (3, 2, 6))
        self.assertEqual(p["ignored"], [])

    def test_a1111_aliases(self):
        p = bridge.translate("img2img", dict(model="m.ckpt", cfg_scale=5.5, denoising_strength=0.4,
                                             n_iter=2, sampler_name="Euler a", init_images=[b64png()]))
        self.assertEqual((p["guidance_scale"], p["strength"], p["batch_count"], p["sampler"]),
                         (5.5, 0.4, 2, "EulerA"))

    def test_guidance_wins_over_cfg_and_is_reported(self):
        p = bridge.translate("txt2img", body(cfg_scale=9))
        self.assertEqual(p["guidance_scale"], 1.0)
        self.assertIn("cfg_scale", p["ignored"])

    def test_txt2img_strength_is_one_and_strength_reported_ignored(self):
        p = bridge.translate("txt2img", body(strength=0.3, init_images=[b64png()]))
        self.assertEqual(p["strength"], 1.0)
        self.assertEqual(p["ignored"], ["init_images", "strength"])
        self.assertIsNone(p["canvas"])

    def test_seed_random(self):
        for s in (-1, None, "-1"):
            b = body(seed=s)
            p = bridge.translate("txt2img", b, rng=random.Random(1))
            self.assertTrue(0 <= p["seed"] < 2 ** 32)
            self.assertIn("seed was random", p["notes"])
        p = bridge.translate("txt2img", {k: v for k, v in body().items() if k != "seed"})
        self.assertIn("seed was random", p["notes"])

    def test_samplers(self):
        for name, want in [("DPM++ 2M Karras", "DPMPP2MKarras"), ("Euler A Trailing", "EulerATrailing"),
                           ("DDIMTrailing", "DDIMTrailing"), ("UniPC Trailing", "UniPCTrailing"),
                           ("DPM++ SDE Karras", "DPMPPSDEKarras"), ("euler_a", "EulerA"), ("TCD", "TCD")]:
            self.assertEqual(bridge.translate("txt2img", body(sampler=name))["sampler"], want, name)
        p = bridge.translate("txt2img", body(sampler="Heun"))
        self.assertEqual(p["sampler"], "DDIMTrailing")
        self.assertTrue(any("unknown sampler" in n for n in p["notes"]))
        p = bridge.translate("txt2img", body(), default_sampler="UniPCTrailing")
        self.assertEqual(p["sampler"], "UniPCTrailing")
        self.assertIn("sampler", p["defaults_used"])

    def test_unknown_fields_go_to_ignored(self):
        p = bridge.translate("img2img", body(init_images=[b64png()], mask="xx", restore_faces=True,
                                             override_settings={}, sampler="DDIM", sampler_name="PLMS"))
        self.assertEqual(p["ignored"], ["mask", "override_settings", "restore_faces", "sampler_name"])
        self.assertNotIn("init_images", p["parameters"])
        self.assertNotIn("mask", p["parameters"])
        self.assertIn("restore_faces", p["parameters"])

    def test_round_64(self):
        self.assertEqual([bridge.round_up_64(x) for x in (1, 64, 65, 384, 640, 700, 1000)],
                         [64, 64, 128, 384, 640, 704, 1024])
        p = bridge.translate("txt2img", body(width=700, height=390))
        self.assertEqual((p["req_width"], p["req_height"], p["width"], p["height"]), (704, 448, 700, 390))
        self.assertTrue(any("rounded up" in n for n in p["notes"]))

    def test_init_split_canvas_and_moodboard(self):
        p = bridge.translate("img2img", body(width=640, height=384, init_images=[
            b64png((255, 0, 0)), b64png((0, 0, 255), (40, 20)), "data:image/png;base64," + b64png((0, 9, 0))],
            moodboard_images=[b64png((9, 9, 9))]))
        self.assertEqual(p["canvas"].size, (640, 384))
        self.assertEqual(p["canvas"].getpixel((0, 0)), (255, 0, 0))
        self.assertEqual([m.getpixel((0, 0)) for m in p["moodboard"]], [(0, 0, 255), (0, 9, 0), (9, 9, 9)])
        self.assertEqual(p["moodboard"][0].size, (40, 20))

    def test_bad_inputs(self):
        cases = [("txt2img", dict(prompt="p")), ("img2img", body()), ("img2img", body(init_images=["!!notimg"])),
                 ("txt2img", body(width="wide")), ("txt2img", body(steps=0)), ("txt2img", body(width=99999)),
                 ("txt2img", body(batch_size=8, batch_count=3)), ("txt2img", body(init_images="x")),
                 ("txt2img", body(guidance_scale=True)), ("img2img", body(init_images=[b64png()], strength=2)),
                 ("txt2img", []), ("upscale", body())]
        for ep, b in cases:
            with self.assertRaises(bridge.BadRequest, msg=repr((ep, b))[:80]):
                bridge.translate(ep, b)

    def test_shift_sets_slot_and_disables_resolution_shift(self):
        p = bridge.translate("txt2img", body(shift=3.0))
        cfg = read_config(bridge.build_config(p))
        self.assertAlmostEqual(cfg["shift"], 3.0)
        self.assertFalse(cfg["resolution_dependent_shift"])
        cfg = read_config(bridge.build_config(bridge.translate("txt2img", body())))
        self.assertAlmostEqual(cfg["shift"], 1.0)  # fbs default, not written
        self.assertTrue(cfg["resolution_dependent_shift"])


class TestConfigAndTensors(unittest.TestCase):
    def test_build_config_roundtrip(self):
        p = bridge.translate("img2img", body(width=640, height=384, steps=8, seed=0, strength=0.6,
                                             sampler="UniPC Trailing", batch_size=2, init_images=[b64png()]))
        cfg = read_config(bridge.build_config(p))
        self.assertEqual((cfg["start_width"], cfg["start_height"], cfg["seed"], cfg["steps"]), (10, 6, 0, 8))
        self.assertAlmostEqual(cfg["strength"], 0.6, places=5)
        self.assertEqual((cfg["model"], cfg["sampler"], cfg["batch_size"], cfg["batch_count"]),
                         ("m.ckpt", bridge.SAMPLER_VALUE["UniPCTrailing"], 2, 1))

    def test_tensor_roundtrip_and_multi_n(self):
        im = Image.new("RGB", (64, 128), (200, 10, 30))
        out = bridge.tensor_to_images(bridge.image_to_tensor(im))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].size, (64, 128))
        self.assertTrue(np.abs(np.asarray(out[0], int) - np.asarray(im, int)).max() <= 1)
        t = bridge.image_to_tensor(im)
        hdr = bytearray(t[:68])
        hdr[20:24] = (2).to_bytes(4, "little")  # n = 2
        two = bytes(hdr) + t[68:] + t[68:]
        self.assertEqual(len(bridge.tensor_to_images(two)), 2)

    def test_bad_tensor(self):
        with self.assertRaises(RuntimeError):
            bridge.tensor_to_images(b"\x05" + b"\x00" * 100)


def serve_bridge(br):
    """The real HTTP handler on a free port, backed by bridge br. -> (server, port)."""
    from http.server import ThreadingHTTPServer
    bridge.Handler.bridge = br
    srv = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class StallingCall(object):
    """A GenerateImage stream that sends one step, then goes silent until canceled (Draw Things wedged)."""
    def __init__(self):
        self.canceled = threading.Event()
        self.sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self.sent:
            self.sent = True
            return pb.ImageGenerationResponse(currentSignpost=pb.ImageGenerationSignpostProto(
                sampling=pb.ImageGenerationSignpostProto.Sampling(step=1)))
        if not self.canceled.wait(30):
            raise AssertionError("never canceled")
        raise FakeRpcError(grpc.StatusCode.CANCELLED, "Canceled")

    def cancel(self):
        self.canceled.set()


class StallStub(FakeStub):
    def GenerateImage(self, req, timeout=None):
        self.requests.append(req)
        self.call = StallingCall()
        return self.call


class TestStall(unittest.TestCase):
    def test_silent_stream_is_cancelled_and_answers_503(self):
        stub = StallStub()
        t0 = time.monotonic()
        st, rep, _ = mk(stub, stall_s=0.4).generate("txt2img", body())
        self.assertEqual(st, 503, rep)
        self.assertIn("went silent", rep["error"])
        self.assertTrue(stub.call.canceled.is_set())
        self.assertLess(time.monotonic() - t0, 5)

    def test_steady_stream_is_not_cancelled(self):
        st, rep, _ = mk(FakeStub(), stall_s=0.4).generate("txt2img", body())
        self.assertEqual(st, 200, rep)

    def test_503_reply_carries_box_down_header(self):
        srv, port = serve_bridge(mk(StallStub(), stall_s=0.3))
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/sdapi/v1/txt2img" % port,
                                         data=json.dumps(body()).encode(), headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 503)
            self.assertEqual(cm.exception.headers.get("X-Box-Down"), "1")
        finally:
            srv.shutdown()
            srv.server_close()


class TestBridge(unittest.TestCase):
    def test_txt2img_resize_back_and_reply_shape(self):
        stub = FakeStub()
        st, rep, lf = mk(stub).generate("txt2img", body(width=700, height=390, batch_size=2))
        self.assertEqual(st, 200, rep)
        self.assertEqual(len(rep["images"]), 2)
        for b in rep["images"]:
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(b))).size, (700, 390))
        info = json.loads(rep["info"])
        self.assertEqual(info["seed"], 7)
        self.assertEqual(info["ignored"], [])
        self.assertEqual(rep["parameters"]["width"], 700)
        self.assertEqual(lf["n"], 2)
        self.assertEqual(lf["size"], "700x390")

    def test_img2img_request_carries_canvas_and_shuffle_hints(self):
        stub = FakeStub()
        st, rep, _ = mk(stub).generate("img2img", body(init_images=[b64png(), b64png((0, 0, 255))],
                                                       moodboard_images=[b64png((1, 2, 3))], strength=1.0))
        self.assertEqual(st, 200, rep)
        req = stub.requests[0]
        self.assertTrue(req.HasField("image"))
        self.assertEqual(len(req.hints), 1)
        self.assertEqual(req.hints[0].hintType, "shuffle")
        self.assertEqual([t.weight for t in req.hints[0].tensors], [1.0, 1.0])
        self.assertEqual(json.loads(rep["info"])["moodboard_images"], 2)
        self.assertNotIn("init_images", rep["parameters"])

    def test_txt2img_has_no_image_or_hints(self):
        stub = FakeStub()
        mk(stub).generate("txt2img", body())
        self.assertFalse(stub.requests[0].HasField("image"))
        self.assertEqual(len(stub.requests[0].hints), 0)

    def test_error_codes(self):
        C = grpc.StatusCode
        cases = [(FakeStub(), dict(prompt="p"), 400),
                 (FakeStub(files=["other.ckpt"]), body(), 400),
                 (FakeStub(echo_error=FakeRpcError(C.UNAVAILABLE)), body(), 503),
                 (FakeStub(secret_missing=True), body(), 503),
                 (FakeStub(gen_error=FakeRpcError(C.UNAVAILABLE)), body(), 503),
                 (FakeStub(gen_error=FakeRpcError(C.DEADLINE_EXCEEDED)), body(), 504),
                 (FakeStub(gen_error=FakeRpcError(C.INTERNAL, "model failed")), body(), 500),
                 (FakeStub(tensors=[]), body(), 500),
                 (FakeStub(tensors=[b"\x05" * 80]), body(), 500)]
        for stub, b, want in cases:
            st, rep, _ = mk(stub).generate("txt2img", b)
            self.assertEqual(st, want, rep)
            self.assertIn("error", rep)

    def test_empty_file_list_skips_model_check(self):
        st, rep, _ = mk(FakeStub(files=[])).generate("txt2img", body())
        self.assertEqual(st, 200, rep)

    def test_count_mismatch_is_reported(self):
        one = bridge.image_to_tensor(Image.new("RGB", (512, 512)))
        st, rep, _ = mk(FakeStub(tensors=[one])).generate("txt2img", body(batch_size=2))
        self.assertEqual(st, 200)
        self.assertTrue(any("expected 2 images" in n for n in json.loads(rep["info"])["notes"]))

    def test_probe(self):
        self.assertEqual(mk(FakeStub()).probe(), (True, ""))
        ok, why = mk(FakeStub(echo_error=FakeRpcError(grpc.StatusCode.UNAVAILABLE, "refused"))).probe()
        self.assertFalse(ok)
        self.assertIn("UNAVAILABLE", why)

    def test_busy_lock_times_out_504(self):
        b = mk(FakeStub(), timeout_s=0.2)
        b.gen_lock.acquire()
        try:
            st, rep, _ = b.generate("txt2img", body())
        finally:
            b.gen_lock.release()
        self.assertEqual(st, 504, rep)


if __name__ == "__main__":
    unittest.main(verbosity=1)
