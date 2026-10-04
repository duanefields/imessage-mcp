from imessage_mcp import db, formatters

from .support.synthetic_db import ALICE, DANA_EMAIL, GROUP_NAME

ALICE_CHAT = f"iMessage;-;{ALICE}"
GROUP_CHAT = "iMessage;+;chat999"


def test_format_size():
    assert formatters.format_size(None) == "unknown size"
    assert formatters.format_size(0) == "unknown size"
    assert formatters.format_size(512) == "512 B"
    assert formatters.format_size(2048) == "2.0 KB"
    assert formatters.format_size(2097152) == "2.0 MB"
    assert formatters.format_size(5 * 1024**3) == "5.0 GB"


def test_chat_title_prefers_the_group_name(resolver):
    chat = {"display_name": GROUP_NAME, "chat_identifier": "chat999"}
    assert formatters.chat_title(chat, resolver) == GROUP_NAME


def test_chat_title_resolves_a_one_to_one_chat(resolver):
    chat = {"display_name": None, "chat_identifier": ALICE}
    assert formatters.chat_title(chat, resolver) == "Alice Example"


def test_chat_title_falls_back_to_the_handle():
    chat = {"display_name": None, "chat_identifier": ALICE}
    assert formatters.chat_title(chat, None) == ALICE


def test_format_message_names_the_sender(resolver):
    message = {
        "is_from_me": False,
        "handle": ALICE,
        "date": "2026-03-01T17:00:00+00:00",
        "text": "Are we still on for Saturday?",
    }
    rendered = formatters.format_message(message, resolver)
    assert rendered == (
        "[2026-03-01T17:00:00+00:00] Alice Example: Are we still on for Saturday?"
    )


def test_format_message_labels_my_own_messages(resolver):
    message = {"is_from_me": True, "handle": None, "date": "t", "text": "hi"}
    assert formatters.format_message(message, resolver) == "[t] me: hi"


def test_message_with_no_text_says_so_rather_than_rendering_empty():
    attachment = {"is_from_me": True, "date": "t", "text": None, "has_attachments": True}
    plain = {"is_from_me": True, "date": "t", "text": None, "has_attachments": False}
    assert formatters.format_message(attachment) == "[t] me: [attachment]"
    assert formatters.format_message(plain) == "[t] me: [no text]"


def test_empty_collections_read_as_sentences():
    assert formatters.format_chats([]) == "No conversations found."
    assert formatters.format_messages([]) == "No messages found."
    assert formatters.format_participants([]) == "No participants found."
    assert formatters.format_attachments([]) == "No attachments found."


def test_format_chat_shows_unread_and_guid(conn, resolver):
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}
    rendered = formatters.format_chat(chats[f"iMessage;-;{DANA_EMAIL}"], resolver)
    assert "Dana Example" in rendered
    assert "(2 unread)" in rendered
    assert "Let me know what you think" in rendered
    assert f"guid: iMessage;-;{DANA_EMAIL}" in rendered


def test_format_chat_omits_unread_when_there_is_none(conn, resolver):
    chats = {c["chat_guid"]: c for c in db.list_chats(conn)}
    assert "unread" not in formatters.format_chat(chats[ALICE_CHAT], resolver)


def test_chat_title_names_an_unnamed_group_by_its_members(resolver):
    chat = {"chat_identifier": "chat888", "handles": [ALICE, DANA_EMAIL, "+15125550103"]}
    assert formatters.chat_title(chat, resolver) == (
        "Alice Example, Dana Example, +15125550103"
    )


def test_chat_title_caps_a_large_unnamed_group(resolver):
    handles = [ALICE, "+15125550102", DANA_EMAIL, "+15125550103", "+15125550104"]
    chat = {"chat_identifier": "chat888", "handles": handles}
    assert formatters.chat_title(chat, resolver) == (
        "Alice Example, Bob Example, Dana Example and 2 others"
    )
    chat["handles"] = handles[:4]
    assert formatters.chat_title(chat, resolver).endswith(" and 1 other")


def test_chat_title_of_a_one_to_one_chat_is_the_other_person(resolver):
    """Its identifier is the other person's handle, so it is not a group."""
    chat = {"chat_identifier": ALICE, "handles": [ALICE]}
    assert formatters.chat_title(chat, resolver) == "Alice Example"


def test_chat_title_of_an_empty_unnamed_group_falls_back_to_its_identifier(resolver):
    assert formatters.chat_title({"chat_identifier": "chat888", "handles": []}, resolver) == "chat888"


def test_format_message_marks_edits_and_unsends():
    edited = {"is_from_me": True, "date": "t", "text": "7pm", "edited_at": "t2"}
    assert formatters.format_message(edited) == "[t] me: 7pm (edited)"
    unsent = {"is_from_me": True, "date": "t", "text": None, "unsent": True}
    assert formatters.format_message(unsent) == "[t] me: [unsent]"


def test_format_messages_renders_a_transcript(conn, resolver):
    rendered = formatters.format_messages(db.get_messages(conn, ALICE_CHAT), resolver)
    lines = rendered.splitlines()
    assert len(lines) == 4
    assert "Alice Example: Bringing the good dice 🎲" in lines[0]
    assert "me: Yes — 7pm works" in lines[1]
    assert lines[3] == "  ↳ reactions: Alice Example loved"


def test_format_message_shows_reactions_by_name(conn, resolver):
    rendered = formatters.format_messages(db.get_messages(conn, GROUP_CHAT), resolver)
    reactions = next(line for line in rendered.splitlines() if "reactions" in line)
    assert "Bob Example \U0001f389" in reactions
    assert "me liked" in reactions
    assert "+15125550103 sticker" in reactions
    assert "laughed" not in reactions


def test_format_message_shows_what_a_reply_replied_to(conn, resolver):
    rendered = formatters.format_messages(db.get_messages(conn, GROUP_CHAT), resolver)
    lines = rendered.splitlines()
    reply = next(i for i, line in enumerate(lines) if "Bob Example: I'm in" in line)
    assert lines[reply + 1] == "  ↳ replying to Alice Example: Who's in for Game Night?"


def test_format_message_shortens_a_long_quoted_original():
    message = {
        "is_from_me": True,
        "date": "t",
        "text": "yes",
        "reply_to": {"guid": "G", "text": "x" * 200, "is_from_me": False, "handle": "h"},
    }
    quoted = formatters.format_message(message).splitlines()[1]
    assert quoted.endswith("x…")
    assert len(quoted) < 120


def test_format_message_survives_a_reply_to_a_missing_message():
    """The original can be gone: deleted, or expired from recently deleted."""
    message = {"is_from_me": True, "date": "t", "text": "yes", "reply_to": {"guid": "G"}}
    assert formatters.format_message(message).splitlines()[1] == (
        "  ↳ replying to someone: a message no longer here"
    )


def test_format_participants_keeps_the_handle_alongside_the_name(conn, resolver):
    rendered = formatters.format_participants(
        db.get_participants(conn, GROUP_CHAT), resolver
    )
    assert f"Alice Example ({ALICE}, iMessage)" in rendered
    # Carol is not in the address book, so she is shown by handle, once.
    assert "+15125550103 (+15125550103, SMS)" in rendered


def test_format_attachment(conn):
    rendered = formatters.format_attachments(db.get_attachments(conn))
    assert "IMG_0001.HEIC" in rendered
    assert "image/heic" in rendered
    assert "2.0 MB" in rendered
    assert "sent" in rendered
