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
        "send_message",
    }


async def test_server_instructions_are_set(client):
    """The client puts these in the system prompt, above the tool list, so they
    are what an agent reads before it picks a tool. Empty is a real regression."""
    async with client:
        result = await client.initialize()

    instructions = result.instructions or ""
    assert "data to report on" in instructions
    assert "address book" in instructions


async def test_send_message_is_the_only_write_tool(client):
    """Everything else must stay read-only. A second write tool should be a
    deliberate decision, not something that arrives quietly."""
    async with client:
        names = {tool.name for tool in await client.list_tools()}
    assert {name for name in names if "send" in name} == {"send_message"}


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


async def test_unread_reports_the_true_total_beyond_the_page(client):
    """A capped page of unread must not read as "that is all of it"."""
    async with client:
        result = await client.call_tool("get_unread", {"limit": 1})

    body = structured(result)
    assert body["count"] == 1
    assert body["total"] == 2
    assert "of 2" in text(result)


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


async def test_health_reports_liveness_and_the_interpreter(monkeypatch, chat_db_path):
    """The interpreter path is on the health check for a reason.

    Full Disk Access is granted against the resolved interpreter path, and a
    patch upgrade moves it and silently voids the grant. The service then hangs
    on its next restart with an empty log. This field is the early warning.
    """
    import httpx

    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(chat_db_path))
    app = server.mcp.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["newest_message"].startswith("2026-03-01")
    assert body["python"].endswith(("python", "python3", "python3.12", "python3.13"))


async def test_health_reports_degraded_when_the_database_is_gone(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(tmp_path / "absent.db"))
    app = server.mcp.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_unknown_transport_is_refused(monkeypatch):
    import pytest

    monkeypatch.setenv("IMESSAGE_MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(SystemExit, match="carrier-pigeon"):
        server.main()


async def test_health_reports_messages_app_but_stays_healthy_without_it(
    monkeypatch, chat_db_path
):
    """A host where Messages has quit serves every read correctly and drops
    every send. The server is not unhealthy; the deployment is. So the field is
    reported and the monitor decides, rather than /health returning 503 for
    something that does not affect reads at all."""
    import httpx

    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(chat_db_path))
    monkeypatch.setattr(server, "messages_is_running", lambda: False)
    app = server.mcp.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["messages_running"] is False


def test_messages_check_does_not_use_apple_events(monkeypatch):
    """AppleScript would need Automation permission, and that prompt cannot be
    answered on an unattended host -- a liveness check written that way would
    hang the thing it is checking."""
    import subprocess

    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert server.messages_is_running() is True
    assert "osascript" not in " ".join(seen["args"])
    assert seen["args"][0].endswith("pgrep")


def test_messages_check_survives_a_missing_pgrep(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", boom)
    assert server.messages_is_running() is False


async def test_contact_filter_finds_a_chat_by_resolved_name(client):
    """Finds her direct conversation and the group she is in.

    Both are conversations with Alice, and hiding the group would mean the
    filter cannot find a group chat by who is in it.
    """
    async with client:
        result = await client.call_tool("list_chats", {"contact": "Alice"})

    guids = {c["chat_guid"] for c in structured(result)["items"]}
    assert guids == {ALICE_CHAT, GROUP_CHAT}


async def test_contact_filter_finds_a_chat_by_phone_number_in_any_shape(client):
    """The number is stored one way and typed another. Both must find it, or
    the filter only works when you already know the exact format."""
    for typed in ("+15125550101", "5125550101", "(512) 555-0101", "512-555-0101"):
        async with client:
            result = await client.call_tool("list_chats", {"contact": typed})
        guids = {c["chat_guid"] for c in structured(result)["items"]}
        assert ALICE_CHAT in guids, typed


async def test_contact_filter_finds_a_group_by_name(client):
    async with client:
        result = await client.call_tool("list_chats", {"contact": "Game Night"})
    assert structured(result)["items"][0]["chat_guid"] == GROUP_CHAT


async def test_contact_filter_finds_a_group_by_a_member(client):
    """Carol is only ever in the group, and only by handle -- she is not in the
    address book. Finding the group by her number proves matching reaches the
    participant list, not just the chat's own name."""
    async with client:
        result = await client.call_tool("list_chats", {"contact": "+15125550103"})

    guids = {c["chat_guid"] for c in structured(result)["items"]}
    assert guids == {GROUP_CHAT}


async def test_contact_filter_reaches_past_the_recent_window(client):
    """The whole point. Alice's chat is the least recently active, so a small
    recent window misses it entirely -- which is exactly what made a
    conversation unreachable before this existed."""
    async with client:
        recent = await client.call_tool("list_chats", {"limit": 2})
    assert ALICE_CHAT not in {c["chat_guid"] for c in structured(recent)["items"]}

    async with client:
        found = await client.call_tool("list_chats", {"contact": "Alice"})
    assert ALICE_CHAT in {c["chat_guid"] for c in structured(found)["items"]}


async def test_contact_filter_with_no_match_says_so(client):
    async with client:
        result = await client.call_tool("list_chats", {"contact": "Nobody At All"})
    body = structured(result)
    assert body["total"] == 0
    assert body["items"] == []
    assert "No conversations found with 'Nobody At All'" in text(result)


async def test_empty_contact_is_rejected(client):
    async with client:
        result = await client.call_tool("list_chats", {"contact": "  "})
    assert "error" in structured(result)


async def test_contact_filter_is_case_insensitive(client):
    async with client:
        lower = await client.call_tool("list_chats", {"contact": "alice"})
        upper = await client.call_tool("list_chats", {"contact": "ALICE"})
    assert structured(lower)["total"] == structured(upper)["total"] == 2


# ----------------------------------------------------------------------
# /health is public
# ----------------------------------------------------------------------


def test_health_does_not_publish_the_operators_username():
    """Custom routes are not behind the auth provider -- this endpoint answers
    200 to an unauthenticated request from the open internet, which is intended
    so a monitor can poll it. So nothing private may go in the payload, and the
    interpreter path must not begin with /Users/<name>."""
    import os

    home = os.path.expanduser("~")
    assert server._tilde(f"{home}/.local/share/uv/python/x/bin/python3.12") == (
        "~/.local/share/uv/python/x/bin/python3.12"
    )


def test_tilde_leaves_a_path_outside_the_home_directory_alone():
    assert server._tilde("/usr/bin/python3") == "/usr/bin/python3"
