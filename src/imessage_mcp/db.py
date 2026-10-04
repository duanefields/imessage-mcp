"""Read-only queries against the iMessage database.

Everything here opens the database read-only and never writes. Sending goes
through Messages.app, not through this file -- writing to Apple's schema is not
a recoverable mistake.
"""

import datetime
import os
import pathlib
import sqlite3

from .attributed import message_text

DEFAULT_DB_PATH = pathlib.Path.home() / "Library" / "Messages" / "chat.db"

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# The `message` table holds more than conversation messages. On a real database
# 3,240 rows were tapbacks (associated_message_type != 0) and 891 were system
# events such as group renames (item_type != 0). Both render as messages if they
# are not excluded, so every listing query filters on this.
CONVERSATION_ONLY = (
    "m.item_type = 0 AND m.associated_message_type = 0 AND m.is_system_message = 0"
)

# chat.is_filtered says which list Messages shows a conversation in: 0 the main
# list, 1 Unknown Senders, 2 Junk. Junk is left out of the default views, as it
# is in Messages; Unknown Senders is kept, but labeled, because on a real
# database it held 710 of 1,197 conversations, many of them wanted.
NOT_JUNK = "c.is_filtered IS NOT 2"
_FILTER_LABELS = {1: "unknown sender", 2: "junk"}

_MESSAGE_COLUMNS = """
    m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.is_from_me, m.date,
    m.is_read, m.cache_has_attachments, m.service, m.thread_originator_guid,
    h.id AS handle
"""


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open the database read-only.

    Read-only genuinely works against a live database, WAL and all, so there is
    no need to copy it first. It was verified returning rows written seconds
    earlier.
    """
    if path is not None:
        target = pathlib.Path(path)
    else:
        # Read at call time, not import time, so a launcher or a test can set it
        # after the module is loaded.
        target = pathlib.Path(os.environ.get("IMESSAGE_MCP_DB_PATH") or DEFAULT_DB_PATH)
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def to_iso(apple_ns: int | None) -> str | None:
    """Convert Apple's nanoseconds-since-2001 timestamp to an ISO 8601 string.

    Every row on the reference database used the nanosecond form, including
    messages from 2014 -- Apple migrated the old second-precision values -- so
    there is deliberately no dual-scale handling here.
    """
    if not apple_ns:
        return None
    seconds = apple_ns / 1_000_000_000
    return (APPLE_EPOCH + datetime.timedelta(seconds=seconds)).isoformat()


def now_apple_ns() -> int:
    """The current time in Apple's nanoseconds-since-2001 encoding."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return int((now - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def find_outgoing(
    conn: sqlite3.Connection, chat_guid: str, text: str, since_ns: int
) -> dict | None:
    """Find a message we sent to ``chat_guid`` since ``since_ns`` matching ``text``.

    Used to confirm a send actually happened. AppleScript's ``send`` is
    fire-and-forget: it reports success for handing the message to Messages, not
    for Messages doing anything with it. Reading the message back out of the
    database is the difference between "we asked" and "it exists".
    """
    rows = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE c.guid = ? AND m.is_from_me = 1 AND m.date >= ?
           AND {CONVERSATION_ONLY}
         ORDER BY m.date DESC
         LIMIT 20
        """,
        (chat_guid, since_ns),
    ).fetchall()
    for row in rows:
        if message_text(row["text"], row["attributedBody"]) == text:
            return _message_row(row)
    return None


def _message_row(row: sqlite3.Row) -> dict:
    return {
        "guid": row["guid"],
        "text": message_text(row["text"], row["attributedBody"]),
        "is_from_me": bool(row["is_from_me"]),
        "date": to_iso(row["date"]),
        "is_read": bool(row["is_read"]),
        "has_attachments": bool(row["cache_has_attachments"]),
        "service": row["service"],
        "handle": row["handle"],
        # Just the guid until add_reply_context fills in who said what.
        "reply_to": {"guid": row["thread_originator_guid"]}
        if row["thread_originator_guid"]
        else None,
    }


def add_reply_context(conn: sqlite3.Connection, messages: list[dict]) -> None:
    """Fill in the message each reply in ``messages`` was replying to, in place.

    A reply in a thread names the message it answers by guid, and that message
    is usually not on the same page -- it can be days older. Without it a reply
    reads as a bare "yes" with nothing to say what was agreed to.
    """
    wanted = {m["reply_to"]["guid"] for m in messages if m.get("reply_to")}
    if not wanted:
        return
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"""
        SELECT m.guid, m.text, m.attributedBody, m.is_from_me,
               m.cache_has_attachments, h.id AS handle
          FROM message m
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE m.guid IN ({placeholders})
        """,
        list(wanted),
    ).fetchall()
    found = {
        row["guid"]: {
            "guid": row["guid"],
            "text": message_text(row["text"], row["attributedBody"]),
            "is_from_me": bool(row["is_from_me"]),
            "has_attachments": bool(row["cache_has_attachments"]),
            "handle": row["handle"],
        }
        for row in rows
    }
    for message in messages:
        if message.get("reply_to"):
            message["reply_to"] = found.get(
                message["reply_to"]["guid"], message["reply_to"]
            )


# A reaction is its own row in the message table. associated_message_type 2000
# to 2007 adds one, 3000 to 3007 takes the same kind back off, and 1000 is a
# sticker placed on a message by an older client.
_REACTION_KINDS = {
    0: "loved",
    1: "liked",
    2: "disliked",
    3: "laughed",
    4: "emphasized",
    5: "questioned",
    6: "emoji",
    7: "sticker",
}


def _reaction_target(associated_guid: str) -> str:
    """The guid a reaction points at, without its message-part prefix.

    The prefix says which part of the message was reacted to: ``p:0/`` for the
    first part of an ordinary message, ``p:2/`` for its third, ``bp:`` for an
    app balloon. Some rows carry the bare guid.
    """
    if associated_guid.startswith("bp:"):
        return associated_guid[3:]
    return associated_guid.rpartition("/")[2]


def add_reactions(
    conn: sqlite3.Connection, chat_guid: str, messages: list[dict]
) -> None:
    """Set ``reactions`` on each of ``messages``, in place.

    Messages keeps only the latest state per person and kind: a removal usually
    deletes the row it cancels, but not always, so the newest row for each
    sender and kind decides whether that reaction stands. Which part of a
    multi-part message was reacted to is not kept.
    """
    wanted = {m["guid"] for m in messages}
    rows = conn.execute(
        """
        SELECT m.associated_message_guid, m.associated_message_type,
               m.associated_message_emoji, m.is_from_me, h.id AS handle
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE c.guid = ?
           AND (m.associated_message_type = 1000
                OR m.associated_message_type BETWEEN 2000 AND 3007)
         ORDER BY m.date
        """,
        (chat_guid,),
    ).fetchall()

    latest: dict[tuple, sqlite3.Row] = {}
    for row in rows:
        target = _reaction_target(row["associated_message_guid"] or "")
        if target not in wanted:
            continue
        code = row["associated_message_type"]
        kind = 7 if code == 1000 else code % 1000
        sender = (row["is_from_me"], row["handle"])
        latest[(target, sender, kind, row["associated_message_emoji"])] = row

    reactions: dict[str, list[dict]] = {guid: [] for guid in wanted}
    for (target, _, kind, emoji), row in latest.items():
        if row["associated_message_type"] >= 3000:
            continue
        reactions[target].append(
            {
                "reaction": _REACTION_KINDS[kind],
                "emoji": emoji,
                "is_from_me": bool(row["is_from_me"]),
                "handle": None if row["is_from_me"] else row["handle"],
            }
        )
    for message in messages:
        message["reactions"] = reactions[message["guid"]]


# group_concat needs a separator that cannot occur in a handle. Unit separator
# is the same choice dav-mcp makes in its event ids, for the same reason.
_HANDLE_SEPARATOR = "\x1f"


def chat_identities(conn: sqlite3.Connection) -> list[dict]:
    """Just enough of every chat to match it against a name or handle.

    Deliberately cheap: no per-chat subqueries, no message decoding. Matching
    has to consider every conversation, not just recent ones -- the whole point
    is finding somebody you have not spoken to lately -- so the query that runs
    over all of them must stay small. The expensive detail query then runs only
    for the ones that matched.
    """
    rows = conn.execute(
        f"""
        SELECT c.guid, c.chat_identifier, c.display_name,
               group_concat(h.id, '{_HANDLE_SEPARATOR}') AS handles
          FROM chat c
          LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
          LEFT JOIN handle h ON h.ROWID = chj.handle_id
         GROUP BY c.ROWID
        """
    ).fetchall()
    return [
        {
            "chat_guid": row["guid"],
            "chat_identifier": row["chat_identifier"],
            "display_name": row["display_name"],
            "handles": (row["handles"] or "").split(_HANDLE_SEPARATOR)
            if row["handles"]
            else [],
        }
        for row in rows
    ]


def list_chats(
    conn: sqlite3.Connection,
    limit: int = 20,
    offset: int = 0,
    guids: list[str] | None = None,
) -> list[dict]:
    """Conversations in order of most recent activity.

    ``guids`` restricts the result to those conversations, keeping the same
    ordering. An empty list means nothing matched and returns nothing, which is
    different from ``None`` meaning every conversation outside Junk. Junk is
    reachable only by asking for it by guid, the way a contact search does.
    """
    if guids is not None and not guids:
        return []

    restrict = f"WHERE {NOT_JUNK}"
    params: list = []
    if guids is not None:
        restrict = f"WHERE c.guid IN ({','.join('?' * len(guids))})"
        params.extend(guids)
    params.extend([limit, offset])

    rows = conn.execute(
        f"""
        SELECT c.guid, c.chat_identifier, c.display_name, c.service_name,
               c.is_filtered, MAX(j.message_date) AS last_date,
               (SELECT COUNT(*)
                  FROM message m
                  JOIN chat_message_join j2 ON j2.message_id = m.ROWID
                 WHERE j2.chat_id = c.ROWID
                   AND m.is_read = 0 AND m.is_from_me = 0 AND m.is_finished = 1
                   AND {CONVERSATION_ONLY}) AS unread_count,
               (SELECT m.ROWID
                  FROM message m
                  JOIN chat_message_join j3 ON j3.message_id = m.ROWID
                 WHERE j3.chat_id = c.ROWID AND {CONVERSATION_ONLY}
                 ORDER BY m.date DESC LIMIT 1) AS last_message_id
          FROM chat c
          JOIN chat_message_join j ON j.chat_id = c.ROWID
         {restrict}
         GROUP BY c.ROWID
         ORDER BY last_date DESC
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()

    previews = _previews(conn, [r["last_message_id"] for r in rows])

    return [
        {
            "chat_guid": row["guid"],
            "chat_identifier": row["chat_identifier"],
            "display_name": row["display_name"],
            "service": row["service_name"],
            "last_activity": to_iso(row["last_date"]),
            "unread_count": row["unread_count"],
            "last_message": previews.get(row["last_message_id"]),
            "filtered": _FILTER_LABELS.get(row["is_filtered"]),
        }
        for row in rows
    ]


def _previews(conn: sqlite3.Connection, message_ids: list[int | None]) -> dict[int, str]:
    """Decode the last message of each chat, in one query rather than per chat."""
    wanted = [i for i in message_ids if i is not None]
    if not wanted:
        return {}
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT ROWID, text, attributedBody FROM message WHERE ROWID IN ({placeholders})",
        wanted,
    ).fetchall()
    return {
        row["ROWID"]: message_text(row["text"], row["attributedBody"]) for row in rows
    }


def chat_exists(conn: sqlite3.Connection, chat_guid: str) -> bool:
    """Whether a chat with this guid exists.

    Lets a tool tell "no such conversation" apart from "that conversation is
    empty", which are the same empty list otherwise.
    """
    row = conn.execute(
        "SELECT 1 FROM chat WHERE guid = ? LIMIT 1", (chat_guid,)
    ).fetchone()
    return row is not None


def count_chats(
    conn: sqlite3.Connection, guids: list[str] | None = None
) -> int:
    """Chats that have at least one message, matching what list_chats returns."""
    if guids is not None and not guids:
        return 0
    restrict = f"WHERE {NOT_JUNK}"
    params: list = []
    if guids is not None:
        restrict = f"WHERE c.guid IN ({','.join('?' * len(guids))})"
        params.extend(guids)
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT c.ROWID) AS n
          FROM chat c
          JOIN chat_message_join j ON j.chat_id = c.ROWID
         {restrict}
        """,
        params,
    ).fetchone()
    return row["n"]


# Messages hides attachments it keeps for its own use, chiefly the
# `.pluginPayloadAttachment` data behind a link preview. They are not anything
# anybody sent, and on a real database they were 6,526 of 14,101 rows.
VISIBLE_ATTACHMENT = "a.hide_attachment = 0"


def count_attachments(conn: sqlite3.Connection, chat_guid: str | None = None) -> int:
    where = f"WHERE {VISIBLE_ATTACHMENT}"
    params = []
    if chat_guid:
        where += " AND c.guid = ?"
        params.append(chat_guid)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM attachment a
          JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
          JOIN message m ON m.ROWID = maj.message_id
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
         {where}
        """,
        params,
    ).fetchone()
    return row["n"]


def count_messages(conn: sqlite3.Connection, chat_guid: str) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
         WHERE c.guid = ? AND {CONVERSATION_ONLY}
        """,
        (chat_guid,),
    ).fetchone()
    return row["n"]


def get_messages(
    conn: sqlite3.Connection, chat_guid: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Messages in one chat, newest first, with their reactions and reply context."""
    rows = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE c.guid = ? AND {CONVERSATION_ONLY}
         ORDER BY m.date DESC
         LIMIT ? OFFSET ?
        """,
        (chat_guid, limit, offset),
    ).fetchall()
    messages = [_message_row(row) for row in rows]
    add_reactions(conn, chat_guid, messages)
    add_reply_context(conn, messages)
    return messages


def get_participants(conn: sqlite3.Connection, chat_guid: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT h.id AS handle, h.service
          FROM chat_handle_join chj
          JOIN handle h ON h.ROWID = chj.handle_id
          JOIN chat c ON c.ROWID = chj.chat_id
         WHERE c.guid = ?
         ORDER BY h.id
        """,
        (chat_guid,),
    ).fetchall()
    return [{"handle": row["handle"], "service": row["service"]} for row in rows]


# Apple's own conditions, copied from the index it keeps for exactly this
# query -- unread means received, finished, and not a system row.
UNREAD_ONLY = (
    f"m.is_read = 0 AND m.is_from_me = 0 AND m.is_finished = 1 AND {CONVERSATION_ONLY}"
)


def count_unread(conn: sqlite3.Connection) -> int:
    """Every unread message outside Junk, not just the ones a limit would return."""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
         WHERE {UNREAD_ONLY} AND {NOT_JUNK}
        """
    ).fetchone()
    return row["n"]


def get_unread(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Received messages outside Junk that have not been read, newest first."""
    rows = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}, c.guid AS chat_guid, c.display_name,
               c.is_filtered
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE {UNREAD_ONLY} AND {NOT_JUNK}
         ORDER BY m.date DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    unread = [
        {
            **_message_row(row),
            "chat_guid": row["chat_guid"],
            "display_name": row["display_name"],
            "filtered": _FILTER_LABELS.get(row["is_filtered"]),
        }
        for row in rows
    ]
    add_reply_context(conn, unread)
    return unread


def get_attachments(
    conn: sqlite3.Connection,
    chat_guid: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Attachment metadata. The files themselves are deliberately not read."""
    where = f"WHERE {VISIBLE_ATTACHMENT}"
    params: list = []
    if chat_guid:
        where += " AND c.guid = ?"
        params.append(chat_guid)
    params += [limit, offset]
    rows = conn.execute(
        f"""
        SELECT a.guid, a.transfer_name, a.mime_type, a.uti, a.total_bytes,
               a.is_outgoing, m.date, c.guid AS chat_guid
          FROM attachment a
          JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
          JOIN message m ON m.ROWID = maj.message_id
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
         {where}
         ORDER BY m.date DESC
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [
        {
            "attachment_guid": row["guid"],
            "name": row["transfer_name"],
            "mime_type": row["mime_type"],
            "uti": row["uti"],
            "size_bytes": row["total_bytes"],
            "is_outgoing": bool(row["is_outgoing"]),
            "date": to_iso(row["date"]),
            "chat_guid": row["chat_guid"],
        }
        for row in rows
    ]


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    offset: int = 0,
    chat_guid: str | None = None,
) -> tuple[list[dict], int]:
    """Substring search across message bodies. Returns ``(page, total_matches)``.

    The text lives in a binary blob, so this cannot be pushed into SQL -- rows
    have to be decoded before they can be matched. That is affordable: decoding
    and matching all 123,023 messages of the reference database took 2.77
    seconds. The whole history is therefore scanned and ``total`` is a true
    count, with only the returned page bounded. Capping the scan instead would
    make "no results" mean "not in the last few months", which reads exactly
    like "not there".
    """
    needle = query.casefold()
    where = f"WHERE {CONVERSATION_ONLY}"
    params: list = []
    if chat_guid:
        where += " AND c.guid = ?"
        params.append(chat_guid)

    cursor = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}, c.guid AS chat_guid, c.display_name
          FROM message m
          JOIN chat_message_join j ON j.message_id = m.ROWID
          JOIN chat c ON c.ROWID = j.chat_id
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         {where}
         ORDER BY m.date DESC
        """,
        params,
    )

    total = 0
    page: list[dict] = []
    for row in cursor:
        body = message_text(row["text"], row["attributedBody"])
        if not body or needle not in body.casefold():
            continue
        total += 1
        if total > offset and len(page) < limit:
            page.append(
                {
                    **_message_row(row),
                    "chat_guid": row["chat_guid"],
                    "display_name": row["display_name"],
                }
            )
    add_reply_context(conn, page)
    return page, total
