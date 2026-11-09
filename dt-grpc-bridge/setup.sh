#!/bin/bash
# Set up dt-grpc-bridge:
#   1. create the bridge's private venv (.venv beside this file) and install requirements.txt into it;
#   2. download Draw Things' protocol files from drawthingsai/draw-things-community at a pinned commit,
#      check their SHA-256, and generate imageService_pb2*.py from imageService.proto.
# Those files are GPL-3.0 (see THIRD_PARTY.md), so they are fetched here instead of shipped in this repo.
# No global, brew or pip --user installs: everything lands in .venv and in this folder.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

DT_COMMIT="37d071b0697b2c6965276538cfe8c05048dd87e1"   # draw-things-community main, 2026-11-08
DT_SHORT="37d071b069"
DT_RAW="https://raw.githubusercontent.com/drawthingsai/draw-things-community/$DT_COMMIT"
# local name | upstream path | sha256
FILES="imageService.proto|Libraries/GRPC/Models/Sources/imageService/imageService.proto|64f250b6d2ae34cc55e567590f208e9da49afd1ff786f898d035c3c5becce9e7
config.fbs|Libraries/DataModels/Sources/config.fbs|e496aae127d7b2c7e2c5cd31f8bccd6e7fdc94b5000f48abb07335f4711ecf41
dt_root_ca.crt|Libraries/BinaryResources/Resources/root_ca.crt|3a3094b120b0278a3c5f1112229c00f7b5c494558e526706f8d0c8de62b72de3"

# PYTHON=/path/to/python3 picks the interpreter for the venv; otherwise the newest python3.x on PATH.
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 /usr/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
fi
[ -n "$PY" ] || { echo "setup.sh: no python3 found (set PYTHON=/path/to/python3)" >&2; exit 1; }
if [ ! -x "$HERE/.venv/bin/python" ]; then
  "$PY" -m venv "$HERE/.venv"
fi
VPY="$HERE/.venv/bin/python"
"$VPY" -I -m pip install --quiet --upgrade pip
"$VPY" -I -m pip install --quiet -r "$HERE/requirements.txt"

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
echo "$FILES" | while IFS='|' read -r name path want; do
  if [ -f "$HERE/$name" ] && [ "$(sha256 "$HERE/$name")" = "$want" ]; then continue; fi
  curl -fsSL -o "$HERE/$name.part" "$DT_RAW/$path"
  got="$(sha256 "$HERE/$name.part")"
  if [ "$got" != "$want" ]; then
    rm -f "$HERE/$name.part"
    echo "setup.sh: $name from $DT_RAW/$path has sha256 $got, expected $want" >&2
    exit 1
  fi
  mv "$HERE/$name.part" "$HERE/$name"
  echo "fetched $name (draw-things-community @ $DT_SHORT)"
done

# grpcio-tools only generates code; it is not needed at run time.
"$VPY" -I -m pip install --quiet "grpcio-tools>=1.80.1"
( cd "$HERE" && "$VPY" -I -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. imageService.proto )
"$VPY" -I -c "import grpc, google.protobuf, flatbuffers, numpy, PIL, fpzip; print('dt-grpc-bridge venv ok: grpcio', grpc.__version__)"
( cd "$HERE" && "$VPY" -I -c "import sys; sys.path.insert(0, '.'); import imageService_pb2, imageService_pb2_grpc; print('protocol files ok')" )
