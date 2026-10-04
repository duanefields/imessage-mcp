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


# Messages marks where an attachment sits in the body with U+FFFC and where an
# app balloon sits with U+FFFD. An attachment-only message is not empty: its
# text is a lone U+FFFC. Measured on a real database, 4,024 messages decoded to
# nothing but placeholders and 2,501 more carried one alongside their text.
_PLACEHOLDERS = ("￼", "�")


def _without_placeholders(text: str | None) -> str | None:
    if not text or not any(p in text for p in _PLACEHOLDERS):
        return text
    for placeholder in _PLACEHOLDERS:
        text = text.replace(placeholder, "")
    # The placeholder usually sits on its own line above a caption, so removing
    # it leaves the newline behind.
    return text.strip()


def message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Return the best available text for a message, or ``None`` if it has none.

    The blob wins over the column. A message can carry both, and when it does
    the blob is the one Messages renders. Attachment placeholders are removed
    from either, so an attachment-only message has no text rather than an
    invisible character.
    """
    for candidate in (decode_attributed_body(attributed_body), text):
        cleaned = _without_placeholders(candidate)
        if cleaned:
            return cleaned
    return None
