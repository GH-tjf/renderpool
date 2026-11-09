"""RenderPool engine plug-ins.

Every engine answers three questions for one box, and nothing else in RenderPool talks to an engine:

    class Engine:
        def __init__(self, base_url, box, run_cmd=None, dt_models_dir=None): ...
        def is_up(self, timeout) -> bool
        def list_models(self) -> set | None          # None = unknown; RenderPool then never uses the box
        def run(self, endpoint, body, timeout) -> (status, content_type, body_bytes)
            # raises EngineDown(reason) when nothing answers, EngineTimeout when the run timed out

base_url is http://127.0.0.1:<local_port> (a remote box is reached through its ssh tunnel). box is the
validated box record. run_cmd(which, argv, timeout) -> (rc, stdout) runs an argv built by
boxes.commands(); an engine never builds its own shell command. Commands are run, never logged.

To add an engine: write engines/<name>.py with one class, add it to ENGINES below, and make
test_engine_contract.run_contract pass against a fake server for it.
"""


class EngineDown(Exception):
    """Nothing answered (refused, reset, unreachable). The job goes back to the queue."""


class BoxSaysDown(EngineDown):
    """The box answered, but said it cannot draw right now (e.g. its app stalled). The job goes back to
    the queue and the box sits out one down_retry_s, so the job goes to another box first: a quick health
    probe can pass while the app is still wedged."""


class EngineTimeout(Exception):
    """The engine took longer than the run timeout. The caller gets 504."""


from engines.a1111 import A1111Engine  # noqa: E402

ENGINES = {"a1111": A1111Engine}
