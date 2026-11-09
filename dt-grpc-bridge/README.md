# dt-grpc-bridge

A small HTTP server that speaks the A1111-style API RenderPool already uses (`GET /`, `POST /sdapi/v1/txt2img`,
`POST /sdapi/v1/img2img`) and turns each request into a gRPC call to the Draw Things app when the app's
API server is in **gRPC mode** (TLS).

## Why

RenderPool (`../renderpool.py`) is stdlib-only and can run on the system `/usr/bin/python3` (on macOS, the interpreter
you grant disk access to). gRPC needs grpcio, protobuf, flatbuffers, numpy, pillow and fpzip. Rather than
put those into RenderPool, the bridge runs in its own venv and RenderPool sees an ordinary HTTP engine.

gRPC mode also unlocks the **moodboard** (several reference images, sent as `shuffle` hints), which the
app's HTTP API does not expose.

## Run

    ./setup.sh                                   # once: creates .venv, installs requirements.txt,
                                                 # downloads Draw Things' protocol files (see THIRD_PARTY.md)
    .venv/bin/python -I bridge.py                # listens on 127.0.0.1:7864 only
    .venv/bin/python -I test_bridge.py           # unit tests, no network

`setup.sh` needs `curl` and network access to raw.githubusercontent.com. The protocol files are pinned to
one upstream commit and checked by SHA-256; they are GPL-3.0 and are not shipped in this repository.

launchd, one bridge per Draw Things machine: `../examples/launchd/io.github.renderpool.dt-grpc-bridge-local.plist`
(port 7865 -> this machine's gRPC 7859) and `../examples/launchd/io.github.renderpool.dt-grpc-bridge-remote.plist`
(port 7864 -> local 7861 = RenderPool's ssh tunnel to another machine's 7859).

| env | default | |
|---|---|---|
| `DT_GRPC_ADDR` | `127.0.0.1:7859` | Draw Things gRPC server (TLS name is always checked as `localhost`) |
| `DT_BRIDGE_PORT` | `7864` | bridge listen port, always on 127.0.0.1 |
| `DT_BRIDGE_TIMEOUT_S` | `1200` | per request, including waiting for an earlier job |
| `DT_BRIDGE_STALL_S` | `120` | Draw Things silent this long mid-job: cancel, answer 503 + `X-Box-Down` (RenderPool requeues); 0 = off |
| `DT_BRIDGE_PROBE_TIMEOUT_S` | `5` | `GET /` Echo timeout |
| `DT_GRPC_TLS` | `1` | `0` for a server with TLS off |
| `DT_BRIDGE_DEFAULT_SAMPLER` | `DDIMTrailing` | used when the request names no sampler or an unknown one |
| `DT_BRIDGE_LOG` | `../state/dt-grpc-requests.log` | one line per request, rotated at 1 MB x 3; `-` = stderr |

## Pointing RenderPool at it

An RenderPool box record has `api_port` (0 = plain HTTP). To put a box on gRPC: switch Draw Things to gRPC
(port 7859), load that machine's bridge plist, then edit the box: an ssh box gets `remote_port` 7859 (its
tunnel now carries gRPC on `local_port`) and `api_port` = the bridge port; a local box gets
`local_port` 7859 and `api_port` 7865. RenderPool sends HTTP to `api_port`. Back to HTTP = `api_port` 0 and the
old ports. `GET /` returns 200 only when a gRPC Echo to Draw Things succeeds, so RenderPool's up/down check is the
real app's health.

## What it accepts

`prompt`, `negative_prompt`, `model` (required: a `.ckpt` file name on the Draw Things side; checked
against the server's file list), `width`, `height`, `steps`, `seed` (-1 or missing = random; the real seed
is in `info`), `guidance_scale` or `cfg_scale`, `strength` or `denoising_strength` (img2img; txt2img is
always 1.0), `batch_size`, `batch_count` or `n_iter`, `sampler` or `sampler_name` (Draw Things names,
their display names, and the matching A1111 names), `shift` (also turns off resolution-dependent shift so
it takes effect), `init_images` (first = canvas, every further one = moodboard), `moodboard_images`.

Anything else is listed in `info.ignored`, never dropped silently. Defaults the bridge filled in are in
`info.defaults_used`; resizes, roundings and fallbacks are in `info.notes`.

Reply: `{"images": [base64 PNG...], "parameters": <request minus images>, "info": "<json string>"}`.
Errors: `{"error": "..."}` with 400 bad input, 503 Draw Things unreachable, 500 generation failed, 504 timeout.

## Limits

- One generation at a time (Draw Things renders serially); a second request waits inside its timeout.
- Width/height go to Draw Things rounded up to a multiple of 64; the output is resized back to the exact
  size asked for. The init image is resized to the rounded size (aspect not preserved).
- Not supported: masks/inpainting, LoRAs, ControlNets other than the moodboard, hires fix, upscalers,
  face restoration, refiner, a server shared secret. Unwritten configuration fields take Draw Things'
  `config.fbs` defaults, not whatever the app's UI currently has.
- Moodboard images above 2048 px are scaled down; at most 8.
- Never logs prompts or images.
