# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-08

First public release.

### Added

- One queue in front of several image/video machines, speaking the A1111-style HTTP API
  (`GET /`, `POST /sdapi/v1/txt2img`, `POST /sdapi/v1/img2img`) that Draw Things serves.
- Image and video pools with a per-machine checkbox; routing to a free, online, on-AC box that holds
  the exact model file (or a mapped equivalent).
- Boxes screen: add, edit and remove machines from the web page; local and ssh-tunneled boxes,
  macOS and Windows power checks, Draw Things relaunch and "needs you" detection.
- Cleared-model allow-list with a required license note per model; test tier for other models from
  RenderPool's own page.
- History with thumbnails, autoclear, and full copies for chosen sources.
- Optional request headers: source, group, label, backend pin, idempotency key, seat, priority,
  max wait. Collect a result later with `GET /renderpool/api/result/<item>`; re-sort the queue with `bump`.
- Settings tab, applied live and saved to the config file.
- `renderpool.py ctl` command line over the same JSON API.
- `dt-grpc-bridge/`: an optional A1111-style HTTP front for Draw Things in gRPC mode, including
  moodboard images. Its Draw Things protocol files (GPL-3.0) are downloaded by `setup.sh`.
- Works with any app serving the A1111-style API: AUTOMATIC1111, Forge, SD.Next, stable-diffusion.cpp
  `sd-server`, KoboldCpp, Draw Things (README "Works with"). An app with no page at `/` (404) still
  counts as up.
- Linux boxes (`"os": "linux"`), with a sysfs on-AC check for Linux laptops.
- Optional password (`password` setting or `RENDERPOOL_PASSWORD`): Bearer header, a password header,
  or a page login cookie.
- Stalled Draw Things jobs move on: `dt-grpc-bridge` cancels a request that has been silent for
  `DT_BRIDGE_STALL_S` (120 s) and answers 503 + `X-Box-Down`; RenderPool requeues the job and that box
  sits out one `down_retry_s`.
- Packaging (`pyproject.toml`, `renderpool` command), launchd and systemd examples, CI on Linux and
  macOS for Python 3.9-3.13.
