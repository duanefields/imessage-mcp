"""Send messages by driving Messages.app.

This is the only part of the server that is not read-only, and a send cannot be
recalled. The safety property that matters is enforced above this layer: the
server only ever passes a chat guid that already exists in the database, so a
wrong or invented phone number has no path to a stranger.
"""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"

# The message text and the chat id are passed as arguments rather than pasted
# into the script, so there is no escaping to get wrong and no injection
# surface. Verified that argv survives quotes, backslashes, $(...) and
# backticks untouched.
#
# `chat id` is the guid, the same string the database calls chat.guid.
_SEND_SCRIPT = """
on run argv
    set chatId to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        send messageText to chat id chatId
    end tell
end run
"""

# Generous, because this is not waiting on the network. It is the ceiling on the
# consent-prompt hang described below.
DEFAULT_TIMEOUT_SECONDS = 30


# The outcome of the most recent send, for ``/health`` to report.
#
# Reads and sends fail independently, and only one of them is visible. Every
# read comes off chat.db, so a host whose Apple Events grant for Messages has
# been revoked -- from System Settings, or by the interpreter moving -- serves
# every read correctly and drops every send. `messages_running` does not catch
# it: Messages is running, it just will not take orders.
_last_send: dict = {"at": None, "ok": None, "action": None, "error": None}


def last_send() -> dict:
    """A copy of the most recent send's outcome."""
    return dict(_last_send)


def reset_last_send() -> None:
    """Forget the last send. For tests, which must not leak state into each other."""
    _last_send.update({"at": None, "ok": None, "action": None, "error": None})


def _publishable_failure(exc: Exception) -> str:
    """Describe a send failure in terms safe to serve from ``/health``.

    Only the exception's class name, and here that matters more than anywhere
    else in this project. ``/health`` is unauthenticated and
    ``scripts/healthcheck.sh`` forwards what it finds to a ping service off the
    host -- while ``SendError`` carries osascript's stderr, and the argv
    osascript was given is ``[chat_guid, text]``. The text is the message. A
    published message means a private conversation leaves the machine over the
    open internet because somebody mistyped a chat guid.

    The class name is what an operator needs anyway: ``SendTimeout`` means the
    Apple Events consent dialog is unanswered, ``SendError`` means Messages
    refused. The full exception still reaches the log, which is local.
    """
    return type(exc).__name__


def _record(ok: bool, exc: Exception | None = None) -> None:
    _last_send.update(
        {
            "at": time.time(),
            "ok": ok,
            "action": "send_message",
            "error": _publishable_failure(exc) if exc is not None else None,
        }
    )


class SendError(Exception):
    """A send did not go out."""


class SendTimeout(SendError):
    """Messages did not answer, most likely waiting on a consent dialog."""


def send_to_chat(
    chat_guid: str, text: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> None:
    """Send ``text`` to an existing chat. Raises ``SendError`` if it did not go.

    The timeout is not defensive padding. Controlling Messages needs Apple
    Events permission, and the first attempt raises a consent dialog that
    **blocks until somebody clicks it**. On an unattended host nobody does, so
    without a timeout the tool call hangs forever and the client eventually
    gives up with no explanation. Failing after 30 seconds with a message that
    names the real cause is far more useful.
    """
    # Deliberately not recorded below. An empty message is a caller's mistake
    # caught before Messages is consulted, not a sign that this host can no
    # longer send -- recording it would page somebody over a model's bad
    # argument.
    if not text:
        raise SendError("Refusing to send an empty message.")

    try:
        result = subprocess.run(
            [OSASCRIPT, "-", chat_guid, text],
            input=_SEND_SCRIPT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        failure = SendTimeout(
            f"Messages did not respond within {timeout:.0f}s. This is usually the "
            "macOS consent dialog asking to control Messages, which blocks until "
            "someone answers it at the machine. Grant it there once, then retry."
        )
        _record(False, failure)
        raise failure from exc
    except OSError as exc:
        failure = SendError(f"Could not run osascript: {exc}")
        _record(False, failure)
        raise failure from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit status {result.returncode}"
        failure = SendError(f"Messages refused the send: {detail}")
        _record(False, failure)
        raise failure

    # Records that Messages accepted the send, which is what this layer can
    # know. Whether it was then delivered is a separate question the server
    # answers per call by reading the message back out of the database; a send
    # that is merely unconfirmed is explicitly not treated as a failure there,
    # so it must not become one here either.
    _record(True)
