"""Send messages by driving Messages.app.

This is the only part of the server that is not read-only, and a send cannot be
recalled. The safety property that matters is enforced above this layer: the
server only ever passes a chat guid that already exists in the database, so a
wrong or invented phone number has no path to a stranger.
"""

import logging
import subprocess

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
        raise SendTimeout(
            f"Messages did not respond within {timeout:.0f}s. This is usually the "
            "macOS consent dialog asking to control Messages, which blocks until "
            "someone answers it at the machine. Grant it there once, then retry."
        ) from exc
    except OSError as exc:
        raise SendError(f"Could not run osascript: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit status {result.returncode}"
        raise SendError(f"Messages refused the send: {detail}")
