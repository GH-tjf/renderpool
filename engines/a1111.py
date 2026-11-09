"""A1111-style API engine: Draw Things' HTTP API, and stable-diffusion.cpp's sd-server.

GET / answers when the API is up; POST /sdapi/v1/txt2img|img2img draws. Models:
- app "drawthings": the *.ckpt files in Draw Things' Models folder, listed with the argv from
  boxes.commands() (a local ls, or the same over the box's ssh).
- any other app: GET /sdapi/v1/sd-models. UNVERIFIED that sd-server serves this endpoint.
"""
import json
import os
import socket
import urllib.error
import urllib.request

import boxes
from engines import BoxSaysDown, EngineDown, EngineTimeout


def _is_timeout(e):
    return isinstance(e, socket.timeout) or isinstance(getattr(e, "reason", None), socket.timeout)


class A1111Engine(object):
    def __init__(self, base_url, box, run_cmd=None, dt_models_dir=None):
        self.base_url = base_url.rstrip("/")
        self.box = box
        self.run_cmd = run_cmd
        self.dt_models_dir = dt_models_dir or boxes.DT_MODELS_DIR
        self.last_error = ""

    def is_up(self, timeout):
        try:
            with urllib.request.urlopen(self.base_url + "/", timeout=timeout) as r:
                r.read()
                return r.status == 200
        except urllib.error.HTTPError as e:
            # the server answered: some apps (API-only servers) have no page at "/" and say 404
            return e.code < 500
        except Exception:  # noqa: BLE001 - any other failure means "not answering"
            return False

    def list_models(self):
        try:
            if self.box.get("app") == "drawthings":
                argv = boxes.commands(self.box, self.dt_models_dir)["models"]
                if self.run_cmd is None:
                    raise OSError("no command runner")
                rc, out = self.run_cmd("models", argv, 30)
                if rc != 0:
                    raise OSError("model list command exited %d" % rc)
                models = set(os.path.basename(x.strip()) for x in out.splitlines() if x.strip().endswith(".ckpt"))
            else:
                with urllib.request.urlopen(self.base_url + "/sdapi/v1/sd-models", timeout=10) as r:
                    listing = json.loads(r.read().decode("utf-8"))
                if not isinstance(listing, list):
                    raise ValueError("sd-models did not return a list")
                models = set()
                for m in listing:
                    if isinstance(m, dict):
                        name = m.get("filename") or m.get("model_name") or m.get("title") or ""
                        if name:
                            models.add(os.path.basename(name))
            self.last_error = ""
            return models
        except Exception as e:  # noqa: BLE001 - unknown inventory
            self.last_error = ("%s: %s" % (type(e).__name__, e))[:200]
            return None

    def run(self, endpoint, body, timeout):
        req = urllib.request.Request(self.base_url + "/sdapi/v1/" + endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type") or "application/json", r.read()
        except urllib.error.HTTPError as e:
            if e.code == 503 and e.headers.get("X-Box-Down"):
                # the box (e.g. dt-grpc-bridge: Draw Things unreachable or stalled) says IT is down:
                # requeue the job like a refused connection, rather than failing it to the caller
                raise BoxSaysDown("box says down: %s" % e.read()[:200].decode("utf-8", "replace"))
            return e.code, e.headers.get("Content-Type") or "application/json", e.read()
        except Exception as e:  # noqa: BLE001 - refused / reset / unreachable = down
            if _is_timeout(e):
                raise EngineTimeout("timed out after %ss" % timeout)
            raise EngineDown("%s: %s" % (type(e).__name__, getattr(e, "reason", e)))
