# iMessage MCP

An MCP server that reads the local iMessage database and sends messages through
Messages.app on macOS.

It runs over stdio for local use and over HTTP for remote use, so a phone or
tablet can reach the same server that the laptop uses. That is the point of the
project: the desktop-only iMessage integrations work well, but the desktop is
the wrong place — the messages worth acting on arrive while you are away from
it.

## Status

Early. The read layer is being built; the HTTP transport, auth, and deployment
are not written yet. See `docs/scope.md` for the design, the measurements behind
it, and what is deliberately deferred.

## Requirements

- macOS, with Messages signed in
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Full Disk Access for the Python interpreter, to read `~/Library/Messages/chat.db`

Sending additionally needs permission to control Messages, which macOS prompts
for the first time a message is sent.

## Development

```bash
uv sync --extra test
uv run pytest
```

The test suite is offline and runs against a synthetic database built from
Apple's schema. It never opens the real one, so it works on a machine that has
never sent an iMessage.

## License

MIT
