# Running as a background service on macOS

How to keep the HTTP server running on a Mac that nobody is sitting at, and the macOS behaviors
that will otherwise cost you an afternoon.

Real hostnames, ports and tunnel URLs belong in `docs/local/`, which is gitignored. This file
uses `imessage.example.com` throughout.

## It must be a LaunchAgent, not a LaunchDaemon

Two reasons, and the second one only bites later.

Reading the message database is protected by Full Disk Access, which is granted per user. And
sending, when it arrives, drives Messages.app through Apple Events, which needs a logged-in GUI
session. A root LaunchDaemon has no session, so sends would fail with nothing useful in the log.

Install into `~/Library/LaunchAgents/`, and make sure the machine auto-logs in so the session
exists after a reboot.

## Invoke the interpreter directly, not `uv run`

`uv run` spawns the interpreter as a child, so launchd supervises the wrapper. Killing the job
leaves the real server holding the port. Point `ProgramArguments` at the venv's interpreter.

## Template

Save as `~/Library/LaunchAgents/com.example.imessage-mcp.plist`, replacing the placeholders. It
contains a password, so `chmod 600` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.imessage-mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/path/to/imessage-mcp/.venv/bin/python</string>
    <string>-m</string>
    <string>imessage_mcp</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/path/to/imessage-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>IMESSAGE_MCP_TRANSPORT</key><string>http</string>
    <key>IMESSAGE_MCP_HOST</key><string>127.0.0.1</string>
    <key>IMESSAGE_MCP_PORT</key><string>18791</string>
    <key>IMESSAGE_MCP_AUTH</key><string>password</string>
    <key>IMESSAGE_MCP_PASSWORD</key><string>REPLACE-WITH-A-LONG-RANDOM-VALUE</string>
    <key>IMESSAGE_MCP_BASE_URL</key><string>https://imessage.example.com</string>
    <key>IMESSAGE_MCP_STATE_DIR</key><string>/Users/USERNAME/.imessage-mcp</string>
    <!-- Without this, Python block-buffers to the log file and it stays empty,
         which makes a startup problem look like total silence. -->
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/.imessage-mcp/server.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.imessage-mcp/server.log</string>
</dict>
</plist>
```

```bash
chmod 600 ~/Library/LaunchAgents/com.example.imessage-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.imessage-mcp.plist
curl -s localhost:18791/health
```

Bind to `127.0.0.1` and reach it through a tunnel or reverse proxy.

## The privacy prompt that hangs the service

**This is the one that will catch you.** Reading `~/Library/Messages/chat.db` means reading data
macOS protects. A LaunchAgent has no approval for that, so on first start the system raises a
consent dialog — and if the Mac is headless or unattended, that dialog sits unanswered and the
process **blocks indefinitely inside `open()`**.

It does not crash, does not log, and does not time out. `launchctl list` reports it running with a
healthy PID, the port is never bound, and the log file is empty. With `KeepAlive` set, launchd
keeps restarting it and each attempt stacks another dialog.

Running the same command over SSH works fine, which is thoroughly misleading: your shell inherits
an approval that the LaunchAgent does not have.

**Fix:** grant Full Disk Access to the interpreter, before first launch. System Settings → Privacy
& Security → Full Disk Access → `+`, then `Cmd+Shift+G` in the picker to type the path, since it
is usually hidden. Grant the **resolved** binary, not the venv symlink:

```bash
python3 -c "import os; print(os.path.realpath('.venv/bin/python'))"
```

Clicking Allow on the popup is often not enough. Interpreters installed by `uv` and similar tools
are ad-hoc signed with an empty identifier, and macOS binds approvals to a code-signing identity.
With nothing to bind to, the prompt returns on every launch and the approval never sticks. An
explicitly added Full Disk Access entry is recorded against the path and does work.

### It will break again when the interpreter is upgraded

Tool-managed interpreters live at version-stamped paths:

```text
~/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12
```

Approval is granted against that path. A patch upgrade moves the binary, silently invalidating it,
and the service goes back to hanging on startup with no error anywhere. A `.python-version`
holding only `3.12` permits exactly that upgrade.

`GET /health` reports the resolved interpreter path so the change is visible before it bites.
`scripts/healthcheck.sh` compares it against `EXPECTED_PYTHON` and fails when it moves.

## Contacts need no second permission

Worth stating because it is easy to assume otherwise, and assuming otherwise leads to a design
that cannot be deployed unattended.

Contact names are resolved by reading the AddressBook sqlite files directly, which is plain file
access covered by Full Disk Access. Verified on macOS 26.4.1: a process holding Full Disk Access
and no Contacts grant read the address book without a prompt.

Going through the Contacts API instead — `CNContactStore`, or scripting Contacts.app — needs the
separate Contacts permission, which **cannot be pre-granted** from System Settings. It has to be
answered at least once, interactively, on a machine nobody is sitting at.

## Messages.app must be running

Only for sending -- reads come off the database whether or not it is up. But a host where
Messages has quietly quit serves every read correctly and drops every send, which is a confusing
way to find out. Set the host to launch Messages at login, and let `/health` report
`messages_running` so the monitor catches it.

## A second prompt, when sending arrives

Sending drives Messages.app through Apple Events. That is a separate protection from file access
and produces its own dialog the first time a message is sent:

> "python3.12" wants access to control "Messages".

Until it is answered the tool call hangs, the same way startup does. Everything else keeps
working, so this can lie dormant and then surface the first time a send is attempted from a phone.

Trigger it deliberately while you are at the machine rather than letting it ambush a remote client
later. Like Full Disk Access, it cannot be granted ahead of time: System Settings → Privacy &
Security → Automation only lets you toggle pairs macOS has already recorded.

This does not apply yet — the current milestone is read-only — but it is the first thing to hit
when sending lands.

## `/health` is public, so keep it boring

`/health` is a custom route, and custom routes do **not** sit behind the auth
provider: it answers `200` to an unauthenticated request from the open internet
while `/mcp` does not. That is deliberate — an external uptime monitor has to
reach it without credentials, and a monitor running on the host cannot report
that the host is gone.

The consequence is a rule about the payload, not the routing: **nothing goes in
it that you would not publish.** The interpreter path is reported because a uv
upgrade moving it is what silently voids Full Disk Access, but it is reported
relative to `~`, since the absolute form begins with the operator's account
name and publishing that buys nothing. `scripts/healthcheck.sh` compares the
interpreter by resolving it locally, so this costs the check nothing.

## Monitoring

`scripts/healthcheck.sh` checks a running server and reports to a dead-man's-switch service.
Either healthchecks.io or a self-hosted Uptime Kuma works; the script speaks both. Configure it
in `~/.imessage-mcp/check.env`:

```bash
HEALTH_URL=http://127.0.0.1:18791/health
PING_URL=https://kuma.example.com/api/push/TOKEN   # or https://hc-ping.com/UUID
PING_STYLE=auto       # auto | kuma | healthchecks
EXPECTED_PYTHON=/Users/USERNAME/.local/share/uv/python/cpython-3.12.13-.../bin/python3.12
VENV_PYTHON=/Users/USERNAME/path/to/imessage-mcp/.venv/bin/python
MAX_QUIET_SECONDS=0   # staleness check off; see below
```

`auto` recognizes a Kuma push URL by its `/api/push/` path, so `PING_STYLE` only needs setting
when a reverse proxy has rewritten that away.

Expand `$REPO` and `~` yourself; cron does neither.

```cron
*/10 * * * * $REPO/scripts/healthcheck.sh >> ~/.imessage-mcp/check.log 2>&1
```

`chmod 600` the config: the ping URL is a capability, not just an address.

It reports failure on five things:

- **No response.** Either down, or hung on a permission prompt. A timeout is meaningful here,
  since the documented failure mode is a hang rather than a crash.
- **The database is unreachable.** `/health` returns 503 and says so.
- **Messages.app is not running.** Reads keep working, so nothing else looks wrong, but sending
  would fail. The host should be set to launch Messages at login.
- **The last send failed.** The failure the one above does not catch: with the Apple Events grant
  revoked, Messages is running and every read works while every send is dropped. Nothing else
  about the server looks wrong.
- **The interpreter moved.** The early warning for the privacy-approval problem above. Re-grant
  Full Disk Access and update `EXPECTED_PYTHON` together.

It can also flag an archive that has received nothing recently, but this is **off by default**
(`MAX_QUIET_SECONDS=0`) and deserves care. The age of the newest message cannot tell "Messages has
stopped syncing" apart from "nobody has texted me" — a quiet weekend looks exactly like a broken
sync on a personal account. The age is reported on every run regardless, so let the log show what
an ordinary lull looks like, then pick a threshold no genuine silence would reach.

An outward ping is what makes the whole machine being gone detectable. A monitor running on the
same host cannot report its own host's death — which is the one thing to get right when the
monitor is self-hosted rather than a service. Kuma has to run somewhere other than this Mac, or
it goes down with the thing it is watching and reports nothing.

### With Uptime Kuma

Two monitors, doing different jobs.

**A Push monitor**, which is Kuma's dead-man's switch and the direct equivalent of a
healthchecks.io check. Create it, copy the push URL it shows into `PING_URL`, and set its
heartbeat interval comfortably longer than the cron period — 15 minutes against a 10-minute cron,
so one slow run is not an alert. The script pushes `status=up` with the same one-line report it
logs, and `status=down` with the reason on a failure, which Kuma shows in its event table.

This is the monitor that carries the checks only the host can make: the interpreter having moved
out from under Full Disk Access, Messages.app not running, the last send having failed.

**An HTTP(s) monitor with a Json Query**, pointed at the public `/health` through the tunnel, with
the query `status` and expected value `ok`. This is the outside-in view the push monitor cannot
give: it fails when the tunnel is down, when the server is hung on a permission prompt, or when
`/health` returns its 503 for an unreachable database. It needs no credentials, which is exactly
why `/health` is unauthenticated.

Self-hosting removes the check limit that makes a hosted plan a decision, so run both rather than
choosing. Neither replaces the other — the push monitor knows things `/health` deliberately does
not publish, and the HTTP monitor knows whether anything outside the house can reach the server at
all.

The push URL is a capability in the same way the healthchecks.io ping URL is: anyone holding it
can silence the alarm. Keep it in `check.env` at `chmod 600`, and out of this repository — the
tracked docs say `kuma.example.com`, and the real host belongs in `docs/local/`.

## Deploying by pushing

`scripts/self-update.sh` pulls the tracked branch, syncs dependencies if they moved, and restarts
the service, so a push is a deploy. Configure it in `~/.imessage-mcp/update.env`:

```bash
REPO_DIR=/Users/USERNAME/path/to/imessage-mcp
BRANCH=main
LAUNCH_LABEL=com.example.imessage-mcp   # omit to skip the restart
PING_URL=https://kuma.example.com/api/push/A-DIFFERENT-TOKEN
```

Give the deploy its own monitor rather than reusing the healthcheck's. An hourly deploy and a
ten-minute check cannot share one heartbeat interval, and a quiet deploy would keep marking the
health check up.

By default it refuses to touch a checkout whose tracked files have been modified, assuming
somebody is debugging in place. On a host that is only ever deployed to, that assumption is wrong
and expensive: one stray edit wedges every future deploy and nobody is reading the log. Set
`RESET_HARD=true` there.

One trap: **build the virtualenv where it will finally live.** `uv` records absolute paths, so a
venv created in one directory and then moved leaves the editable install pointing at the old
location, and the service fails with `No module named imessage_mcp`. Re-run `uv sync` after any
move.

## Checking on it

```bash
launchctl list | grep imessage-mcp     # pid, last exit code
curl -s localhost:18791/health         # liveness, newest message, interpreter path
tail -f ~/.imessage-mcp/server.log
```

A hang looks like: a live PID, nothing on the port, and an empty log. That is the privacy prompt
above, not a crash.
