"""Tests for the one tool that can change something outside this process.

Nothing here runs osascript. The send layer is faked at the boundary, because a
test that actually drove Messages would send real messages to real people.
"""

import subprocess

import pytest

from imessage_mcp import applescript, server

from .support.synthetic_db import ALICE

ALICE_CHAT = f"iMessage;-;{ALICE}"


class FakeCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_text_and_guid_are_arguments_never_pasted_into_the_script(monkeypatch):
    """The message text must not be interpolated into the AppleScript source.

    Escaping quotes and backslashes by hand is the usual approach and the usual
    bug. Passing them as argv means there is nothing to escape, so a message
    containing quotes, backslashes or shell syntax is just data.
    """
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input", "")
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    nasty = 'He said "hi" \\ then $(whoami) and `id` \'quoted\''
    applescript.send_to_chat(ALICE_CHAT, nasty)

    assert seen["args"] == ["/usr/bin/osascript", "-", ALICE_CHAT, nasty]
    assert nasty not in seen["input"]
    assert ALICE_CHAT not in seen["input"]
    # No shell anywhere in the call.
    assert "shell" not in str(seen["args"])


def test_empty_text_never_reaches_osascript(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("osascript must not run for empty text")

    monkeypatch.setattr(subprocess, "run", explode)
    with pytest.raises(applescript.SendError):
        applescript.send_to_chat(ALICE_CHAT, "")


def test_a_hung_consent_dialog_fails_with_an_explanation(monkeypatch):
    """The documented failure is a dialog nobody is there to click, which blocks
    forever. A timeout that says so beats a tool call that never returns."""

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=30)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(applescript.SendTimeout) as exc:
        applescript.send_to_chat(ALICE_CHAT, "hello")
    assert "consent" in str(exc.value).lower()


def test_a_refused_send_surfaces_the_reason(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: FakeCompleted(returncode=1, stderr="execution error: -1728"),
    )
    with pytest.raises(applescript.SendError, match="-1728"):
        applescript.send_to_chat(ALICE_CHAT, "hello")


# --- the tool ---------------------------------------------------------------


@pytest.fixture
def wired_send(monkeypatch, chat_db_path, resolver):
    monkeypatch.setenv("IMESSAGE_MCP_DB_PATH", str(chat_db_path))
    monkeypatch.setattr(server, "_resolver_for_now", lambda: resolver)
    monkeypatch.setattr(server, "messages_is_running", lambda: True)
    monkeypatch.setattr(server, "_CONFIRM_TIMEOUT_SECONDS", 0.5)


async def call_send(client, **kwargs):
    async with client:
        return await client.call_tool("send_message", kwargs)


@pytest.fixture
def client():
    from fastmcp import Client

    return Client(server.mcp)


async def test_unknown_chat_is_refused_before_anything_is_sent(
    wired_send, client, monkeypatch
):
    """This is the safety property. A guid that is not already a conversation
    must never reach Messages, or a wrong number could reach a stranger."""

    def explode(*args, **kwargs):
        raise AssertionError("must not attempt a send for an unknown chat")

    monkeypatch.setattr(applescript, "send_to_chat", explode)

    result = await call_send(client, chat_guid="iMessage;-;+15125550199", text="hi")

    assert "error" in result.structured_content
    assert "already exists" in result.content[0].text


async def test_empty_text_is_refused(wired_send, client, monkeypatch):
    monkeypatch.setattr(
        applescript,
        "send_to_chat",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    result = await call_send(client, chat_guid=ALICE_CHAT, text="   ")
    assert "error" in result.structured_content


async def test_refuses_when_messages_is_not_running(
    wired_send, client, monkeypatch
):
    monkeypatch.setattr(server, "messages_is_running", lambda: False)
    monkeypatch.setattr(
        applescript,
        "send_to_chat",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    result = await call_send(client, chat_guid=ALICE_CHAT, text="hello")
    assert "error" in result.structured_content
    assert "not running" in result.content[0].text


async def test_send_failure_is_reported_not_raised(wired_send, client, monkeypatch):
    def fail(*args, **kwargs):
        raise applescript.SendError("Messages refused the send: nope")

    monkeypatch.setattr(applescript, "send_to_chat", fail)
    result = await call_send(client, chat_guid=ALICE_CHAT, text="hello")
    assert "error" in result.structured_content
    assert "nope" in result.content[0].text


async def test_unconfirmed_send_says_so_and_warns_about_retrying(
    wired_send, client, monkeypatch
):
    """Messages accepted it but nothing showed up. Claiming success would be a
    lie and claiming failure would invite a duplicate, so it says exactly what
    is known."""
    monkeypatch.setattr(applescript, "send_to_chat", lambda *a, **k: None)

    result = await call_send(client, chat_guid=ALICE_CHAT, text="never lands")

    body = result.structured_content
    assert body["sent"] is True
    assert body["confirmed"] is False
    assert body["message_guid"] is None
    assert "twice" in result.content[0].text


async def test_confirmed_send_reports_the_message_it_found(
    wired_send, client, monkeypatch, chat_db_path
):
    """The confirmation reads the message back out of the database. AppleScript
    reports success for handing the text to Messages, not for Messages doing
    anything with it."""
    import sqlite3

    from .support.synthetic_db import apple_time
    from .support.typedstream_writer import attributed_body
    import datetime

    sent_text = "confirmed hello"

    def fake_send(chat_guid, text, *args, **kwargs):
        # Stand in for Messages writing the row.
        conn = sqlite3.connect(str(chat_db_path))
        when = apple_time(datetime.datetime.now(datetime.timezone.utc))
        conn.execute(
            "INSERT INTO message (ROWID, guid, attributedBody, handle_id, is_from_me,"
            " is_read, date, service, is_finished, is_sent)"
            " VALUES (900, 'SYNTHETIC-0900', ?, 0, 1, 1, ?, 'iMessage', 1, 1)",
            (attributed_body(text), when),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date)"
            " VALUES (1, 900, ?)",
            (when,),
        )
        conn.commit()
        conn.close()

    monkeypatch.setattr(applescript, "send_to_chat", fake_send)

    result = await call_send(client, chat_guid=ALICE_CHAT, text=sent_text)

    body = result.structured_content
    assert body["sent"] is True
    assert body["confirmed"] is True
    assert body["message_guid"] == "SYNTHETIC-0900"
    assert "confirmed" in result.content[0].text.lower()

    # Leave the fixture as it was found; it is session-scoped.
    conn = sqlite3.connect(str(chat_db_path))
    conn.execute("DELETE FROM chat_message_join WHERE message_id = 900")
    conn.execute("DELETE FROM message WHERE ROWID = 900")
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# What /health is told about the last send
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def forget_the_last_send():
    """The record lives in the module, so without this one test's send shows up
    in the next one's assertions."""
    applescript.reset_last_send()
    yield
    applescript.reset_last_send()


def test_nothing_is_reported_before_the_first_send():
    """A freshly restarted server has not been asked to send. That is not a
    failure, and a monitor must not read it as one."""
    assert applescript.last_send() == {
        "at": None,
        "ok": None,
        "action": None,
        "error": None,
    }


def test_a_successful_send_is_recorded(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    applescript.send_to_chat("SYNTHETIC-0001", "hello")
    record = applescript.last_send()
    assert record["ok"] is True
    assert record["action"] == "send_message"
    assert record["error"] is None


def test_a_timeout_is_recorded_as_a_failed_send(monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.SendTimeout):
        applescript.send_to_chat("SYNTHETIC-0001", "hello")
    assert applescript.last_send()["ok"] is False
    assert applescript.last_send()["error"] == "SendTimeout"


def test_a_refusal_is_recorded_as_a_failed_send(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Messages got an error (-1728)"
        ),
    )
    with pytest.raises(applescript.SendError):
        applescript.send_to_chat("SYNTHETIC-0001", "hello")
    assert applescript.last_send()["ok"] is False
    assert applescript.last_send()["error"] == "SendError"


def test_the_published_failure_never_carries_the_message_text(monkeypatch):
    """The sharpest version of this hazard in the project. /health is
    unauthenticated and healthcheck.sh forwards it off the host, while the argv
    osascript is handed is [chat_guid, text] -- the text being someone's
    private message. Publishing stderr would send a conversation to a ping
    service because somebody mistyped a chat guid."""
    private = "meet me at the safehouse at midnight"

    def fake_run(argv, **kw):
        # osascript echoing back what it was given, which is the real shape.
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=f"osascript: error running {argv!r}"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(applescript.SendError, match="safehouse"):
        applescript.send_to_chat("SYNTHETIC-0001", private)

    published = applescript.last_send()
    assert published["error"] == "SendError"
    assert private not in repr(published)
    assert "safehouse" not in repr(published)


def test_an_empty_message_is_not_recorded_as_a_failed_send():
    """A caller's bad argument, rejected before Messages is consulted. It says
    nothing about whether this host can still send, and recording it would page
    somebody over a model's mistake."""
    with pytest.raises(applescript.SendError):
        applescript.send_to_chat("SYNTHETIC-0001", "")
    assert applescript.last_send()["at"] is None
