"""MCP tools over the iMessage database.

Read-only for now. Sending is a later step, and is deliberately not wired up
here yet -- a send cannot be recalled, so it does not get added as a side effect
of building the read surface.
"""

import json
import logging
import os
import platform
import subprocess
import sys
import time

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from starlette.responses import JSONResponse

from . import db
from .auth import build_auth
from .contacts import ContactResolver
from .formatters import (
    format_attachments,
    format_chats,
    format_messages,
    format_participants,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("iMessage")

# The address book is small (34ms for 1,672 handles on the reference machine),
# so it is cached rather than held open, and re-read periodically. Never
# reloading would leave a long-running server resolving names that have since
# changed; reloading per call would spend a third of a listing's time on it.
_CONTACTS_TTL_SECONDS = 300
_resolver: ContactResolver | None = None
_resolver_loaded_at: float = 0.0


def _resolver_for_now() -> ContactResolver:
    global _resolver, _resolver_loaded_at
    now = time.monotonic()
    if _resolver is None or now - _resolver_loaded_at > _CONTACTS_TTL_SECONDS:
        _resolver = ContactResolver()
        _resolver_loaded_at = now
    return _resolver


def _validate_pagination(limit: int | None, offset: int) -> str | None:
    if limit is not None and limit <= 0:
        return "Error: limit must be a positive integer"
    if offset < 0:
        return "Error: offset must be zero or a positive integer"
    return None


def _error_result(message: str) -> ToolResult:
    """Errors are returned, never raised.

    A raised exception reaches the model as an opaque failure it cannot act on.
    """
    return ToolResult(content=message, structured_content={"error": message})


def _result(
    items: list[dict],
    text: str,
    total: int,
    offset: int,
    limit: int | None,
) -> ToolResult:
    """Text for the model to read, plus the same data as structured content.

    Pagination is done in SQL rather than by slicing a full result set, so
    ``items`` is already the page and ``total`` is counted separately.
    """
    if total > len(items) and items:
        first = offset + 1
        text = f"Showing {first}-{offset + len(items)} of {total}\n\n{text}"
    structured = {
        "items": json.loads(json.dumps(items, default=str)),
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
    }
    return ToolResult(content=text, structured_content=structured)


@mcp.tool
async def list_chats(limit: int = 20, offset: int = 0) -> ToolResult:
    """List conversations, most recently active first.

    Each entry carries the chat_guid needed by the other tools, the other
    participant's name where it is known, how many unread messages it holds, and
    a preview of the last message.

    Args:
        limit: Maximum number of conversations to return (default: 20)
        offset: Number of conversations to skip from the start (default: 0)
    """
    error = _validate_pagination(limit, offset)
    if error:
        return _error_result(error)

    conn = db.connect()
    try:
        chats = db.list_chats(conn, limit=limit, offset=offset)
        total = db.count_chats(conn)
    finally:
        conn.close()

    resolver = _resolver_for_now()
    return _result(chats, format_chats(chats, resolver), total, offset, limit)


@mcp.tool
async def get_messages(chat_guid: str, limit: int = 50, offset: int = 0) -> ToolResult:
    """Read messages from one conversation, newest first.

    Tapbacks and system events such as group renames are excluded: they are
    stored as messages but are not things anybody said.

    Args:
        chat_guid: The conversation's guid, from list_chats
        limit: Maximum number of messages to return (default: 50)
        offset: Number of messages to skip from the start (default: 0)
    """
    error = _validate_pagination(limit, offset)
    if error:
        return _error_result(error)

    conn = db.connect()
    try:
        if not db.chat_exists(conn, chat_guid):
            return _error_result(f"Error: no conversation with guid '{chat_guid}'")
        messages = db.get_messages(conn, chat_guid, limit=limit, offset=offset)
        total = db.count_messages(conn, chat_guid)
    finally:
        conn.close()

    resolver = _resolver_for_now()
    return _result(messages, format_messages(messages, resolver), total, offset, limit)


@mcp.tool
async def search_messages(
    query: str,
    limit: int = 20,
    offset: int = 0,
    chat_guid: str | None = None,
) -> ToolResult:
    """Search message text across conversations, case-insensitively.

    The entire history is searched, not a recent window, so a total of 0 means
    the text is genuinely not there. The total is a true count of matches;
    limit bounds only how many are returned.

    Args:
        query: Text to look for
        limit: Maximum number of matches to return (default: 20)
        offset: Number of matches to skip from the start (default: 0)
        chat_guid: Restrict the search to one conversation (default: all)
    """
    error = _validate_pagination(limit, offset)
    if error:
        return _error_result(error)
    if not query or not query.strip():
        return _error_result("Error: query must not be empty")

    conn = db.connect()
    try:
        if chat_guid and not db.chat_exists(conn, chat_guid):
            return _error_result(f"Error: no conversation with guid '{chat_guid}'")
        matches, total = db.search_messages(
            conn, query, limit=limit, offset=offset, chat_guid=chat_guid
        )
    finally:
        conn.close()

    resolver = _resolver_for_now()
    text = format_messages(matches, resolver)
    if not matches:
        text = f"No messages matching '{query}'."
    return _result(matches, text, total, offset, limit)


@mcp.tool
async def get_participants(chat_guid: str) -> ToolResult:
    """List who is in a conversation, with their handles and service.

    Args:
        chat_guid: The conversation's guid, from list_chats
    """
    conn = db.connect()
    try:
        if not db.chat_exists(conn, chat_guid):
            return _error_result(f"Error: no conversation with guid '{chat_guid}'")
        participants = db.get_participants(conn, chat_guid)
    finally:
        conn.close()

    resolver = _resolver_for_now()
    return _result(
        participants,
        format_participants(participants, resolver),
        len(participants),
        0,
        None,
    )


@mcp.tool
async def get_unread(limit: int = 50) -> ToolResult:
    """List received messages that have not been read yet, newest first.

    Args:
        limit: Maximum number of messages to return (default: 50)
    """
    error = _validate_pagination(limit, 0)
    if error:
        return _error_result(error)

    conn = db.connect()
    try:
        unread = db.get_unread(conn, limit=limit)
    finally:
        conn.close()

    resolver = _resolver_for_now()
    text = format_messages(unread, resolver) if unread else "No unread messages."
    return _result(unread, text, len(unread), 0, limit)


@mcp.tool
async def get_attachments(
    chat_guid: str | None = None, limit: int = 20, offset: int = 0
) -> ToolResult:
    """List attachment metadata: name, type, size, direction and date.

    The files themselves are not read, so this says what was sent, not what is
    in it.

    Args:
        chat_guid: Restrict to one conversation (default: all)
        limit: Maximum number of attachments to return (default: 20)
        offset: Number of attachments to skip from the start (default: 0)
    """
    error = _validate_pagination(limit, offset)
    if error:
        return _error_result(error)

    conn = db.connect()
    try:
        if chat_guid and not db.chat_exists(conn, chat_guid):
            return _error_result(f"Error: no conversation with guid '{chat_guid}'")
        attachments = db.get_attachments(
            conn, chat_guid=chat_guid, limit=limit, offset=offset
        )
        total = db.count_attachments(conn, chat_guid=chat_guid)
    finally:
        conn.close()

    return _result(attachments, format_attachments(attachments), total, offset, limit)


def messages_is_running() -> bool:
    """Whether Messages.app is up.

    Reads work without it -- the database is on disk either way -- but sending
    goes through Messages, so a host where it has quietly quit can serve every
    read correctly and drop every send.

    Checked with pgrep rather than by asking Messages over AppleScript. Asking
    would need Apple Events permission, and that prompt cannot be pre-granted or
    answered on an unattended host, so a liveness check written that way would
    itself hang the thing it is meant to be checking.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Messages"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Liveness, plus the two things that actually break this deployment.

    `python` is reported because Full Disk Access is granted against the
    interpreter's resolved path, and a patch upgrade silently moves it and
    voids the grant. The service then hangs on its next restart with nothing in
    the log. Watching this field is the early warning.

    `newest_message` distinguishes a working server from one that is serving a
    database Messages has stopped writing to, and `messages_running` catches
    the case where Messages has quit: reads keep working, sends would not.
    """
    payload = {
        "status": "ok",
        "python": os.path.realpath(sys.executable),
        "python_version": platform.python_version(),
        # Reported, but does not make the server unhealthy: reads work whether
        # or not Messages is up. It is the monitor's job to decide that a host
        # which cannot send is a problem worth waking someone for.
        "messages_running": messages_is_running(),
    }
    try:
        conn = db.connect()
        try:
            row = conn.execute("SELECT MAX(date) AS newest FROM message").fetchone()
            payload["newest_message"] = db.to_iso(row["newest"])
            payload["database"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        payload["status"] = "degraded"
        payload["database"] = f"unreachable: {exc.__class__.__name__}"
        return JSONResponse(payload, status_code=503)

    return JSONResponse(payload)


def main() -> None:
    """Run the server.

    Transport settings are read here rather than at import time so that a
    launcher or a test can set the environment after importing.
    """
    # FastMCP checks PyPI for a newer version on startup and prints a banner.
    # Neither is wanted here: this server exists to read a private message
    # archive, so it should not make an unrequested outbound request every time
    # it starts, and on an unattended host that call is startup latency and one
    # more thing to fail when the network is down. setdefault, so an operator
    # who wants them back can still ask.
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "false")
    os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

    transport = os.environ.get("IMESSAGE_MCP_TRANSPORT", "stdio")
    if transport == "http":
        host = os.environ.get("IMESSAGE_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("IMESSAGE_MCP_PORT", "8000"))
        # Authentication guards the HTTP transport only. stdio takes its
        # security from the fact that running it means already having a shell.
        mcp.auth = build_auth()
        # Stateless by default: a fresh transport per request. A remote client
        # dials from a pool of addresses, and a request arriving from a
        # different address than the one that opened the session is rejected
        # with a 400 that wedges the connection. Nothing here needs session
        # state -- no subscriptions, no server-initiated messages.
        stateless = (
            os.environ.get("IMESSAGE_MCP_STATELESS", "true").strip().lower() != "false"
        )
        mcp.run(transport="http", host=host, port=port, stateless_http=stateless)
    elif transport == "stdio":
        mcp.run()
    else:
        raise SystemExit(
            f"Unknown IMESSAGE_MCP_TRANSPORT {transport!r}. Supported: stdio, http."
        )


if __name__ == "__main__":
    main()
