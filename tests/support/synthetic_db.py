"""Build a synthetic ``chat.db`` for the tests.

The real database cannot be used: this is a public repository and the tests must
run anywhere, on a machine that has never seen an iMessage. So the fixture is
generated from Apple's own schema (``chat_schema.sql``) and filled with invented
handles in the 555-01xx range reserved for fiction.

Building on the real schema rather than a hand-written subset means a query that
names a column wrong fails here, rather than passing against a convenient
approximation and failing on the real database.
"""

import datetime
import pathlib
import plistlib
import sqlite3

from .typedstream_writer import attributed_body

SCHEMA = pathlib.Path(__file__).with_name("chat_schema.sql")

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# A fixed point in time, so every run produces the same database.
BASE = datetime.datetime(2026, 3, 1, 17, 0, tzinfo=datetime.timezone.utc)

ALICE = "+15125550101"
BOB = "+15125550102"
CAROL = "+15125550103"
DANA_EMAIL = "dana@example.com"
# Not in any address book: one lands in Unknown Senders, the other in Junk.
STRANGER = "+15125550104"
SPAMMER = "+15125550105"

GROUP_NAME = "Game Night"


def apple_time(when: datetime.datetime) -> int:
    """Convert a datetime to Apple's nanoseconds-since-2001 encoding."""
    return int((when - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def _minutes(n: int) -> datetime.datetime:
    return BASE + datetime.timedelta(minutes=n)


def build(path: str | pathlib.Path) -> sqlite3.Connection:
    """Create a synthetic chat.db at ``path`` and return an open connection."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA.read_text())

    handles = [
        (1, ALICE, "iMessage"),
        (2, BOB, "iMessage"),
        (3, CAROL, "SMS"),
        (4, DANA_EMAIL, "iMessage"),
        (5, STRANGER, "SMS"),
        (6, SPAMMER, "SMS"),
    ]
    conn.executemany(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)", handles
    )

    # The last column is is_filtered: 0 the main list, 1 Unknown Senders, 2 Junk.
    chats = [
        (1, f"iMessage;-;{ALICE}", ALICE, "iMessage", None, 0),
        (2, f"iMessage;-;{BOB}", BOB, "iMessage", None, 0),
        (3, "iMessage;+;chat999", "chat999", "iMessage", GROUP_NAME, 0),
        (4, f"iMessage;-;{DANA_EMAIL}", DANA_EMAIL, "iMessage", None, 0),
        (5, f"SMS;-;{STRANGER}", STRANGER, "SMS", None, 1),
        (6, f"SMS;-;{SPAMMER}", SPAMMER, "SMS", None, 2),
    ]
    conn.executemany(
        "INSERT INTO chat (ROWID, guid, chat_identifier, service_name, display_name,"
        " is_filtered) VALUES (?, ?, ?, ?, ?, ?)",
        chats,
    )

    conn.executemany(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
        [(1, 1), (2, 2), (3, 1), (3, 2), (3, 3), (4, 4), (5, 5), (6, 6)],
    )

    # (rowid, chat, handle, from_me, minutes, body, use_blob, is_read)
    #
    # `use_blob` False writes the text to the `text` column with no
    # attributedBody, standing in for the old rows that still exist in a long
    # history. A body of "\ufffc" is an attachment-only message: Messages marks
    # where the attachment sits with that character, and it is the whole body.
    messages = [
        (1, 1, 1, 0, 0, "Are we still on for Saturday?", True, 1),
        (2, 1, 1, 1, 2, "Yes — 7pm works", True, 1),
        (3, 1, 1, 0, 5, "Bringing the good dice 🎲", True, 1),
        (4, 2, 2, 0, 10, "legacy row, text column only", False, 1),
        (5, 2, 2, 1, 12, "\ufffc", True, 1),
        (6, 3, 1, 0, 20, "Who's in for Game Night?", True, 1),
        (7, 3, 2, 0, 22, "I'm in", True, 1),
        (8, 3, 0, 1, 25, "Same, see you there", True, 1),
        (9, 4, 4, 0, 30, "Sent you the itinerary", True, 0),
        (10, 4, 4, 0, 31, "Let me know what you think", True, 0),
        (13, 5, 5, 0, 35, "Your package is out for delivery", True, 0),
        (14, 6, 6, 0, 40, "You have won a prize, reply YES", True, 0),
    ]

    for rowid, chat_id, handle_id, from_me, offset, body, use_blob, is_read in messages:
        when = apple_time(_minutes(offset))
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, handle_id,"
            " is_from_me, is_read, date, service, is_sent, is_delivered, is_finished)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'iMessage', ?, ?, 1)",
            (
                rowid,
                f"SYNTHETIC-{rowid:04d}",
                None if use_blob else body,
                attributed_body(body) if use_blob else None,
                handle_id,
                from_me,
                is_read,
                when,
                from_me,
                from_me,
            ),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
            " VALUES (?, ?, ?)",
            (chat_id, rowid, when),
        )

    # Rows that are not conversation messages but live in the same table, and
    # so appear in any listing that does not filter them out. The real database
    # held 3,240 tapbacks and 891 system rows, which would be that many phantom
    # entries in a transcript.
    #
    # A tapback: associated_message_type 2000 is "loved", pointing at message 1.
    conn.execute(
        "INSERT INTO message (ROWID, guid, attributedBody, handle_id, is_from_me,"
        " is_read, date, service, is_finished, associated_message_type,"
        " associated_message_guid)"
        " VALUES (11, 'SYNTHETIC-0011', ?, 1, 0, 1, ?, 'iMessage', 1, 2000,"
        " 'p:0/SYNTHETIC-0001')",
        (attributed_body("Loved \u201cAre we still on for Saturday?\u201d"), apple_time(_minutes(6))),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
        " VALUES (1, 11, ?)",
        (apple_time(_minutes(6)),),
    )

    # Reactions in the group, all on Alice's "Who's in for Game Night?" (rowid
    # 6), using each shape of target the real database holds: a part prefix, an
    # app-balloon prefix, and a bare guid. Bob laughed and then took it back,
    # and the row for the removal is kept, as Messages sometimes does.
    #
    # (rowid, handle, from_me, minutes, type, target, emoji)
    reactions = [
        (15, 2, 0, 23, 2006, "p:0/SYNTHETIC-0006", "\U0001f389"),
        (16, 2, 0, 23, 2003, "p:0/SYNTHETIC-0006", None),
        (17, 2, 0, 24, 3003, "p:0/SYNTHETIC-0006", None),
        (18, 0, 1, 24, 2001, "bp:SYNTHETIC-0006", None),
        (19, 3, 0, 24, 1000, "SYNTHETIC-0006", None),
    ]
    for rowid, handle_id, from_me, offset, kind, target, emoji in reactions:
        when = apple_time(_minutes(offset))
        conn.execute(
            "INSERT INTO message (ROWID, guid, handle_id, is_from_me, is_read, date,"
            " service, is_finished, associated_message_type, associated_message_guid,"
            " associated_message_emoji)"
            " VALUES (?, ?, ?, ?, 1, ?, 'iMessage', 1, ?, ?, ?)",
            (rowid, f"SYNTHETIC-{rowid:04d}", handle_id, from_me, when, kind, target, emoji),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
            " VALUES (3, ?, ?)",
            (rowid, when),
        )

    # "Yes — 7pm works" was edited from an earlier draft. Messages rewrites
    # attributedBody to the latest text and keeps the history in the
    # message_summary_info plist: per part, each version as a typedstream.
    edited_at = _minutes(3)
    history = {
        "ec": {
            "0": [
                {"t": attributed_body("Yes — 6pm works"), "d": 0.0},
                {"t": attributed_body("Yes — 7pm works"), "d": 0.0},
            ]
        },
        "ep": [0],
        "otr": {},
        "ust": True,
    }
    conn.execute(
        "UPDATE message SET date_edited = ?, message_summary_info = ? WHERE ROWID = 2",
        (apple_time(edited_at), plistlib.dumps(history, fmt=plistlib.FMT_BINARY)),
    )

    # Bob sent something and unsent it. The row stays, with its text gone, an
    # edit date, and the retracted part listed under "rp".
    unsent_at = _minutes(11)
    conn.execute(
        "INSERT INTO message (ROWID, guid, handle_id, is_from_me, is_read, date,"
        " service, is_finished, date_edited, message_summary_info)"
        " VALUES (20, 'SYNTHETIC-0020', 2, 0, 1, ?, 'iMessage', 1, ?, ?)",
        (
            apple_time(unsent_at),
            apple_time(unsent_at + datetime.timedelta(seconds=30)),
            plistlib.dumps({"rp": [0], "otr": {}, "ust": True}, fmt=plistlib.FMT_BINARY),
        ),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
        " VALUES (2, 20, ?)",
        (apple_time(unsent_at),),
    )

    # Bob's "I'm in" is a reply in a thread started by Alice's question.
    conn.execute(
        "UPDATE message SET thread_originator_guid = 'SYNTHETIC-0006',"
        " thread_originator_part = '0:0:24' WHERE ROWID = 7"
    )

    # A system row: someone named the group. item_type 2 is a group-name change.
    conn.execute(
        "INSERT INTO message (ROWID, guid, handle_id, is_from_me, is_read, date,"
        " service, is_finished, item_type, group_title)"
        " VALUES (12, 'SYNTHETIC-0012', 1, 0, 1, ?, 'iMessage', 1, 2, ?)",
        (apple_time(_minutes(19)), GROUP_NAME),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
        " VALUES (3, 12, ?)",
        (apple_time(_minutes(19)),),
    )

    # One attachment, hung off the attachment-only message (rowid 5).
    conn.execute(
        "INSERT INTO attachment (ROWID, guid, original_guid, filename, mime_type,"
        " uti, total_bytes, is_outgoing, transfer_name)"
        " VALUES (1, 'ATT-0001', 'ATT-0001', '~/Library/Messages/Attachments/ab/IMG_0001.HEIC',"
        " 'image/heic', 'public.heic', 2097152, 1, 'IMG_0001.HEIC')"
    )
    conn.execute(
        "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (5, 1)"
    )
    # Apple keeps this column current with a trigger on message_attachment_join.
    # The schema here has no triggers -- they call functions only Messages.app
    # registers -- so anything a trigger would maintain has to be set by hand.
    conn.execute("UPDATE message SET cache_has_attachments = 1 WHERE ROWID = 5")

    # A hidden attachment: the link-preview payload Messages stores alongside a
    # message. On a real database these were 6,526 of 14,101 attachment rows.
    conn.execute(
        "INSERT INTO attachment (ROWID, guid, original_guid, filename, uti,"
        " total_bytes, is_outgoing, transfer_name, hide_attachment)"
        " VALUES (2, 'ATT-0002', 'ATT-0002',"
        " '~/Library/Messages/Attachments/cd/SYNTHETIC-0003.pluginPayloadAttachment',"
        " 'dyn.synthetic', 4096, 0, 'SYNTHETIC-0003.pluginPayloadAttachment', 1)"
    )
    conn.execute(
        "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (3, 2)"
    )

    conn.commit()
    return conn
