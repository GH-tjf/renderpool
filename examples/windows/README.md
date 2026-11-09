# RenderPool and Windows

**Windows as a box** (a render machine RenderPool sends work to, over ssh) is supported: set `"os": "windows"`
on the box. Its power check is `(Get-CimInstance Win32_Battery).BatteryStatus` (unverified on real
hardware). Windows 10/11 ship an OpenSSH server as an optional feature.

**Windows as the RenderPool host** is not supported yet: `renderpool.py serve` raises its open-file limit with the
Unix-only `resource` module and fails to start on Windows. Run RenderPool on macOS or Linux (or under WSL2).
Making the open-file step optional on Windows is a welcome contribution; once that works, either of
these keeps it running:

- **Task Scheduler**: a task "At log on" (or "At startup" with "Run whether user is logged on or not"),
  action `py.exe`, arguments `-3 C:\path\to\renderpool\renderpool.py serve --config C:\path\to\renderpool\config.json`,
  "Start in" `C:\path\to\renderpool`; under Settings, "If the task fails, restart every 1 minute".
- **NSSM** (a third-party service manager): `nssm install renderpool py.exe -3 C:\path\to\renderpool\renderpool.py serve --config ...`.
