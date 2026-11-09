# RenderPool guide

The full reference: routing, power and network behavior, boxes, models, headers, configuration and the
command line. New here? Start with the [README](../README.md).

## Contents

- [Running it as a service](#running-it-as-a-service)
- [Draw Things in gRPC mode: `dt-grpc-bridge/`](#draw-things-in-grpc-mode-dt-grpc-bridge)
- [How a job is routed](#how-a-job-is-routed)
- [Power and network](#power-and-network)
- [Adding a box](#adding-a-box)
- [Video](#video)
- [History](#history)
- [Cleared models](#cleared-models)
- [Test models](#test-models)
- [Engine plug-ins](#engine-plug-ins)
- [Optional request headers](#optional-request-headers)
- [Behavior worth knowing](#behavior-worth-knowing)
- [Access](#access)
  - [Password](#password)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Tests](#tests)
- [Known limits](#known-limits)
- [License](#license)

## Running it as a service

Examples, with `__RENDERPOOL_DIR__` standing for the folder that holds `renderpool.py`:

- macOS: `examples/launchd/io.github.renderpool.plist` (plus one `dt-grpc-bridge` plist per Draw Things
  machine in gRPC mode, see below).
- Linux: `examples/systemd/renderpool.service` (a systemd user unit).
- Windows: `examples/windows/README.md` (not supported as the RenderPool host yet; Windows boxes are).

## Draw Things in gRPC mode: `dt-grpc-bridge/`

If a Draw Things machine runs its API server in gRPC mode, `dt-grpc-bridge/` puts an A1111-style HTTP
front on it so RenderPool can keep speaking HTTP. It has its own venv and dependencies (grpcio, protobuf, ...),
and its `setup.sh` downloads Draw Things' protocol files (GPL-3.0, see `dt-grpc-bridge/THIRD_PARTY.md`)
from the upstream repository at a pinned commit. RenderPool itself does not need it.

## How a job is routed

1. The job's model is its `model` field, or `default_model` when it names none. A model that is not on
   `cleared_models` is refused at once with `422 {"error": "RenderPool: model <file> is not on the cleared list"}`.
2. The model's kind (`image` or `video`) picks the pool.
3. If no box able to do that kind holds the model at all (checked or not), the job is refused with 422.
   If only unchecked boxes hold it, it waits with the note `waiting: only unchecked boxes hold this model`.
4. Jobs wait in arrival order. Each goes to an idle box that is checked in its pool, up, not "needs
   you", on AC if it is a laptop, and holds the model. When two are free, the one expected to finish
   sooner wins (the average of its last 10 runs for that model and size, else its `speed_rank`).
5. A box runs one job at a time, whatever the pool: a box in both pools drawing a video gets no pictures.

**Exact files only.** A box is used for a model only when it holds that exact file, or when its
`model_map` names its own file for it. Two engines (or two quantizations) are not assumed to draw the
same picture from the same seed.

## Power and network

- A laptop's power source is re-read every `power_interval_s`. On battery, or when the check cannot be
  read, it counts as out of the pool: its current job finishes, nothing new is sent. It rejoins when
  back on AC.
- A box that drops off the network mid-job (connection reset, tunnel gone) loses that job: it goes back
  to its place in the queue and runs on another box, inside the caller's same open request. The caller
  gets exactly one answer. After `max_attempts` dispatches the job fails with 502.
- Draw Things closed: RenderPool reopens it once per outage. Draw Things open but its API not answering for
  `relaunch_wait_s`: the box shows `needs you: turn on API Server in Draw Things`, and `notify_cmd`
  runs once. The flag clears when the API answers again.

## Adding a box

On the page: **Boxes** tab, fill the form, **Add box**. Or seed boxes in `config.json` under
`backends`; that list is read only on the first start (empty state database). After that the
database wins and the page is where boxes are changed.

| Field | Meaning |
|---|---|
| `name` | 1-32 letters, digits, `_`, `-`. |
| `where` | `local` (this machine) or `ssh`. |
| `ssh_host` | `user@host` for an ssh box. |
| `ssh_key` | a key file name inside `~/.ssh` (file name only). Optional. |
| `remote_port` | the engine's port on the box (Draw Things, A1111, Forge, SD.Next: 7860 by default). |
| `local_port` | unique per box. A local box's engine port; for an ssh box, the local end of the tunnel RenderPool keeps open (`ssh -N -L local_port:127.0.0.1:remote_port`). |
| `engine` | a plug-in name (`a1111`). |
| `app` | `drawthings` or `other` (any other A1111-style app). Draw Things boxes get the model folder listing, relaunch and needs-you checks; `other` boxes list their models with `GET /sdapi/v1/sd-models`. |
| `os` | `macos`, `windows` or `linux` (picks the power check). |
| `abilities` | `["image"]`, `["video"]` or both. A pool can only be checked for an ability the box has. |
| `laptop` | `true`: out of the pool on battery. |
| `model_map` | `{"cleared_file": "file_on_this_box"}` for a box that holds a different file for a cleared model (e.g. a GGUF for sd-server). The value must be in that cleared model's `equivalents`. |
| `pools` | `{"image": bool, "video": bool}`: the checkboxes. |
| `speed_rank` | expected seconds per picture before any history exists. |

Every field is validated when it is saved. Shell metacharacters, spaces and newlines are refused in
`name`, `ssh_host` and `ssh_key`.

**Commands are built, never typed.** The commands RenderPool runs for a box (ssh tunnel, model list,
relaunch, power check, app check) are built in `boxes.py` from those checked fields as argv lists,
with fixed remote command words and `shlex.quote`. Nothing runs through a local shell, no command
can be set over HTTP, and commands are run, never logged.

| Check | macOS | Windows | Linux |
|---|---|---|---|
| on AC | `pmset -g batt` contains `'AC Power'` | `(Get-CimInstance Win32_Battery).BatteryStatus` prints `2` (unverified on real hardware) | a `Mains` adapter under `/sys/class/power_supply` reads `online` = 1 (tested against a sample folder, not yet on a real laptop) |
| app running (Draw Things) | `pgrep -x DrawThings` | | |
| relaunch (Draw Things) | `open -g -a "Draw Things"` | | |
| models (Draw Things) | `ls` of `~/Library/Containers/com.liuliu.draw-things/Data/Documents/Models` (`*.ckpt`) | | |
| models (other apps) | `GET /sdapi/v1/sd-models` | same | same |

## Video

Draw Things makes a video through the same `POST /sdapi/v1/txt2img` call, with a video model in `model` and two
more fields: `num_frames` (1-201) and `fps` (1-30). It answers `{"images": [...]}`, a list of base64 pictures;
RenderPool passes the request and that answer through unchanged. Sources: Draw Things' `Libraries/Invocation/Sources/
Parameters.swift` (the ranges) and `Libraries/HTTPAPIServer/Sources/ServerModels.swift` (`SuccessResponse.images`)
in https://github.com/drawthingsai/draw-things-community. LTX models step in 8 frames, so use 8k+1 frames
(9, 17, ... 121). A video job gets the `video` run timeout (default 3600 s).

## History

- Every successful job gets a thumbnail (`thumb_px`, default 256 px long side, made with macOS `sips`; elsewhere
  the picture itself is stored and the page scales it). A video's thumbnail is its first frame, with a "video"
  badge and its frame count; a video with no pictures shows a plain "video" tile.
- A full copy is kept only for `keep_copies_for` sources; clicking such a thumbnail opens it. A kept video's
  frames go in one folder, `outputs/<stamp>-<item>-frames/f0001.png ...`; the click opens the first frame.
- Prompts stay hidden as before (API jobs always, plus `hide_prompt_for`); the pictures themselves are shown.
- **Clear history** (page button, with a confirm; API verb `clear-history`) deletes every finished job (done,
  failed, canceled, abandoned, orphaned) and its thumbnail and copies. Queued and running jobs are never touched.
- **Autoclear**: on start and every hour, finished jobs older than `history_keep_days` (default 7; 0 = keep)
  are deleted the same way.

## Cleared models

`cleared_models` is the allow-list: `[{"file": "...", "kind": "image"|"video", "label": "...",
"license": "...", "equivalents": [...]}]`. Put a model here only when you may use it for your purpose
(for example, commercially). A model is cleared only with a cited license: every entry needs a non-empty
`license` (e.g. `"Apache-2.0 (black-forest-labs/FLUX.2-klein-4B LICENSE)"`), or RenderPool refuses to start.
`equivalents` lists the other files (another quantization, a GGUF) a box's `model_map` may run for that
model; it is set in the config file only, after you have accepted their pictures. There is
no deny-list: anything not listed is never run, and a box's other files show greyed out as
"test only", "support file" or "unknown type" on the Boxes screen. `default_model` must be one of the cleared files.

`fixed` (optional) lists parameters a model always runs at, e.g. `"fixed": {"guidance_scale": 1.0}` for FLUX.2
klein (a distilled model). RenderPool writes them into every job for that model, from the API or the page, and the
page hides those fields. Draw Things reads guidance as `guidance_scale` (A1111's name is `cfg_scale`; the page
hides the Guidance field for either).

## Test models

Every other GENERATION model a box holds is a test model: sources in `test_sources` (default `["me", "gui"]`,
the page submits as `gui`) may run it; anyone else (a caller that sends no `X-RenderPool-Source`, so it is `api`) gets the same
422 as before. The page lists test models in their own "TEST ONLY - not cleared for production" group, and
queue and History mark their jobs. `test_notes` (`{"file name prefix": "text"}`) adds a license note beside
matching files, e.g. `"qwen_image_2.1": "Qwen RESEARCH license, non-commercial (test only)"`.

Draw Things' HTTP API lists no models, and its built-in model metadata is not on disk (`custom.json` in the
Models folder covers imported models only), so a file-name rule (`classify_file` in `renderpool.py`) decides:
support files (VAEs, text encoders such as `qwen_3_*` and `gemma_*`, upscalers, LoRAs, control models) are never
offered; `ltx*`, `wan*2*`, `hunyuan_video*`, `minimax_h3*`, `svd*` ... are video; `flux*`, `sd*`, `qwen_image*`,
`hidream*`, `z_image*` ... are image. A file the rule cannot place is listed on the page as "unknown type" and
never run; set it in `test_kinds` (`{"file.ckpt": "image"|"video"|"support"}`) to change that.

## Engine plug-ins

Every engine answers three questions for one box. Write `engines/<name>.py` with one class, add it to
`ENGINES` in `engines/__init__.py`, and make `test_engine_contract.run_contract` pass against a fake
server for it.

```python
class MyEngine:
    def __init__(self, base_url, box, run_cmd=None, dt_models_dir=None): ...
    def is_up(self, timeout) -> bool
    def list_models(self) -> set | None            # None = unknown; RenderPool then never uses the box
    def run(self, endpoint, body, timeout) -> (status, content_type, body_bytes)
        # raise engines.EngineDown(reason) when nothing answers (the job is re-queued)
        # raise engines.EngineTimeout when the run timed out (the caller gets 504)
```

`base_url` is `http://127.0.0.1:<local_port>`. `run_cmd(which, argv, timeout) -> (rc, stdout)` runs an
argv built by `boxes.commands()`; an engine never builds its own shell command.

The `a1111` plug-in: `GET /` for `is_up` (any answer below 500 counts, since API-only servers may have
no page there); `POST /sdapi/v1/<endpoint>` for `run`; models from the Draw Things Models folder for
`app: drawthings`, else `GET /sdapi/v1/sd-models`.

**Wanted: more engines.** ComfyUI, InvokeAI and SwarmUI each have their own API and each need one
class like `engines/a1111.py`. `test_engine_contract.py`
checks any engine against the contract.

## Optional request headers

| Header | Meaning |
|---|---|
| `X-RenderPool-Source` | who asked (`storyapp`, `me`, `gui`, ...). Default `api`. Drives `keep_copies_for`. |
| `X-RenderPool-Group` | a batch name; groups can be paused or canceled together. |
| `X-RenderPool-Label` | short text shown on the page. |
| `X-RenderPool-Backend` | hard pin: the job runs only on this box. |
| `X-RenderPool-Key` | idempotency key. A request whose key is queued or running **attaches** to that job (one picture, several waiters); a key that finished within `key_replay_s` gets the stored response (`X-RenderPool-Replay: 1`). |
| `X-RenderPool-Seat` | who sent the job (a person, script or AI session; up to 40 characters). Shown as 👤 on every queue and running line, and usable for `bump`. |
| `X-RenderPool-Priority` | `high` (5), `normal` (0, default), `low` (-5) or a whole number -9..9. A new job is placed after every queued job of the same or higher priority, so equal priorities keep arrival order. Anything else is a 400. Hand moves and `bump` still reorder freely. |
| `X-RenderPool-Max-Wait` | seconds to wait for the job to **start** (default `default_max_wait_s`, 1800; `0` = no limit). If it has not started by then RenderPool answers `503 {"error": "RenderPool: still queued", "renderpool": "queued", "item", "position", ...}` with `Retry-After`, and the job keeps its place. Retry with the same key to pick it up. |

Nothing depends on a header: a caller that sends none gets arrival order, a hidden prompt and one
open request that waits up to 30 minutes for its job to start.

Every picture answer carries `X-RenderPool-Backend` (the box), `X-RenderPool-Model` (the file actually run) and `X-RenderPool-Item`.

## Behavior worth knowing

- `GET /` answers 200 whenever RenderPool is up, whatever the boxes are doing (callers read 200 as "up").
  "No box can run this" is a 422 on the POST, never a down root.
- Prompts of API jobs are never returned by the API or shown on the page (only the label). Page
  submits show theirs; `hide_prompt_for` hides more sources.
- Pictures are copied to `outputs/` (thumbnails) only for sources in `keep_copies_for`.
- The caller's connection stays open while its job waits. If the last caller disconnects while the
  job is queued it is `abandoned` - unless it was sent with an `X-RenderPool-Key`, which keeps its place; while
  drawing, the picture finishes and the job is `orphaned` (with a key, the answer is kept for `key_replay_s`).
- **Collect a picture later**: `GET /renderpool/api/result/<item>` -> 200 with the stored answer (keyed jobs,
  for `key_replay_s` after they finish), 202 `{"state","position"}` while queued or running, 410 when it
  finished but nothing was kept, 404 for an unknown item. Sending the same key again does the same.
- **Re-sort the queue**: `POST /renderpool/api/bump {"seat"|"source"|"model"|"group": X, "to": "front"|"back"}`
  moves every queued job matching that one field to the top (or bottom), keeping their order. On the
  page, click a 👤 seat or a model chip; from the shell, `renderpool.py ctl bump --seat batch-script`.
- Canceling only cancels queued jobs; a running job always finishes.
- After a restart, jobs whose caller was waiting are `abandoned`; page submits are queued again.
- `GET /renderpool/api/group/<group>` gives `{"group","paused","all_paused","queued","running","position_of_first"}`.

## Access

RenderPool listens only on the addresses in `listen`: loopback, and/or a tailnet address
(`100.64.0.0/10`), and a private LAN address only with `"allow_lan": true`; anything else
(including `0.0.0.0`) is refused at start. A browser POST whose `Origin` is not loopback, a `listen`
address or one of `allowed_origins` is refused with 403. Without a password, anyone who can reach a
`listen` address can edit boxes, and RenderPool will ssh to a box's host with the configured key.
See `SECURITY.md`.

### Password

Optional, off by default. Set `"password"` in the config, or better the `RENDERPOOL_PASSWORD`
environment variable (it wins over the file and keeps the password out of it). Then every call except
`GET /` (health) and the page itself needs one of:

- `Authorization: Bearer <password>` (scripts; most A1111 clients can add a header);
- `X-RenderPool-Password: <password>`;
- the page's login cookie: the page asks once, and the cookie holds a keyed hash, never the password.

`renderpool.py ctl` sends `RENDERPOOL_PASSWORD` from its own environment. A wrong page login waits half a
second. The password is sent in clear over plain HTTP: on a LAN, put RenderPool behind HTTPS or a tailnet.

## Configuration

See `config.example.json`. Relative paths are resolved against the config file's folder.

| Key | Default | Meaning |
|---|---|---|
| `listen`, `port` | `["127.0.0.1"]`, `7875` | addresses (loopback or tailnet only) and port. |
| `allowed_origins` | `[]` | extra browser Origin host names, e.g. this machine's tailnet name. |
| `cleared_models`, `default_model` | | see above (required). |
| `test_sources` / `test_kinds` / `test_notes` | `["me","gui"]` / `{}` / `{}` | see Test models. |
| `history_keep_days` | `7` | autoclear age for finished jobs (0 = keep). |
| `thumb_px` | `256` | thumbnail long side. |
| `backends` | `[]` | box records that seed the state database on the first start only. |
| `db_path` | `state/renderpool.db` | SQLite queue, history and box settings. |
| `outputs_dir` | `outputs` | copies of finished pictures for `keep_copies_for` sources. |
| `keep_copies_for` | `["me","gui"]` | sources whose pictures are copied. |
| `hide_prompt_for` | `[]` | extra sources whose prompts are hidden (API jobs always are). |
| `default_max_wait_s` | `1800` | wait-to-start limit when the caller sends no `X-RenderPool-Max-Wait`. |
| `run_timeout_s` | `{"image": 900, "video": 3600}` | per-kind run timeout (caller gets 504). |
| `power_interval_s` | `30` | how often a laptop's power source is re-read. |
| `notify_cmd` | `[]` | argv run once when a box needs you; the message is added as its last argument. |
| `retry_after_s` | `30` | `Retry-After` on the still-queued 503. |
| `key_replay_s` | `600` | how long a finished keyed answer is kept. |
| `group_affinity` / `affinity_max_wait_s` | `true` / `120` | prefer the box a group last ran on while its expected wait is below this. |
| `inventory_refresh_s` | `300` | how often each box's model list is re-read. |
| `probe_interval_s` / `probe_timeout_s` | `10` / `5` | health probe (`GET /`) of idle boxes. |
| `down_retry_s` | `60` | re-probe interval for a down box. |
| `relaunch_wait_s` | `90` | wait after a relaunch; also how long an open app's API may stay down before "needs you". |
| `max_attempts` | `3` | dispatches before a job whose box keeps dropping fails. |
| `drawthings_models_dir` | the Draw Things container path | where Draw Things keeps its models. |

## Command line

```
renderpool.py serve [--config FILE] [--port N] [--verbose]
renderpool.py ctl [--url URL | --config FILE] [--json] VERB
  list | status
  pause  [--group G | --item N]      resume [--group G | --item N]
  front N | up N | down N | move N POS
  cancel N | cancel --group G
  pin N [BOX]                        (no BOX = unpin)
  pool BOX image|video on|off
  backend BOX on|off                 (same as: pool BOX image on|off)
  group G
  refresh-models
```

The page and the CLI use one JSON API: `GET /renderpool/api/state`, `GET /renderpool/api/boxes`, and
`POST /renderpool/api/<verb>` with a JSON body (`Content-Type: application/json`). `clear-history` takes `{}`. Box verbs:
`box-add`, `box-edit` (a whole record), `box-remove {name}`, `pool {box, kind, on}`.

## Tests

```sh
python3 test_boxes.py
python3 test_engine_contract.py
python3 test_renderpool.py                     # about 2.5 minutes
# or all three: python3 -m unittest discover -s . -p 'test_*.py'

dt-grpc-bridge/setup.sh && dt-grpc-bridge/.venv/bin/python -I dt-grpc-bridge/test_bridge.py   # optional
```

The tests use fake engines on random loopback ports and a fake command runner; they never contact
a real engine and never run ssh.

## Known limits

- A local Draw Things box run by launchd: macOS may refuse the launchd-run python access to Draw Things'
  container (App Data privacy), so the model list fails (`model list command exited 1`) and the box is never
  used. Grant that python Full Disk Access, or run that box over ssh.
- One monitor thread probes every box in turn, so a slow or hanging box (a probe or ssh command at its
  timeout) delays the health, power and model checks of the others by that long.
- One optional shared password, no user accounts (see Access and `SECURITY.md`).
- Thumbnails and the prompt-test preview's resize to the size asked for use macOS `sips`. On a Linux host,
  thumbnails fall back as described under History and previews keep the engine's own size.
- `renderpool.py serve` imports the Unix-only `resource` module, so the RenderPool host must be macOS or Linux.

## License

MIT, see `LICENSE`. Draw Things, AUTOMATIC1111 Stable Diffusion WebUI, Forge, SD.Next, stable-diffusion.cpp,
KoboldCpp, Stability Matrix, ComfyUI, InvokeAI and SwarmUI are third-party projects named here only to
say what RenderPool works with; this project is not affiliated with any of them. `dt-grpc-bridge/setup.sh` downloads GPL-3.0 files from Draw Things' community repository at
install time; they are not part of this repository (see `dt-grpc-bridge/THIRD_PARTY.md`).
