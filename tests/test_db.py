from imessage_mcp import db

from .support.synthetic_db import ALICE, BOB, DANA_EMAIL, GROUP_NAME

ALICE_CHAT = f"iMessage;-;{ALICE}"
BOB_CHAT = f"iMessage;-;{BOB}"
GROUP_CHAT = "iMessage;+;chat999"
DANA_CHAT = f"iMessage;-;{DANA_EMAIL}"


def test_connection_is_read_only(conn):
    import sqlite3
    import pytest

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO handle (id, service) VALUES ('x', 'iMessage')")


def test_to_iso():
    from .support.synthetic_db import BASE, apple_time

    assert db.to_iso(None) is None
    assert db.to_iso(0) is None
    assert db.to_iso(apple_time(BASE)) == BASE.isoformat()


def test_list_chats_orders_by_activity(conn):
    chats = db.list_chats(conn)
    assert [c["chat_guid"] for c in chats] == [
        DANA_CHAT,
        GROUP_CHAT,
        BOB_CHAT,
        ALICE_CHAT,
    ]


def test_list_chats_carries_preview_and_unread(conn):
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}

    assert chats[DANA_CHAT]["unread_count"] == 2
    assert chats[DANA_CHAT]["last_message"] == "Let me know what you think"
    assert chats[ALICE_CHAT]["unread_count"] == 0
    assert chats[GROUP_CHAT]["display_name"] == GROUP_NAME


def test_list_chats_preview_skips_tapbacks(conn):
    """The newest row in Alice's chat is a tapback, not a message.

    Without the conversation filter the preview reads "Loved ..." instead of the
    last thing actually said.
    """
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}
    assert chats[ALICE_CHAT]["last_message"] == "Bringing the good dice 🎲"


def test_get_messages_excludes_tapbacks_and_system_rows(conn):
    alice = db.get_messages(conn, ALICE_CHAT)
    assert [m["text"] for m in alice] == [
        "Bringing the good dice 🎲",
        "Yes — 7pm works",
        "Are we still on for Saturday?",
    ]

    group = db.get_messages(conn, GROUP_CHAT)
    assert all("Loved" not in (m["text"] or "") for m in group)
    assert len(group) == 3


def test_get_messages_direction_and_handles(conn):
    messages = db.get_messages(conn, ALICE_CHAT)
    latest = messages[0]
    assert latest["is_from_me"] is False
    assert latest["handle"] == ALICE
    assert messages[1]["is_from_me"] is True


def test_get_messages_reads_legacy_text_column(conn):
    messages = db.get_messages(conn, BOB_CHAT)
    assert "legacy row, text column only" in [m["text"] for m in messages]


def test_get_messages_attachment_only_has_no_text(conn):
    messages = db.get_messages(conn, BOB_CHAT)
    attachment_only = [m for m in messages if m["has_attachments"]]
    assert len(attachment_only) == 1
    assert attachment_only[0]["text"] is None


def test_pagination(conn):
    assert db.count_messages(conn, ALICE_CHAT) == 3
    first = db.get_messages(conn, ALICE_CHAT, limit=2)
    second = db.get_messages(conn, ALICE_CHAT, limit=2, offset=2)
    assert len(first) == 2 and len(second) == 1
    assert first[0]["guid"] != second[0]["guid"]


def test_participants(conn):
    assert db.get_participants(conn, GROUP_CHAT) == [
        {"handle": ALICE, "service": "iMessage"},
        {"handle": BOB, "service": "iMessage"},
        {"handle": "+15125550103", "service": "SMS"},
    ]
    assert db.get_participants(conn, ALICE_CHAT) == [
        {"handle": ALICE, "service": "iMessage"}
    ]


def test_unread(conn):
    unread = db.get_unread(conn)
    assert [m["text"] for m in unread] == [
        "Let me know what you think",
        "Sent you the itinerary",
    ]
    assert all(m["is_from_me"] is False for m in unread)
    assert unread[0]["chat_guid"] == DANA_CHAT


def test_attachments(conn):
    attachments = db.get_attachments(conn)
    assert len(attachments) == 1
    assert attachments[0]["name"] == "IMG_0001.HEIC"
    assert attachments[0]["mime_type"] == "image/heic"
    assert attachments[0]["size_bytes"] == 2097152
    assert attachments[0]["is_outgoing"] is True
    assert db.get_attachments(conn, chat_guid=ALICE_CHAT) == []


def test_search_finds_across_chats(conn):
    page, total = db.search_messages(conn, "the")
    assert total == len(page)
    assert total >= 2
    assert {m["chat_guid"] for m in page} >= {ALICE_CHAT, DANA_CHAT}


def test_search_is_case_insensitive(conn):
    lower, lower_total = db.search_messages(conn, "saturday")
    upper, upper_total = db.search_messages(conn, "SATURDAY")
    assert lower_total == upper_total == 1
    assert lower[0]["guid"] == upper[0]["guid"]


def test_search_matches_decoded_blobs_not_just_text_column(conn):
    """Nearly every real message is a blob, so a search that only reads the
    text column matches almost nothing."""
    page, total = db.search_messages(conn, "dice")
    assert total == 1
    assert page[0]["text"] == "Bringing the good dice 🎲"


def test_search_scoped_to_chat(conn):
    _, everywhere = db.search_messages(conn, "in")
    _, in_group = db.search_messages(conn, "in", chat_guid=GROUP_CHAT)
    assert in_group < everywhere


def test_search_total_is_true_count_not_page_size(conn):
    page, total = db.search_messages(conn, "e", limit=1)
    assert len(page) == 1
    assert total > 1


def test_search_excludes_tapbacks(conn):
    _, total = db.search_messages(conn, "Loved")
    assert total == 0


def test_search_offset(conn):
    first, total = db.search_messages(conn, "e", limit=1)
    second, _ = db.search_messages(conn, "e", limit=1, offset=1)
    assert first[0]["guid"] != second[0]["guid"]
