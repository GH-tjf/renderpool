#!/usr/bin/env python3
"""dt-grpc-bridge: an A1111-style HTTP front (what RenderPool's engines/a1111.py speaks) for a Draw Things
app whose API server is in gRPC mode (TLS). Listens on 127.0.0.1 only.

  GET  /                      200 "dt-grpc-bridge ok" iff a gRPC Echo to Draw Things succeeds, else 503
  POST /sdapi/v1/txt2img      JSON in, {"images": [b64 PNG...], "parameters": {...}, "info": "<json>"} out
  POST /sdapi/v1/img2img      same; init_images[0] = canvas, init_images[1:] + moodboard_images = moodboard
                              (sent as "shuffle" hints, weight 1.0, like the app's Moodboard)

Errors are JSON {"error": "..."}: 400 bad input, 503 gRPC unreachable, 500 generation failed, 504 timeout.
Never logs prompts or images: one line per request (time, endpoint, model, size, steps, n, secs, result).

Env: DT_GRPC_ADDR (127.0.0.1:7859)  DT_BRIDGE_PORT (7864)  DT_BRIDGE_TIMEOUT_S (1200)
     DT_BRIDGE_PROBE_TIMEOUT_S (5)  DT_GRPC_TLS (1)  DT_BRIDGE_DEFAULT_SAMPLER (DDIMTrailing)
     DT_BRIDGE_LOG (../state/dt-grpc-requests.log)  DT_BRIDGE_LOG_MAX_BYTES (1000000, x3 rotated files)

Protocol: drawthingsai/draw-things-community imageService.proto + config.fbs (GPL-3.0; setup.sh
fetches them beside this file, see THIRD_PARTY.md).
Tensor wire format = s4nnc Tensor.data(): 68-byte header (17 x uint32) + float16 NHWC in [-1, 1];
header[0] == 0 uncompressed, == 1012247 fpzip-compressed (the app's "response compression").
"""
import base64
import binascii
import io
import json
import logging
import logging.handlers
import os
import random
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import flatbuffers  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# --------------------------------------------------------------------------------------------
# Protocol constants
# --------------------------------------------------------------------------------------------
CCV_TENSOR_CPU_MEMORY = 0x1
CCV_TENSOR_FORMAT_NHWC = 0x02
CCV_16F = 0x20000
CCV_32F = 0x04000
FPZIP_ID = 1012247

# config.fbs table GenerationConfiguration: slot = field position (deprecated fields keep their slot).
SLOT = dict(id=0, start_width=1, start_height=2, seed=3, steps=4, guidance_scale=5, strength=6,
            model=7, sampler=8, batch_count=9, batch_size=10, shift=49,
            resolution_dependent_shift=71)
NUM_SLOTS = 92  # GenerationConfiguration has 92 fields (id .. sol_attention_tau)

# config.fbs enum SamplerType: byte (order = value)
SAMPLER_ENUM = ["DPMPP2MKarras", "EulerA", "DDIM", "PLMS", "DPMPPSDEKarras", "UniPC", "LCM",
                "EulerASubstep", "DPMPPSDESubstep", "TCD", "EulerATrailing", "DPMPPSDETrailing",
                "DPMPP2MAYS", "EulerAAYS", "DPMPPSDEAYS", "DPMPP2MTrailing", "DDIMTrailing",
                "UniPCTrailing", "UniPCAYS", "TCDTrailing"]
SAMPLER_VALUE = {n: i for i, n in enumerate(SAMPLER_ENUM)}

# --------------------------------------------------------------------------------------------
# Request translation (pure: no network)
# --------------------------------------------------------------------------------------------
IMAGE_KEYS = ("init_images", "moodboard_images", "mask")
HANDLED = {"prompt", "negative_prompt", "model", "width", "height", "steps", "seed",
           "guidance_scale", "cfg_scale", "strength", "denoising_strength", "batch_size",
           "batch_count", "n_iter", "sampler", "sampler_name", "shift", "init_images",
           "moodboard_images"}
DEFAULTS = dict(width=512, height=512, steps=20, guidance_scale=7.0, img2img_strength=0.75)
MAX_SIDE = 4096
MAX_IMAGES = 16
MOOD_MAX_SIDE = 2048


class BadRequest(Exception):
    pass


def norm_sampler_key(name):
    s = str(name).lower().replace("++", "pp")
    return re.sub(r"[^a-z0-9]", "", s)


# DT enum names, DT HTTP API display names ("Euler A Trailing", "DPM++ 2M Karras") and the A1111 names
# that are the same sampler all normalize to the enum name lowercased.
SAMPLER_ALIASES = {norm_sampler_key(n): n for n in SAMPLER_ENUM}
SAMPLER_ALIASES.update({"dpmpp2mkarras": "DPMPP2MKarras", "dpmppsdekarras": "DPMPPSDEKarras",
                        "eulerancestral": "EulerA", "unipcbh2": "UniPC"})


def map_sampler(name):
    """-> enum name or None (unknown)."""
    if name is None or name == "":
        return None
    return SAMPLER_ALIASES.get(norm_sampler_key(name))


def round_up_64(v):
    return max(64, ((int(v) + 63) // 64) * 64)


def _int(body, key, default, lo, hi):
    v = body.get(key, default)
    if v is None:
        v = default
    try:
        if isinstance(v, bool):
            raise ValueError
        f = float(v)
        if f != int(f):
            raise ValueError
        v = int(f)
    except (TypeError, ValueError):
        raise BadRequest("%s must be an integer" % key)
    if not lo <= v <= hi:
        raise BadRequest("%s must be between %d and %d" % (key, lo, hi))
    return v


def _float(body, key, lo, hi):
    v = body.get(key)
    try:
        if isinstance(v, bool):
            raise ValueError
        v = float(v)
    except (TypeError, ValueError):
        raise BadRequest("%s must be a number" % key)
    if not lo <= v <= hi:
        raise BadRequest("%s must be between %g and %g" % (key, lo, hi))
    return v


def decode_image(b64, what):
    if not isinstance(b64, str) or not b64:
        raise BadRequest("%s must be a non-empty base64 string" % what)
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    try:
        raw = base64.b64decode(b64, validate=False)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (binascii.Error, ValueError, OSError) as e:
        raise BadRequest("%s is not a decodable image (%s)" % (what, type(e).__name__))
    return img.convert("RGB")


def translate(endpoint, body, default_sampler="DDIMTrailing", rng=None):
    """Validate an A1111-style body and turn it into a generation plan (dict). Raises BadRequest."""
    if endpoint not in ("txt2img", "img2img"):
        raise BadRequest("unknown endpoint %s" % endpoint)
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    rng = rng or random.SystemRandom()
    notes, defaults_used = [], []
    ignored = sorted(k for k in body if k not in HANDLED)

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise BadRequest("model (a Draw Things .ckpt file name) is required")
    model = model.strip()
    prompt = body.get("prompt") or ""
    negative = body.get("negative_prompt") or ""
    if not isinstance(prompt, str) or not isinstance(negative, str):
        raise BadRequest("prompt and negative_prompt must be strings")

    for k in ("width", "height", "steps"):
        if body.get(k) is None:
            defaults_used.append(k)
    width = _int(body, "width", DEFAULTS["width"], 64, MAX_SIDE)
    height = _int(body, "height", DEFAULTS["height"], 64, MAX_SIDE)
    steps = _int(body, "steps", DEFAULTS["steps"], 1, 150)

    seed = body.get("seed")
    if seed is None or seed == -1 or seed == "-1":
        seed = rng.randrange(0, 2 ** 32)
        notes.append("seed was random")
    else:
        seed = _int(body, "seed", 0, -1, 2 ** 32 - 1)

    if body.get("guidance_scale") is not None:
        cfg = _float(body, "guidance_scale", 0.0, 50.0)
        if body.get("cfg_scale") is not None:
            ignored.append("cfg_scale")
            notes.append("guidance_scale and cfg_scale both given; used guidance_scale")
    elif body.get("cfg_scale") is not None:
        cfg = _float(body, "cfg_scale", 0.0, 50.0)
    else:
        cfg = DEFAULTS["guidance_scale"]
        defaults_used.append("guidance_scale")

    has_strength = [k for k in ("strength", "denoising_strength") if body.get(k) is not None]
    if endpoint == "txt2img":
        strength = 1.0
        ignored.extend(has_strength)
    elif has_strength:
        strength = _float(body, has_strength[0], 0.0, 1.0)
        if len(has_strength) == 2:
            ignored.append("denoising_strength")
            notes.append("strength and denoising_strength both given; used strength")
    else:
        strength = DEFAULTS["img2img_strength"]
        defaults_used.append("strength")

    batch_size = _int(body, "batch_size", 1, 1, MAX_IMAGES)
    if body.get("batch_count") is not None:
        batch_count = _int(body, "batch_count", 1, 1, MAX_IMAGES)
        if body.get("n_iter") is not None:
            ignored.append("n_iter")
    else:
        batch_count = _int(body, "n_iter", 1, 1, MAX_IMAGES)
    if batch_size * batch_count > MAX_IMAGES:
        raise BadRequest("batch_size * batch_count must be <= %d" % MAX_IMAGES)

    sampler_in = body.get("sampler") if body.get("sampler") not in (None, "") else body.get("sampler_name")
    if body.get("sampler") not in (None, "") and body.get("sampler_name") not in (None, ""):
        ignored.append("sampler_name")
    sampler = map_sampler(sampler_in)
    if sampler is None:
        sampler = default_sampler
        if sampler_in in (None, ""):
            defaults_used.append("sampler")
        else:
            notes.append("unknown sampler %r; used bridge default %s" % (str(sampler_in)[:40], sampler))

    shift = None
    if body.get("shift") is not None:
        shift = _float(body, "shift", 0.0, 100.0)
        notes.append("explicit shift: resolution_dependent_shift turned off so it takes effect")

    # images
    canvas, mood = None, []
    init = body.get("init_images")
    if init is not None and not isinstance(init, list):
        raise BadRequest("init_images must be a list of base64 images")
    extra_mood = body.get("moodboard_images")
    if extra_mood is not None and not isinstance(extra_mood, list):
        raise BadRequest("moodboard_images must be a list of base64 images")
    if endpoint == "img2img":
        if not init:
            raise BadRequest("img2img needs init_images (at least one)")
        canvas = decode_image(init[0], "init_images[0]")
        mood = [decode_image(x, "init_images[%d]" % i) for i, x in enumerate(init[1:], 1)]
    elif init:
        ignored.append("init_images")
    mood += [decode_image(x, "moodboard_images[%d]" % i) for i, x in enumerate(extra_mood or [])]
    if len(mood) > 8:
        raise BadRequest("at most 8 moodboard images")

    rw, rh = round_up_64(width), round_up_64(height)
    if (rw, rh) != (width, height):
        notes.append("size rounded up to %dx%d for Draw Things, output resized back to %dx%d"
                     % (rw, rh, width, height))
    if canvas is not None and canvas.size != (rw, rh):
        notes.append("init image resized from %dx%d to %dx%d" % (canvas.size + (rw, rh)))
        canvas = canvas.resize((rw, rh), Image.BICUBIC)
    for i, m in enumerate(mood):
        if max(m.size) > MOOD_MAX_SIDE:
            s = MOOD_MAX_SIDE / float(max(m.size))
            mood[i] = m.resize((max(1, int(m.size[0] * s)), max(1, int(m.size[1] * s))), Image.BICUBIC)
            notes.append("moodboard image %d downscaled to fit %dpx" % (i + 1, MOOD_MAX_SIDE))

    parameters = {k: v for k, v in body.items() if k not in IMAGE_KEYS}
    return dict(endpoint=endpoint, model=model, prompt=prompt, negative_prompt=negative,
                width=width, height=height, req_width=rw, req_height=rh, steps=steps, seed=seed,
                guidance_scale=cfg, strength=strength, batch_size=batch_size,
                batch_count=batch_count, n_expected=batch_size * batch_count, sampler=sampler,
                shift=shift, canvas=canvas, moodboard=mood, ignored=sorted(set(ignored)),
                notes=notes, defaults_used=defaults_used, parameters=parameters)


def build_config(plan):
    b = flatbuffers.Builder(256)
    model_off = b.CreateString(plan["model"])
    b.StartObject(NUM_SLOTS)
    b.PrependInt64Slot(SLOT["id"], 0, 1)  # force-write id 0
    b.PrependUint16Slot(SLOT["start_width"], plan["req_width"] // 64, 0)
    b.PrependUint16Slot(SLOT["start_height"], plan["req_height"] // 64, 0)
    b.PrependUint32Slot(SLOT["seed"], plan["seed"], 0xFFFFFFFF)  # force-write
    b.PrependUint32Slot(SLOT["steps"], plan["steps"], 0)
    b.PrependFloat32Slot(SLOT["guidance_scale"], plan["guidance_scale"], -1.0)
    b.PrependFloat32Slot(SLOT["strength"], plan["strength"], -1.0)
    b.PrependUOffsetTRelativeSlot(SLOT["model"], model_off, 0)
    b.PrependInt8Slot(SLOT["sampler"], SAMPLER_VALUE[plan["sampler"]], -1)
    b.PrependUint32Slot(SLOT["batch_count"], plan["batch_count"], 0)
    b.PrependUint32Slot(SLOT["batch_size"], plan["batch_size"], 0)
    if plan.get("shift") is not None:
        b.PrependFloat32Slot(SLOT["shift"], plan["shift"], -1.0)
        b.PrependBoolSlot(SLOT["resolution_dependent_shift"], False, True)
    b.Finish(b.EndObject())
    return bytes(b.Output())


def image_to_tensor(img):
    img = img.convert("RGB")
    w, h = img.size
    arr = np.asarray(img, dtype=np.float32) / 255.0 * 2.0 - 1.0
    header = struct.pack("<9I", 0, CCV_TENSOR_CPU_MEMORY, CCV_TENSOR_FORMAT_NHWC, CCV_16F,
                         0, 1, h, w, 3) + b"\x00" * (68 - 36)
    return header + arr.astype("<f2").tobytes()


def tensor_to_images(data):
    """-> list of PIL RGB images (a tensor may carry N > 1)."""
    if len(data) < 68:
        raise RuntimeError("tensor reply shorter than its header")
    hdr = struct.unpack_from("<17I", data, 0)
    n, h, w, c = hdr[5], hdr[6], hdr[7], hdr[8]
    n = max(1, n)
    count = n * h * w * c
    if hdr[0] == FPZIP_ID:
        import fpzip
        arr = np.asarray(fpzip.decompress(data[68:], order="C"), dtype=np.float32).ravel()[:count]
    elif hdr[0] != 0:
        raise RuntimeError("unknown tensor codec header[0]=%d" % hdr[0])
    elif hdr[3] == CCV_16F:
        arr = np.frombuffer(data, dtype="<f2", offset=68, count=count)
    elif hdr[3] == CCV_32F:
        arr = np.frombuffer(data, dtype="<f4", offset=68, count=count)
    else:
        raise RuntimeError("unexpected tensor datatype 0x%x" % hdr[3])
    arr = arr.astype(np.float32).reshape(n, h, w, c)
    arr = np.clip((arr + 1.0) * 127.5, 0, 255).astype(np.uint8)
    return [Image.fromarray(np.ascontiguousarray(a[:, :, :3])) for a in arr]


def png_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------------------------
# Bridge (network side; the stub is injectable for tests)
# --------------------------------------------------------------------------------------------
class GrpcFail(Exception):
    def __init__(self, status, msg):
        Exception.__init__(self, msg)
        self.status = status


def grpc_status(e):
    """Map a grpc.RpcError to an HTTP status."""
    import grpc
    code = e.code() if hasattr(e, "code") else None
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return 504
    if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAUTHENTICATED,
                grpc.StatusCode.PERMISSION_DENIED):
        return 503
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return 400
    return 500


def grpc_msg(e):
    code = e.code().name if hasattr(e, "code") and e.code() is not None else "?"
    det = e.details() if hasattr(e, "details") else str(e)
    return ("gRPC %s: %s" % (code, det or ""))[:300]


def make_stub(addr, tls=True):
    import grpc
    import imageService_pb2_grpc as pbg
    opts = [("grpc.max_receive_message_length", 512 << 20),
            ("grpc.max_send_message_length", 512 << 20),
            # One channel lives as long as the bridge. gRPC's default reconnect backoff grows to 120 s, so after
            # Draw Things (or the ssh tunnel) was down or in HTTP mode the bridge stayed "not answering" for
            # minutes after it came back. Cap it so RenderPool's next probe sees the app within seconds.
            ("grpc.initial_reconnect_backoff_ms", 1000),
            ("grpc.min_reconnect_backoff_ms", 1000),
            ("grpc.max_reconnect_backoff_ms", 5000)]
    if tls:
        # The app's server cert (CN=localhost) is signed by "Draw Things Root CA", public in
        # draw-things-community Libraries/BinaryResources/Resources/root_ca.crt.
        with open(os.path.join(HERE, "dt_root_ca.crt"), "rb") as f:
            creds = grpc.ssl_channel_credentials(root_certificates=f.read())
        ch = grpc.secure_channel(addr, creds, options=opts + [("grpc.ssl_target_name_override", "localhost")])
    else:
        ch = grpc.insecure_channel(addr, options=opts)
    return pbg.ImageGenerationServiceStub(ch)


class Bridge(object):
    def __init__(self, stub, pb, timeout_s=1200.0, probe_timeout_s=5.0, default_sampler="DDIMTrailing",
                 clock=time.monotonic, stall_s=120.0):
        self.stub, self.pb = stub, pb
        self.stall_s = float(stall_s)
        self.timeout_s, self.probe_timeout_s = float(timeout_s), float(probe_timeout_s)
        self.default_sampler = default_sampler
        self.clock = clock
        self.gen_lock = threading.Lock()  # Draw Things renders one job at a time
        self._files, self._files_at = None, 0.0

    def echo(self, timeout):
        r = self.stub.Echo(self.pb.EchoRequest(name="dt-grpc-bridge"), timeout=timeout)
        if r.sharedSecretMissing:
            raise GrpcFail(503, "Draw Things server requires a shared secret (not supported by the bridge)")
        self._files, self._files_at = set(r.files), self.clock()
        return r

    def probe(self):
        """-> (ok, reason)"""
        try:
            self.echo(self.probe_timeout_s)
            return True, ""
        except GrpcFail as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001 - any failure = not answering
            return False, grpc_msg(e) if hasattr(e, "code") else type(e).__name__

    def check_model(self, model):
        """400 when the server lists files and the model is not among them."""
        try:
            if self._files is None or self.clock() - self._files_at > 60:
                self.echo(self.probe_timeout_s)
        except GrpcFail:
            raise
        except Exception as e:  # noqa: BLE001
            raise GrpcFail(grpc_status(e) if hasattr(e, "code") else 503,
                           "Draw Things unreachable: " + (grpc_msg(e) if hasattr(e, "code") else type(e).__name__))
        if self._files and model not in self._files:
            raise GrpcFail(400, "model %s is not on the Draw Things server" % model)

    def generate(self, endpoint, body):
        """-> (http_status, reply_dict, log_fields)"""
        t0 = self.clock()
        lf = dict(endpoint=endpoint, model="-", size="-", steps="-", n=0)
        try:
            plan = translate(endpoint, body, self.default_sampler)
        except BadRequest as e:
            return 400, {"error": str(e)}, lf
        lf.update(model=plan["model"], size="%dx%d" % (plan["width"], plan["height"]), steps=plan["steps"])
        try:
            self.check_model(plan["model"])
            remaining = self.timeout_s - (self.clock() - t0)
            if not self.gen_lock.acquire(timeout=max(0.0, remaining)):
                raise GrpcFail(504, "timed out after %gs waiting for an earlier job" % self.timeout_s)
            try:
                tensors = self._run(plan, self.timeout_s - (self.clock() - t0))
            finally:
                self.gen_lock.release()
            images = []
            for t in tensors:
                images.extend(tensor_to_images(t))
            if not images:
                raise GrpcFail(500, "Draw Things returned no image")
        except GrpcFail as e:
            return e.status, {"error": str(e)}, lf
        except Exception as e:  # noqa: BLE001
            if hasattr(e, "code"):
                st = grpc_status(e)
                msg = grpc_msg(e)
                if st == 504:
                    msg = "timed out after %gs (%s)" % (self.timeout_s, msg)
                return st, {"error": msg}, lf
            return 500, {"error": "generation failed: %s: %s" % (type(e).__name__, str(e)[:200])}, lf

        out_size = (plan["width"], plan["height"])
        got_size = images[0].size
        images = [im if im.size == out_size else im.resize(out_size, Image.LANCZOS) for im in images]
        notes = list(plan["notes"])
        if got_size != (plan["req_width"], plan["req_height"]):
            notes.append("Draw Things returned %dx%d" % got_size)
        if len(images) != plan["n_expected"]:
            notes.append("expected %d images, Draw Things returned %d" % (plan["n_expected"], len(images)))
        lf["n"] = len(images)
        info = dict(model=plan["model"], seed=plan["seed"], width=plan["width"], height=plan["height"],
                    steps=plan["steps"], guidance_scale=plan["guidance_scale"],
                    strength=plan["strength"], sampler=plan["sampler"], batch_size=plan["batch_size"],
                    batch_count=plan["batch_count"], shift=plan["shift"],
                    moodboard_images=len(plan["moodboard"]), ignored=plan["ignored"],
                    defaults_used=plan["defaults_used"], notes=notes, engine="draw-things-grpc via dt-grpc-bridge")
        return 200, {"images": [png_b64(im) for im in images], "parameters": plan["parameters"],
                     "info": json.dumps(info)}, lf

    def _run(self, plan, timeout):
        pb = self.pb
        if timeout <= 0:
            raise GrpcFail(504, "timed out after %gs" % self.timeout_s)
        req = pb.ImageGenerationRequest(scaleFactor=1, prompt=plan["prompt"],
                                        negativePrompt=plan["negative_prompt"],
                                        configuration=build_config(plan), user="dt-grpc-bridge",
                                        device=pb.LAPTOP, chunked=False)
        if plan["canvas"] is not None:
            req.image = image_to_tensor(plan["canvas"])
        if plan["moodboard"]:
            req.hints.append(pb.HintProto(hintType="shuffle", tensors=[
                pb.TensorAndWeight(tensor=image_to_tensor(m), weight=1.0) for m in plan["moodboard"]]))
        # Draw Things streams a message per step. If it goes silent (the app can sit idle
        # with the request open), cancel after stall_s and answer 503 + X-Box-Down so the pool moves the job elsewhere
        # instead of waiting out the whole deadline.
        call = self.stub.GenerateImage(req, timeout=timeout)
        last = [time.monotonic()]
        stalled, done = threading.Event(), threading.Event()

        def watch():
            while not done.wait(min(1.0, self.stall_s / 4.0)):
                if time.monotonic() - last[0] > self.stall_s:
                    stalled.set()
                    cancel = getattr(call, "cancel", None)
                    if cancel:
                        cancel()
                    return

        if self.stall_s > 0:
            threading.Thread(target=watch, daemon=True).start()
        got = []
        try:
            for resp in call:
                last[0] = time.monotonic()
                if resp.generatedImages:
                    got.extend(resp.generatedImages)
        except Exception:
            if stalled.is_set():
                raise GrpcFail(503, "Draw Things went silent for %gs mid-job; request dropped" % self.stall_s)
            raise
        finally:
            done.set()
        if stalled.is_set():
            raise GrpcFail(503, "Draw Things went silent for %gs mid-job; request dropped" % self.stall_s)
        return got


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------
LOG = logging.getLogger("dt-grpc-bridge")
MAX_BODY = 256 << 20


class Handler(BaseHTTPRequestHandler):
    bridge = None  # set by serve()
    server_version = "dt-grpc-bridge/1"

    def log_message(self, fmt, *args):  # no default access log (request lines go nowhere)
        pass

    def _send(self, status, payload, ctype="application/json", extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] != "/":
            return self._send(404, {"error": "not found"})
        ok, why = self.bridge.probe()
        if ok:
            return self._send(200, b"dt-grpc-bridge ok", "text/plain; charset=utf-8")
        return self._send(503, ("dt-grpc-bridge: Draw Things gRPC not answering: %s" % why).encode("utf-8"),
                          "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        ep = {"/sdapi/v1/txt2img": "txt2img", "/sdapi/v1/img2img": "img2img"}.get(path)
        if ep is None:
            return self._send(404, {"error": "not found"})
        t0 = time.monotonic()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_BODY:
            status, reply, lf = 400, {"error": "missing or oversized body"}, dict(endpoint=ep, model="-", size="-", steps="-", n=0)
        else:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = None
            if body is None:
                status, reply, lf = 400, {"error": "body is not valid JSON"}, dict(endpoint=ep, model="-", size="-", steps="-", n=0)
            else:
                status, reply, lf = self.bridge.generate(ep, body)
        secs = time.monotonic() - t0
        result = "ok" if status == 200 else "error %d %s" % (status, reply.get("error", "")[:160])
        LOG.info("%s model=%s size=%s steps=%s n=%s secs=%.1f %s", lf["endpoint"], lf["model"], lf["size"],
                 lf["steps"], lf["n"], secs, result)
        try:
            # 503 = Draw Things unreachable or stalled: the box is unusable right now, not the job bad
            self._send(status, reply, extra={"X-Box-Down": "1"} if status == 503 else None)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("%s client went away before the reply", ep)


def setup_logging(path, max_bytes):
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=3)
    else:
        h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    LOG.addHandler(h)


def cap_file(path, max_bytes):
    """Keep launchd's stdout/stderr file (tracebacks only) from growing without bound."""
    try:
        if path and os.path.getsize(path) > max_bytes:
            with open(path, "r+b") as f:
                f.truncate(0)
    except OSError:
        pass


def serve():
    import imageService_pb2 as pb
    addr = os.environ.get("DT_GRPC_ADDR", "127.0.0.1:7859")
    port = int(os.environ.get("DT_BRIDGE_PORT", "7864"))
    tls = os.environ.get("DT_GRPC_TLS", "1") not in ("0", "false", "no")
    default_sampler = os.environ.get("DT_BRIDGE_DEFAULT_SAMPLER", "DDIMTrailing")
    if default_sampler not in SAMPLER_VALUE:
        sys.exit("DT_BRIDGE_DEFAULT_SAMPLER must be one of " + ", ".join(SAMPLER_ENUM))
    log_path = os.environ.get("DT_BRIDGE_LOG", os.path.join(HERE, "..", "state", "dt-grpc-requests.log"))
    max_bytes = int(os.environ.get("DT_BRIDGE_LOG_MAX_BYTES", "1000000"))
    cap_file(os.environ.get("DT_BRIDGE_STDIO_LOG"), max_bytes)
    setup_logging(os.path.abspath(log_path) if log_path != "-" else None, max_bytes)
    Handler.bridge = Bridge(make_stub(addr, tls), pb,
                            timeout_s=float(os.environ.get("DT_BRIDGE_TIMEOUT_S", "1200")),
                            probe_timeout_s=float(os.environ.get("DT_BRIDGE_PROBE_TIMEOUT_S", "5")),
                            default_sampler=default_sampler,
                            stall_s=float(os.environ.get("DT_BRIDGE_STALL_S", "120")))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    LOG.info("start listen=127.0.0.1:%d grpc=%s tls=%s", port, addr, tls)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LOG.info("stop")


if __name__ == "__main__":
    serve()
