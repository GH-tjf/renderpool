# Security

## What RenderPool does and does not protect

**RenderPool's only authentication is an optional shared password, off by default** (README, "Password").
There are no user accounts. Without the password set, anyone who can reach the address and port
RenderPool listens on - and with it, anyone who knows the password - can:

- submit, reorder, pause and cancel jobs;
- read the queue and history (API job prompts are hidden, pictures are not);
- add, edit and remove boxes - including a box's `ssh_host` and `ssh_key` - which makes RenderPool open ssh
  connections from your machine with that key;
- change settings on the Settings tab, which RenderPool writes back to its config file.

What RenderPool does to limit that:

- It listens only on loopback (`127.0.0.1`, `::1`) and/or tailnet addresses (`100.64.0.0/10`, the range
  Tailscale and similar VPNs use). Any other `listen` address, including `0.0.0.0`, is refused at start.
- A browser request whose `Origin` is not loopback, a `listen` address or one of `allowed_origins` is
  refused with 403, so an ordinary web page you visit cannot drive RenderPool through your browser.
- Commands it runs for a box are built as argv lists from validated fields (`boxes.py`); nothing goes
  through a shell, and no command text can be set over HTTP.
- An ssh key is referred to by file name inside `~/.ssh` only; RenderPool never reads or returns key contents.

**Run it only where every device that can reach it is yours.** Do not put it behind a public reverse
proxy or port forward. If you listen on a tailnet address, everyone on that tailnet (and anyone your
tailnet ACLs let in) has full control of RenderPool.

An optional access token is a planned follow-up; until then, treat network reachability as the only
access control.

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" button on this
repository's Security tab, not in a public issue. Include what you did, what happened and the RenderPool
version (`renderpool.py --version`). You should get a reply within two weeks.
