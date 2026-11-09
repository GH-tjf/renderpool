# Contributing

Thanks for helping. A few ground rules keep RenderPool easy to run anywhere:

- **Standard library only** for `renderpool.py`, `boxes.py` and `engines/`. RenderPool must start on a bare
  `python3` (3.9 or newer) with nothing installed. Anything that needs third-party packages lives in
  its own folder with its own venv, like `dt-grpc-bridge/`.
- **Tests with every change.** Add or update a test in `test_renderpool.py`, `test_boxes.py` or
  `test_engine_contract.py`. Tests use fake engines on loopback and a fake command runner; they must
  never reach a real engine, the network or ssh.
- **Commands are built, never typed.** Any new command RenderPool runs for a box goes through `boxes.py` as
  an argv list from validated fields, never through a shell.
- **No secrets, hostnames or personal paths** in code, tests, examples or commit messages.

## Running the tests

```sh
python3 test_boxes.py
python3 test_engine_contract.py
python3 test_renderpool.py                    # about 2.5 minutes
```

The bridge has its own tests:

```sh
dt-grpc-bridge/setup.sh
dt-grpc-bridge/.venv/bin/python -I dt-grpc-bridge/test_bridge.py
```

CI (`.github/workflows/tests.yml`) runs the three RenderPool test files on Linux and macOS for Python 3.9-3.13.
A few `test_renderpool.py` tests depend on timing; if one fails once on a busy machine, rerun it and mention
it in the pull request.

## A new engine

Write `engines/<name>.py` with one class (see "Engine plug-ins" in the README), add it to `ENGINES` in
`engines/__init__.py`, and make `test_engine_contract.run_contract` pass against a fake server for it.

## Pull requests

Keep them small and focused, describe the behavior change in plain words, and add a line to
`CHANGELOG.md` under "Unreleased".
