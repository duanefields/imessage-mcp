# iMessage MCP

An MCP server that reads the local iMessage database and sends messages through
Messages.app on macOS.

It runs over stdio for local use and over HTTP for remote use, so a phone or
tablet can reach the same server that the laptop uses. That is the point of the
project: the desktop-only iMessage integrations work well, but the desktop is
the wrong place — the messages worth acting on arrive while you are away from
it.

## Status

Working, and in use as a remote connector.

Reading is done: the tools below are exercised through a real client over both
transports, with password-guarded OAuth 2.1 on the HTTP side and a LaunchAgent
behind a tunnel. Sending is done as well, restricted to conversations that
already exist.

Starting new conversations, attachment contents, and a full-text search index
are deliberately deferred. See `docs/scope.md` for the design, the measurements
behind it, and the condition that would bring each deferred item back.

## Tools

| Tool               | What it does                                             |
| :----------------- | :------------------------------------------------------- |
| `list_chats`       | Conversations by recent activity, or by who is in them   |
| `get_messages`     | One conversation, paginated, newest first                |
| `search_messages`  | Case-insensitive across the whole archive, not a window  |
| `get_participants` | Chat members, resolved names, service                    |
| `get_attachments`  | Metadata only: filename, MIME type, size, direction      |
| `get_unread`       | What arrived while you were away                         |
| `send_message`     | Existing conversations only, addressed by chat GUID      |

Reads are paginated and return both readable text and structured content, with
a true total alongside the page — so "3 messages" is never mistaken for the
whole answer when 137 matched.

Message text lives in the `attributedBody` typedstream blob rather than the
`text` column, which is empty on roughly 99% of messages on a current macOS.
Decoding that blob is the primary path here, not a fallback.

## Sending is constrained on purpose

A server that reads a private archive, ingests text from anybody who can message
this account, and can send is the whole lethal trifecta in one process. Two
rules bound it:

- **Existing conversations only.** There is no way to start a new one, so a
  hallucinated or mistyped number cannot reach a stranger.
- **No cross-conversation forwarding.** Read tools record which conversation
  they showed text from; `send_message` refuses text that reproduces something
  read from a different one, and names where it came from. `confirm_forward`
  overrides that, and is documented as the operator's authorization — never
  something message text can grant.

Every result carrying message text is labeled untrusted, in both the text and
the structured half. The honest gap is paraphrase: a sentence written from
memory carries nothing tying it back to its source, and no textual rule finds
it. `docs/scope.md` states what each defense catches and what it does not.

## Requirements

- macOS, with Messages signed in
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Full Disk Access for the Python interpreter, to read `~/Library/Messages/chat.db`

Contacts needs no second permission: the address book is read as a sqlite file,
which Full Disk Access already covers.

Sending needs one more grant — Apple Events control of Messages.app — which
macOS prompts for the first time a message is sent. Trigger it deliberately
while sitting at the machine rather than letting it ambush a remote client
months later.

## Running

```bash
uv sync
uv run imessage-mcp
```

That is stdio. For HTTP, set the transport and bind to localhost behind a
tunnel or reverse proxy:

```bash
IMESSAGE_MCP_TRANSPORT=http \
IMESSAGE_MCP_AUTH=password \
IMESSAGE_MCP_PASSWORD=... \
IMESSAGE_MCP_BASE_URL=https://imessage.example.com \
uv run imessage-mcp
```

Auth applies to the HTTP transport only; stdio takes its security from the fact
that running it means already having a shell on the machine. There is no
version of this server that should be exposed to a network without
`IMESSAGE_MCP_AUTH=password` — it reads a personal message archive.

Every setting is documented in `.env.example`: `TRANSPORT`, `HOST`, `PORT`,
`STATELESS`, `AUTH`, `PASSWORD`, `BASE_URL`, `STATE_DIR`, `DB_PATH`, all under
the `IMESSAGE_MCP_` prefix.

`GET /health` is unauthenticated by design so an uptime monitor can poll it. It
reports the resolved interpreter path — a Python patch upgrade silently moves
the binary and voids the Full Disk Access grant, which is the documented way
this service dies — along with the newest message in the database, whether
Messages is running, and the outcome of the last send. It returns 503, with
the problems named in `status`, when the database is unreachable, Messages is
not running, the last send failed, or the interpreter has moved.

## Deployment

`docs/deployment-macos.md` covers running it as a LaunchAgent: the plist, the
privacy prompt that hangs the service if the grant is missing, why it must not
be a LaunchDaemon, monitoring, and pushing an update.

`scripts/self-update.sh` supports that.

## Development

```bash
uv sync --extra test
uv run pytest
```

The test suite is offline and runs against a synthetic database built from
Apple's schema, including synthesized typedstream blobs, so the real decode path
is exercised without committing anybody's messages. It never opens the real
database, so it works on a machine that has never sent an iMessage.

This repository is public and hosts a server for a personal archive, so nothing
identifying goes in it — no hostname, no real phone number, no chat GUID.
`scripts/scan-secrets.sh` checks tracked files for the usual shapes, and CI runs it
alongside the tests. `CLAUDE.md` has the placeholder conventions.

## License

MIT
