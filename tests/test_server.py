"""Exercise the tools through the MCP protocol, not by calling them directly.

Calling a tool function in-process skips schema generation, argument coercion
and the result envelope -- the parts most likely to be wrong in a way a unit
test cannot see.
"""

import json

import pytest
from fastmcp import Client

from imessage_mcp import server

from .support.synthetic_db import ALICE, DANA_EMAIL, GROUP_NAME

ALICE_CHAT = f"iMessage;-;{ALICE}"
GROUP_CHAT = "iMessage;+;chat999"


@pytest.fixture(autouse=True)
def wired(monkeypatch, chat_db_path, resolver):
    """Point the server at the fixture, and at invented contacts."""
    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(chat_db_path))
    monkeypatch.setattr(server, "_resolver_for_now", lambda: resolver)


@pytest.fixture
def client():
    return Client(server.mcp)


def structured(result):
    return result.structured_content


def text(result):
    return result.content[0].text


async def test_every_tool_is_registered(client):
    async with client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {
        "list_chats",
        "get_messages",
        "search_messages",
        "get_participants",
        "get_unread",
        "get_attachments",
    }


async def test_no_write_tool_is_exposed(client):
    """Milestone one is read-only. Sending is not wired up yet, and this fails
    loudly if a send tool is added without a deliberate decision."""
    async with client:
        names = {tool.name for tool in await client.list_tools()}
    assert not any("send" in name for name in names)


async def test_list_chats_returns_text_and_structure(client):
    async with client:
        result = await client.call_tool("list_chats", {})

    body = structured(result)
    assert body["total"] == 4
    assert body["count"] == 4
    assert {chat["chat_guid"] for chat in body["items"]} == {
        ALICE_CHAT,
        GROUP_CHAT,
        f"iMessage;-;{DANA_EMAIL}",
        f"iMessage;-;+15125550102",
    }
    assert "Alice Example" in text(result)
    assert GROUP_NAME in text(result)


async def test_list_chats_pagination_reports_the_true_total(client):
    async with client:
        result = await client.call_tool("list_chats", {"limit": 2})

    body = structured(result)
    assert body["count"] == 2
    assert body["total"] == 4
    assert body["limit"] == 2
    assert "Showing 1-2 of 4" in text(result)


async def test_get_messages_reads_a_conversation(client):
    async with client:
        result = await client.call_tool("get_messages", {"chat_guid": ALICE_CHAT})

    body = structured(result)
    assert body["total"] == 3
    assert [m["text"] for m in body["items"]] == [
        "Bringing the good dice 🎲",
        "Yes — 7pm works",
        "Are we still on for Saturday?",
    ]


async def test_unknown_chat_is_an_error_not_an_empty_list(client):
    """"No such conversation" and "that conversation is empty" are different
    facts, and the model can act on the difference."""
    async with client:
        result = await client.call_tool("get_messages", {"chat_guid": "nope"})

    assert "error" in structured(result)
    assert "no conversation" in text(result).lower()


async def test_search_reports_a_true_total(client):
    async with client:
        result = await client.call_tool(
            "search_messages", {"query": "the", "limit": 1}
        )

    body = structured(result)
    assert body["count"] == 1
    assert body["total"] > 1


async def test_search_with_no_matches_says_so(client):
    async with client:
        result = await client.call_tool(
            "search_messages", {"query": "zzzznotpresent"}
        )

    assert structured(result)["total"] == 0
    assert "No messages matching" in text(result)


async def test_empty_query_is_rejected(client):
    async with client:
        result = await client.call_tool("search_messages", {"query": "   "})
    assert "error" in structured(result)


async def test_bad_pagination_is_rejected(client):
    async with client:
        zero = await client.call_tool("list_chats", {"limit": 0})
        negative = await client.call_tool("list_chats", {"offset": -1})
    assert "error" in structured(zero)
    assert "error" in structured(negative)


async def test_participants_resolve_names(client):
    async with client:
        result = await client.call_tool("get_participants", {"chat_guid": GROUP_CHAT})

    assert structured(result)["total"] == 3
    assert "Alice Example" in text(result)


async def test_unread(client):
    async with client:
        result = await client.call_tool("get_unread", {})

    body = structured(result)
    assert body["count"] == 2
    assert all(item["is_from_me"] is False for item in body["items"])


async def test_attachments_are_metadata_only(client):
    async with client:
        result = await client.call_tool("get_attachments", {})

    item = structured(result)["items"][0]
    assert item["name"] == "IMG_0001.HEIC"
    assert "2.0 MB" in text(result)
    # Nothing that could carry file contents or a local path.
    assert not any(key in item for key in ("data", "contents", "path", "filename"))


async def test_structured_content_is_json_serializable(client):
    async with client:
        result = await client.call_tool("list_chats", {})
    json.dumps(structured(result))
