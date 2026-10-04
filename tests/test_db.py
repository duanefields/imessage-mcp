from imessage_mcp import db

from .support.synthetic_db import (
    ALICE,
    BOB,
    DANA_EMAIL,
    GROUP_NAME,
    SPAMMER,
    STRANGER,
    VOICE_TRANSCRIPT,
)

ALICE_CHAT = f"iMessage;-;{ALICE}"
BOB_CHAT = f"iMessage;-;{BOB}"
GROUP_CHAT = "iMessage;+;chat999"
DANA_CHAT = f"iMessage;-;{DANA_EMAIL}"
STRANGER_CHAT = f"SMS;-;{STRANGER}"
JUNK_CHAT = f"SMS;-;{SPAMMER}"


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
        STRANGER_CHAT,
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


def test_reactions_attach_to_the_message_they_react_to(conn):
    messages = {m["guid"]: m for m in db.get_messages(conn, GROUP_CHAT)}
    reactions = messages["SYNTHETIC-0006"]["reactions"]
    assert sorted((r["reaction"], r["handle"], r["is_from_me"]) for r in reactions) == [
        ("emoji", BOB, False),
        ("liked", None, True),
        ("sticker", "+15125550103", False),
    ]
    assert next(r for r in reactions if r["reaction"] == "emoji")["emoji"] == "\U0001f389"
    assert messages["SYNTHETIC-0007"]["reactions"] == []


def test_a_removed_reaction_does_not_stand(conn):
    """Bob laughed, then took it back; the removal row is newer and wins."""
    messages = {m["guid"]: m for m in db.get_messages(conn, GROUP_CHAT)}
    assert "laughed" not in {r["reaction"] for r in messages["SYNTHETIC-0006"]["reactions"]}


def test_reaction_target_strips_the_part_prefix():
    assert db._reaction_target("p:0/SYNTHETIC-0001") == "SYNTHETIC-0001"
    assert db._reaction_target("p:12/SYNTHETIC-0001") == "SYNTHETIC-0001"
    assert db._reaction_target("bp:SYNTHETIC-0001") == "SYNTHETIC-0001"
    assert db._reaction_target("SYNTHETIC-0001") == "SYNTHETIC-0001"


def test_reactions_are_not_counted_as_messages(conn):
    assert db.count_messages(conn, GROUP_CHAT) == 3


def test_a_reply_carries_the_message_it_replied_to(conn):
    messages = {m["guid"]: m for m in db.get_messages(conn, GROUP_CHAT)}
    assert messages["SYNTHETIC-0007"]["reply_to"] == {
        "guid": "SYNTHETIC-0006",
        "text": "Who's in for Game Night?",
        "is_from_me": False,
        "has_attachments": False,
        "handle": ALICE,
    }
    assert messages["SYNTHETIC-0008"]["reply_to"] is None


def test_reply_context_reaches_past_the_page(conn):
    """The original is older than the reply, so a one-message page lacks it."""
    page = db.get_messages(conn, GROUP_CHAT, limit=2)
    assert "SYNTHETIC-0006" not in {m["guid"] for m in page}
    reply = next(m for m in page if m["guid"] == "SYNTHETIC-0007")
    assert reply["reply_to"]["text"] == "Who's in for Game Night?"


def test_search_results_carry_reply_context(conn):
    matches, _ = db.search_messages(conn, "I'm in")
    assert matches[0]["reply_to"]["text"] == "Who's in for Game Night?"


def test_an_edited_message_says_so_and_shows_its_latest_text(conn):
    messages = {m["guid"]: m for m in db.get_messages(conn, ALICE_CHAT)}
    edited = messages["SYNTHETIC-0002"]
    assert edited["text"] == "Yes — 7pm works"
    assert edited["edited_at"] is not None
    assert edited["unsent"] is False
    assert messages["SYNTHETIC-0001"]["edited_at"] is None


def test_an_unsent_message_is_not_called_edited(conn):
    """Unsending stamps date_edited too; the retracted-parts list tells them apart."""
    messages = {m["guid"]: m for m in db.get_messages(conn, BOB_CHAT)}
    unsent = messages["SYNTHETIC-0020"]
    assert unsent["unsent"] is True
    assert unsent["text"] is None
    assert unsent["edited_at"] is None


def test_a_voice_message_carries_its_transcript(conn):
    messages = {m["guid"]: m for m in db.get_messages(conn, BOB_CHAT)}
    voice = messages["SYNTHETIC-0021"]
    assert voice["voice_message"] is True
    assert voice["text"] == VOICE_TRANSCRIPT
    assert messages["SYNTHETIC-0004"]["voice_message"] is False


def test_search_finds_words_spoken_in_a_voice_message(conn):
    matches, total = db.search_messages(conn, "save me a seat")
    assert total == 1
    assert matches[0]["guid"] == "SYNTHETIC-0021"


def test_list_chats_carries_handles(conn):
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}
    assert sorted(chats[GROUP_CHAT]["handles"]) == sorted([ALICE, BOB, "+15125550103"])
    assert chats[ALICE_CHAT]["handles"] == [ALICE]


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
    messages = {m["guid"]: m for m in db.get_messages(conn, BOB_CHAT)}
    attachment_only = messages["SYNTHETIC-0005"]
    assert attachment_only["has_attachments"] is True
    assert attachment_only["text"] is None


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


def test_list_chats_leaves_out_junk_and_labels_unknown_senders(conn):
    """The junk chat is the most recently active, so it would lead the list."""
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}
    assert JUNK_CHAT not in chats
    assert db.count_chats(conn) == len(chats)
    assert chats[STRANGER_CHAT]["filtered"] == "unknown sender"
    assert chats[DANA_CHAT]["filtered"] is None


def test_list_chats_reaches_junk_when_asked_for_by_guid(conn):
    """A contact search names the chats it wants, and that includes Junk."""
    chats = db.list_chats(conn, guids=[JUNK_CHAT])
    assert [c["filtered"] for c in chats] == ["junk"]
    assert db.count_chats(conn, guids=[JUNK_CHAT]) == 1


def test_unread(conn):
    unread = db.get_unread(conn)
    assert [m["text"] for m in unread] == [
        "Your package is out for delivery",
        "Let me know what you think",
        "Sent you the itinerary",
    ]
    assert all(m["is_from_me"] is False for m in unread)
    assert unread[0]["chat_guid"] == STRANGER_CHAT
    assert unread[0]["filtered"] == "unknown sender"
    assert unread[1]["filtered"] is None


def test_unread_leaves_out_junk(conn):
    assert JUNK_CHAT not in {m["chat_guid"] for m in db.get_unread(conn)}
    assert db.count_unread(conn) == 3


def test_attachments(conn):
    attachments = db.get_attachments(conn)
    assert len(attachments) == 1
    assert attachments[0]["name"] == "IMG_0001.HEIC"
    assert attachments[0]["mime_type"] == "image/heic"
    assert attachments[0]["size_bytes"] == 2097152
    assert attachments[0]["is_outgoing"] is True
    assert db.get_attachments(conn, chat_guid=ALICE_CHAT) == []


def test_attachments_leave_out_hidden_link_preview_data(conn):
    """Alice's chat holds only a hidden .pluginPayloadAttachment."""
    assert all(
        not a["name"].endswith(".pluginPayloadAttachment")
        for a in db.get_attachments(conn)
    )
    assert db.count_attachments(conn) == 1
    assert db.count_attachments(conn, chat_guid=ALICE_CHAT) == 0


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
