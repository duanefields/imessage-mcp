# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## This repository is public

It is a server for reading a personal message archive, so the ordinary rules
about secrets are not enough. Assume every file here is world-readable forever,
including the git history, where a mistake cannot be quietly deleted later.

**Never commit any of the following, in code, tests, fixtures, comments, docs,
commit messages, or example output:**

| Category           | Examples                                                             |
| :----------------- | :------------------------------------------------------------------- |
| Network identity   | Hostnames, machine names, subdomains, IP addresses, tunnel URLs      |
| Credentials        | Passwords, API keys, tokens, OAuth client secrets, certificates      |
| Personal handles   | Real phone numbers, email addresses, Apple IDs                       |
| Message content    | Real message text, decoded or raw `attributedBody` blobs             |
| Identifiers        | Real chat GUIDs, message GUIDs, contact names, `ROWID` values         |
| Filesystem         | Real attachment paths, absolute paths containing a username          |

Use these instead:

- Phone numbers: the `555-01xx` range, which is reserved for fiction.
- Email addresses: `@example.com`.
- Hosts: `imessage.example.com`, or "the host Mac" in prose. **Never the real
  machine's name**, even in a deployment doc where it would be convenient.
- Paths: `/Users/USERNAME/...`.
- GUIDs: an obvious synthetic form such as `SYNTHETIC-0001`.

### Local notes are the escape hatch

Deployment genuinely needs real values -- the host's name, the tunnel URL, the
port. Those go in `docs/local/`, which is gitignored. Keep them there and keep
them out of tracked files, rather than inventing awkward workarounds in the
committed docs.

Anything tracked refers to the machine as "the host Mac" and uses
`imessage.example.com`.

### What is safe

Apple's schema DDL is safe and is checked in at `tests/support/chat_schema.sql`.
It is `CREATE TABLE` and `CREATE INDEX` statements captured from a real
database, and contains no rows. Column names are not private information.

### Tests must never touch the real database

No test may open `~/Library/Messages/chat.db` or the AddressBook, not even
read-only and not even skipped-by-default. The suite runs entirely against the
synthetic fixture in `tests/support/synthetic_db.py`, so it works on a machine
that has never sent an iMessage and cannot leak anything if it fails loudly in
CI output.

Spot checks against the real database are done by hand, from a scratch
directory, and the results are never pasted into the repository — including into
a commit message or a test name.

### Before committing

```bash
git diff --cached | grep -nEi \
  '\+1[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}|@(gmail|icloud|me)\.|/Users/[a-z]'
```

A hit is not automatically a problem -- `555-01xx` numbers and
`/Users/USERNAME` are fine -- but every hit needs a look before the commit
lands.

## Commands

```bash
uv sync --extra test
uv run pytest
uv run pytest -k typedstream
```

The suite is offline and touches no real data.

## Architecture

See `docs/scope.md` for the full design, what was measured to justify it, and
what is deliberately deferred.

- `src/imessage_mcp/server.py` -- the tools. Read-only; sending is a later milestone.
- `src/imessage_mcp/attributed.py` -- recovers message text from the
  `attributedBody` typedstream blob. The `text` column is empty on ~99% of real
  messages, so this is the primary path, not a fallback.
- `tests/support/typedstream_writer.py` -- writes blobs of the shape Messages
  produces, so fixtures exercise the real decode path. `pytypedstream` only
  reads.
