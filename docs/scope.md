# iMessage MCP — Scope

An MCP server that reads the local iMessage database and sends messages through Messages.app,
hosted over HTTP from a Mac that stays logged in, so it is reachable from phone and tablet.

It replaces the Anthropic-provided iMessage extension for Claude Desktop. That extension works
well and is the UX bar to clear, but it only runs on the desktop, which is exactly the wrong
place — the messages you want to act on arrive while you are away from it.

Same architecture as [things-mcp](https://github.com/duanefields/things-mcp/tree/production) and
[dav-mcp](https://github.com/duanefields/dav-mcp): FastMCP over stdio *and* HTTP, a
password-guarded OAuth 2.1 layer for the HTTP transport, running as a LaunchAgent behind a
tunnel. The auth layer has already been ported once (things-mcp to dav-mcp), so it moves again
unchanged apart from the scope name and the env prefix.

[daveremy/imessage-mcp](https://github.com/daveremy/imessage-mcp) is the reference implementation
for the read layer — Node/TypeScript, six tools. We are not forking it; we are reimplementing in
Python to match the other two servers, and taking its tool surface as a starting point.

---

## Verified against the real database

Every line below was measured on a live Mac running macOS 26.4.1, not assumed from documentation.

| Question                       | Finding                                                            |
| :----------------------------- | :----------------------------------------------------------------- |
| Database scale                 | 123,023 messages · 1,247 chats · 1,453 handles · 13,784 attachments |
| Is `message.text` usable?      | **No.** Empty on 121,793 of 123,023 rows (~99%)                     |
| Does typedstream decoding work | 3,998 of 4,000 sampled rows decoded, zero errors                    |
| Read-only access               | `file:…/chat.db?mode=ro` works and reads WAL-fresh rows             |
| Can Messages.app still send?   | Yes — `send … to (participant \| chat)` is in the sdef              |
| Contact source                 | `AddressBook-v22.abcddb`, iCloud source holds 917 records           |
| Full-archive search cost       | 2.77s to fetch, decode and match all 123,023 messages              |
| Retention setting              | Forever (`KeepMessageForDays = 0`) — 11.8 years, nothing pruned     |

### The text column is a trap

Nearly every message on a modern macOS stores its body in the `attributedBody` BLOB — an
NeXTSTEP typedstream archive — and leaves `text` NULL. Any implementation that reads `text` will
appear to work on a handful of old rows and return nothing for everything else.

`pytypedstream` (pure Python, no compilation, so it adds nothing to the deployment story) decoded
3,998 of a 4,000-row sample spread across the full history with zero exceptions. The two misses
were empty, almost certainly attachment-only or sticker rows. Treat "decoded to empty" as a
normal outcome to be rendered as a placeholder, not as a parse failure.

Fall back to `text` when the blob is absent. Do not do it the other way around.

### Read-only access is genuinely read-only

Opening with `mode=ro` returned rows written three minutes earlier, so the WAL is visible and no
copy-the-database step is needed. This works because the files are owned by the logged-in user.
Never open the database writable — a stray write to Apple's schema is not a recoverable mistake.

---

## Permissions: one grant, not two

Reading `chat.db` needs Full Disk Access. So does reading the Things database, so this is a
solved problem — `docs/deployment-macos.md` in things-mcp documents the whole trap and ports over
almost unchanged. The short version, because it costs an afternoon otherwise:

- It must be a LaunchAgent, not a LaunchDaemon. Sending needs a logged-in GUI session.
- Grant FDA to the *resolved* interpreter path, before first launch. Without it the service hangs
  forever inside `open()` on a consent dialog nobody is there to answer — live PID, empty log,
  nothing bound to the port.
- Re-grant after any Python patch upgrade, which silently moves the binary and voids the grant.

Contacts is a **separate** TCC permission, and we deliberately do not need it. Verified: a shell
holding FDA but with no `kTCCServiceAddressBook` grant read 917 records straight out of the
AddressBook sqlite file. Contacts permission is only required when going through the Contacts
*API* — `CNContactStore`, or scripting Contacts.app — which is what the Node reference
implementation and the Anthropic desktop extension both do. Reading the file is plain file
access, and FDA already covers it.

This matters more than it sounds. The Automation and Contacts prompts cannot be pre-granted from
System Settings, so on an unattended host they can only be answered by someone walking over to
the machine. Keeping the count at one is a deployment property, not a purity argument.

Sending is a third grant — Apple Events control of Messages.app — and that one is unavoidable.
Trigger it deliberately while sitting at the machine rather than letting it ambush a remote
client months later.

---

## Tool surface

Six read tools and one write tool. Parity with the reference implementation, plus unread, which
is the tool that justifies the whole project on a phone.

| Tool               | Notes                                                                |
| :----------------- | :------------------------------------------------------------------- |
| `list_chats`       | Recent conversations by activity, with resolved names                |
| `get_messages`     | One chat, paginated, newest-first                                    |
| `search_messages`  | Case-insensitive across chats — see the search note below            |
| `get_participants` | Chat members, resolved names, service (iMessage/SMS/RCS)             |
| `get_attachments`  | Metadata only: filename, MIME type, size, direction                  |
| `get_unread`       | What arrived while you were away — the mobile use case               |
| `send_message`     | Existing chats only, addressed by GUID                               |

Reads return a FastMCP `ToolResult` carrying both a text channel and `structured_content`, with
`limit`/`offset` and an `{items, count, total, offset, limit}` envelope, exactly as the other two
servers do. Errors return an error result, never a raised exception — a raised exception reaches
the model as an opaque failure it cannot act on.

Search cannot be pushed into SQL. The text lives in a binary blob, so `WHERE text LIKE ?` matches
almost nothing — rows have to be fetched and decoded before matching. The reference implementation
responds to this by capping search at the 5,000 most recent messages. **Do not copy that cap.**

Measured on the real database: a full-archive search — fetch, decode and match all 123,023
messages — takes **2.77 seconds**, about 44,000 messages per second end to end. The cap is
guarding a cost that does not exist. It is also far smaller than it sounds: 5,000 messages is
**129 days** of this account's traffic, so a capped search silently answers "nothing found" for
anything older than four months.

Cap the *results*, not the *scan*. Return the top N hits with a true total count. Completeness is
cheap; tokens are not.

Default to a recent window for latency, scan the whole archive when asked, and state in the
result which of the two happened. The 2.77s figure came off a warm page cache, so a cold run will
be slower — the window default covers that case as well.

Deferred to a later pass, once the read surface has been used in anger: tapbacks
(`associated_message_type`), edits and retractions (`date_edited`, `date_retracted`), threaded
replies (`thread_originator_guid`), and group rename events.

---

## Sending

Existing chats only, addressed by chat GUID. Starting new conversations comes later.

The reasoning is that sending is irrevocable. There is no local outbox to intercept and no way to
recall a message — the same hazard as the iMIP mail in dav-mcp, and it deserves the same care.
Restricting to an existing thread means a hallucinated or mistyped phone number cannot reach a
stranger, because the thread it would need already has to exist. It also covers essentially every
real use case: replying to something that just came in.

Two implementation notes:

- `buddy` no longer exists in the Messages scripting dictionary. Nearly every AppleScript snippet
  on the web is written against it and will fail on a current macOS. The surviving form is
  `send <text> to <participant | chat>`.
- Invoke `osascript` with an argv list, never through a shell, and escape the message text into
  the script literal. The same rule the Things area tools follow.

---

## Contacts

Read the AddressBook sqlite directly, merging across `Sources/*/AddressBook-v22.abcddb`. There
are four sources on the target machine; one holds 917 records and another 100, and the rest are
empty stubs.

The alternative was to call dav-mcp over CardDAV and reuse the vCard code already written. That
was rejected: it would put iCloud credentials and a network round-trip into every chat listing,
and iCloud's throttling behavior — documented in dav-mcp, where a burst of writes gets the whole
account 503'd for a while, reads included — would turn into an iMessage outage. A local sqlite
read has no such failure mode.

Handle normalization is the real work here, not the query. Handles arrive as `+15125550100`,
`5125550100`, `(512) 555-0100` and as email addresses, and the reference implementation's known
weakness is that its normalization is US-centric. Normalize to E.164 and match on the last
several digits.

---

## Public repository hygiene

This repo is public. Nothing identifying goes in it — not the host machine's name, not a real
phone number, not an email address, not a chat GUID from the real database.

That constrains testing more than it sounds like it does. The test suite must run entirely
offline against a **synthetic** chat.db fixture built by a script in the repo, with invented
handles in the `555-01xx` reserved range. No test may open the real database. Build the fixture
generator first, not last — it is what makes everything downstream testable.

Real `attributedBody` blobs cannot be committed as fixtures either, since they contain real
message text. The generator has to synthesize typedstream blobs, or the decode tests have to work
from blobs constructed in code.

The deployment doc refers to the host as "the host Mac" throughout. No hostname.

---

## Layout

```text
src/imessage_mcp/
  server.py       tools, /health, transport selection in main()
  db.py           read-only sqlite connection and queries
  attributed.py   typedstream -> text, with text-column fallback
  contacts.py     AddressBook sources, handle normalization, resolution cache
  applescript.py  osascript send
  formatters.py   rows -> human-readable text
  auth.py         password-guarded OAuth 2.1, copied from things-mcp
```

Env prefix `IMESSAGE_MCP_`, mirroring the other two servers: `TRANSPORT`, `HOST`, `PORT`,
`STATELESS`, `AUTH`, `PASSWORD`, `BASE_URL`, `STATE_DIR`.

`stateless_http` defaults to true. A remote client dials from a pool of addresses, and a request
arriving from a different address than the one that opened the session is rejected with a 400
that wedges the connection. Nothing here needs session state.

Transport env vars are read inside `main()`, not at import time, so launchd and the tests can set
them after import.

`scripts/healthcheck.sh` and `scripts/self-update.sh` copy over from things-mcp. `/health` should
report the resolved interpreter path, since an interpreter upgrade voiding the FDA grant is the
documented way this service dies.

---

## Build order

Sending is held back until the read surface has been proven end to end through a real client.
That ordering is deliberate: it means the first version exposed to the network cannot do
anything irreversible, and the whole pipeline -- permissions, tunnel, auth, a client actually
talking to it -- gets proven by a surface where a bug costs nothing.

### Milestone 1: read-only, working through Claude

1. Fixture generator and a synthetic chat.db -- verify: tests open it and count rows. **Done.**
2. `attributed.py` -- verify: decodes synthetic blobs; spot-checked by hand against the real
   database, never committed. **Done.**
3. `db.py` queries and `formatters.py` -- verify: unit tests over the fixture. **Done.**
4. `contacts.py` -- verify: normalization tests over invented numbers in several shapes. **Done.**
5. Read tools -- verify: exercised through an MCP client, not by calling the functions. **Done.**
6. Connect over stdio and use it from Claude against the real database.
7. `auth.py` and the HTTP transport -- verify: `/health` responds, a client completes the OAuth
   flow.
8. LaunchAgent, tunnel, healthcheck, self-update -- verify: reachable from the phone; survives a
   reboot of the host.

Milestone 1 ends with the thing that justifies the project: reading messages from a phone,
with no way to write anything.

### Milestone 2: sending

9. `applescript.py` and `send_message`, restricted to existing chats -- verify: send one message
   to yourself, from the machine, before it is ever reachable remotely.

### Later

New conversations, and everything under Deferred work below.

Steps 1 through 5 need no permissions and no network, so they are the part that can be built
anywhere.

## Deferred work

Nothing here is undecided — each item is a deliberate "not now" with the condition that should
bring it back.

### Full-text search index

**Trigger: when search needs to be _better_, not faster.** Substring matching is the ceiling
today. Ranked relevance ("what did we decide about the trip"), fuzzy matching, or semantic search
all need a decoded, indexed copy. Latency is *not* a trigger — that is already solved.

Rejected now because `chat.db` is already the permanent archive. Retention on this account is
Forever (`KeepMessageForDays = 0`), 11.8 years deep with nothing pruned, so an archive database
would be a second complete copy of the same data, maintained by an internet-reachable process, to
amortize a 2.3-second decode. It would also have to answer an awkward question: when a message is
retracted in Messages, does our copy keep it?

When it is built, it should be an **index, not a system of record** — `chat.db` stays the source
of truth, and the index is disposable.

```text
messages_fts (FTS5)   guid UNINDEXED, decoded_text
meta                  key, value        -- schema version, last build time
```

**Build it by full rebuild, not incremental sync.** This is the part worth remembering: decoding
all 123,023 messages takes 2.3 seconds, so a complete rebuild is cheap enough to run on a timer
or on demand. That removes the entire hard half of the design — no ROWID watermark, no dedup, no
invalidation when a message is edited (`date_edited`), retracted (`date_retracted`) or deleted.
Those are the cases that make incremental indexing subtly wrong, and a rebuild sidesteps all of
them by construction. Do not build the clever version.

Baseline to compare against, measured on the real database: full-archive substring search is
2.77s end to end, ~44,000 messages/sec, warm cache.

### Attachment contents

**Trigger: an explicit decision to let image bytes leave the machine.** Metadata only for now —
filename, MIME type, size, direction. Reading the files means handing message content to a remote
model, which is a bigger call than it looks and should be made on purpose rather than as a side
effect of a convenience feature.

### Starting new conversations

**Trigger: enough real use of `send_message` to trust it.** Deferred, not rejected. Sending into
an existing thread cannot reach a stranger; addressing a new conversation by phone number can,
and it cannot be undone.
