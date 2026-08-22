"""The archive is private, the messages in it are written by strangers, and
this server can send. That is the whole lethal trifecta, so the interesting
tests are the ones where a message tries to use the send tool.

Two defenses are exercised here: content read from one conversation cannot be
sent to another without the operator saying so, and everything carrying message
text is labeled as untrusted in the tool output.
"""

import pytest
from fastmcp import Client

from imessage_mcp import applescript, provenance, server

from .support.synthetic_db import ALICE, BOB, DANA_EMAIL

ALICE_CHAT = f"iMessage;-;{ALICE}"
BOB_CHAT = f"iMessage;-;{BOB}"
DANA_CHAT = f"iMessage;-;{DANA_EMAIL}"

ATTACKER = "iMessage;-;+15125550199"


# --- what counts as forwarding ----------------------------------------------


def test_a_relayed_sentence_is_traced_to_where_it_was_read():
    provenance.record([(ALICE_CHAT, "Are we still on for Saturday at the lake house")])
    assert provenance.cross_chat_sources(
        BOB_CHAT, "she says: are we still on for Saturday at the lake house"
    ) == [ALICE_CHAT]


def test_quoting_a_conversation_back_to_itself_is_not_forwarding():
    """Replying to what somebody just said is the ordinary case, and the whole
    point of the rule is that it is about *where* the text goes."""
    provenance.record([(ALICE_CHAT, "Are we still on for Saturday at the lake house")])
    assert provenance.cross_chat_sources(
        ALICE_CHAT, "Are we still on for Saturday at the lake house"
    ) == []


def test_a_short_common_phrase_is_not_forwarding():
    """Everybody says "sounds good". Refusing on that would train the caller to
    set confirm_forward every time, which costs more than it buys."""
    provenance.record([(ALICE_CHAT, "sounds good")])
    assert provenance.cross_chat_sources(BOB_CHAT, "sounds good") == []


def test_a_verification_code_is_caught_however_it_is_retyped():
    """The highest-value payload in the archive is also the shortest, so it
    clears no run-of-words test. It survives being spaced, dashed and dropped
    into a sentence somebody else wrote."""
    provenance.record([(ALICE_CHAT, "Your verification code is 847213")])
    assert provenance.cross_chat_sources(ATTACKER, "the number is 847 213") == [
        ALICE_CHAT
    ]
    assert provenance.cross_chat_sources(ATTACKER, "847-213") == [ALICE_CHAT]
    assert provenance.cross_chat_sources(ATTACKER, "code: 847213, thanks!") == [
        ALICE_CHAT
    ]


def test_an_address_read_elsewhere_is_caught():
    provenance.record([(DANA_CHAT, "reach her at dana@example.com or 512-555-0104")])
    assert provenance.cross_chat_sources(ATTACKER, "try DANA@example.com") == [DANA_CHAT]
    assert provenance.cross_chat_sources(ATTACKER, "call (512) 555 0104") == [DANA_CHAT]


def test_a_paraphrase_is_not_caught_and_that_is_known():
    """The honest limit of this defense. Nothing in a sentence written from
    memory ties it back to the conversation it came from, which is why the
    untrusted-content labeling below is the other half and not a nicety."""
    provenance.record([(ALICE_CHAT, "Are we still on for Saturday at the lake house")])
    assert provenance.cross_chat_sources(BOB_CHAT, "she wants to know about the weekend") == []


def test_only_the_recent_past_is_remembered():
    """Bounded on purpose: an unbounded log drifts toward refusing to send
    anything the user has ever said."""
    provenance.record([(ALICE_CHAT, "the very first thing that was ever read")])
    provenance.record(
        (BOB_CHAT, f"filler message number {n} in the log") for n in range(600)
    )
    assert provenance.cross_chat_sources(BOB_CHAT, "the very first thing that was ever read") == []


def test_empty_and_unattributed_text_is_not_recorded():
    provenance.record([(ALICE_CHAT, ""), (ALICE_CHAT, None), (None, "orphaned text")])
    assert provenance.cross_chat_sources(BOB_CHAT, "orphaned text at all") == []


# --- the tools --------------------------------------------------------------


@pytest.fixture
def client():
    return Client(server.mcp)


@pytest.fixture(autouse=True)
def wired(monkeypatch, chat_db_path, resolver):
    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(chat_db_path))
    monkeypatch.setattr(server, "_resolver_for_now", lambda: resolver)
    monkeypatch.setattr(server, "messages_is_running", lambda: True)
    monkeypatch.setattr(server, "_CONFIRM_TIMEOUT_SECONDS", 0.1)


@pytest.fixture
def sent(monkeypatch):
    """Record what reaches Messages, without reaching Messages."""
    calls = []
    monkeypatch.setattr(
        applescript, "send_to_chat", lambda guid, text: calls.append((guid, text))
    )
    return calls


def text_of(result):
    return result.content[0].text


ALICE_LINE = "Are we still on for Saturday?"


async def test_reading_one_chat_then_sending_to_another_is_refused(client, sent):
    """The attack this exists for: text arrives in one conversation telling the
    model to relay what is in another. Every check that was already here passes
    -- the chat exists, the text is not empty, Messages is running."""
    async with client:
        await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT})
        result = await client.call_tool(
            "send_message", {"chat_guid": BOB_CHAT, "text": f"Alice asked: {ALICE_LINE}"}
        )

    assert "error" in result.structured_content
    assert "Alice Example" in text_of(result)
    assert "confirm_forward" in text_of(result)
    assert sent == []


async def test_replying_in_the_conversation_it_was_read_from_is_allowed(client, sent):
    async with client:
        await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT})
        result = await client.call_tool(
            "send_message", {"chat_guid": ALICE_CHAT, "text": ALICE_LINE}
        )

    assert result.structured_content["sent"] is True
    assert sent == [(ALICE_CHAT, ALICE_LINE)]


async def test_the_operator_can_still_ask_for_a_forward(client, sent):
    """A refusal that cannot be overridden would break "send Bob what Alice
    said", which is a thing people legitimately want."""
    async with client:
        await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT})
        result = await client.call_tool(
            "send_message",
            {
                "chat_guid": BOB_CHAT,
                "text": f"Alice asked: {ALICE_LINE}",
                "confirm_forward": True,
            },
        )

    assert result.structured_content["sent"] is True
    assert len(sent) == 1


async def test_a_search_hit_is_traced_to_the_chat_it_was_found_in(client, sent):
    """Search crosses conversations, so each row carries its own source rather
    than inheriting the one the tool was called with."""
    async with client:
        await client.call_tool("search_messages", {"query": "Saturday"})
        result = await client.call_tool(
            "send_message", {"chat_guid": BOB_CHAT, "text": ALICE_LINE}
        )

    assert "error" in result.structured_content
    assert "Alice Example" in text_of(result)
    assert sent == []


async def test_unread_is_traced_to_the_chat_it_arrived_in(client, sent):
    async with client:
        await client.call_tool("get_unread", {})
        result = await client.call_tool(
            "send_message",
            {"chat_guid": ALICE_CHAT, "text": "Let me know what you think"},
        )

    assert "error" in result.structured_content
    assert "Dana Example" in text_of(result)
    assert sent == []


async def test_a_chat_preview_is_content_too(client, sent):
    """list_chats shows the last message of every conversation, which is as
    much message text as get_messages returns."""
    async with client:
        await client.call_tool("list_chats", {})
        result = await client.call_tool(
            "send_message",
            {"chat_guid": ALICE_CHAT, "text": "Let me know what you think"},
        )

    assert "error" in result.structured_content
    assert sent == []


async def test_an_unknown_chat_is_still_refused_first(client, sent):
    """The older rule keeps precedence: a guid that is not a conversation is a
    wrong number, and that answer is more useful than a forwarding refusal."""
    async with client:
        await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT})
        result = await client.call_tool(
            "send_message", {"chat_guid": ATTACKER, "text": ALICE_LINE}
        )

    assert "already exists" in text_of(result)
    assert sent == []


# --- untrusted labeling -----------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_messages", {"chat_guid": ALICE_CHAT}),
        ("search_messages", {"query": "Saturday"}),
        ("get_unread", {}),
        ("list_chats", {}),
    ],
)
async def test_message_text_arrives_labeled_as_data(client, tool, args):
    async with client:
        result = await client.call_tool(tool, args)

    assert text_of(result).startswith(server.UNTRUSTED_NOTICE)
    assert result.structured_content["untrusted_content"] is True


async def test_the_label_comes_before_the_pagination_line(client):
    """It has to be the first thing read, not buried under a count."""
    async with client:
        result = await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT, "limit": 1})

    body = text_of(result)
    assert body.startswith(server.UNTRUSTED_NOTICE)
    assert "Showing 1-1 of 3" in body


async def test_results_that_carry_no_message_text_are_not_labeled(client):
    """Overusing the label would make it furniture the model reads past."""
    async with client:
        participants = await client.call_tool(
            "get_participants", {"chat_guid": ALICE_CHAT}
        )
        attachments = await client.call_tool("get_attachments", {})

    assert "untrusted_content" not in participants.structured_content
    assert server.UNTRUSTED_NOTICE not in text_of(attachments)
