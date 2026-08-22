"""Recover message text from the ``attributedBody`` blob.

On a current macOS the ``message.text`` column is almost always empty and the
body lives in ``attributedBody``, an NeXTSTEP typedstream archive of an
``NSAttributedString``. Measured on a real database: 121,793 of 123,023 messages
had no usable ``text``. Reading that column first is the single easiest way to
build something that looks like it works and returns nothing for 99% of rows.
"""

import logging

from typedstream.stream import TypedStreamReader

logger = logging.getLogger(__name__)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Return the message text held in ``blob``, or ``None`` if there is none.

    The archive yields several byte strings. The first is the message text; the
    ones after it are attribute names such as ``__kIMMessagePartAttributeName``.
    Take the **first** one even when it is empty -- skipping to the first
    non-empty string returns an attribute name as the body of every message that
    has no text, which is exactly the attachment-only case.
    """
    if not blob:
        return None

    try:
        for event in TypedStreamReader.from_data(blob):
            if isinstance(event, bytes):
                return event.decode("utf-8", errors="replace")
    except Exception:
        # The blob is opaque binary written by another application; a parse
        # failure is a fact about the data, not a bug to propagate. One
        # unreadable message must not fail the whole listing.
        logger.debug("could not decode attributedBody", exc_info=True)
        return None

    return None


def message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Return the best available text for a message, or ``None`` if it has none.

    The blob wins over the column. A message can carry both, and when it does
    the blob is the one Messages renders.
    """
    decoded = decode_attributed_body(attributed_body)
    if decoded:
        return decoded
    return text or None
