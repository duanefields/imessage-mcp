"""Remember which conversation the model was shown text from.

All three legs of the lethal trifecta are in this one server: the archive is
private data, the read tools carry text written by anybody who can text this
account, and ``send_message`` reaches back out. So injected text in an incoming
message can tell the model to relay something out of a *different*
conversation, and every check ``send_message`` already makes still passes --
the attacker's chat exists, the text is not empty, Messages is running. The
existing rule bounds who can be reached, not what is said to them.

This module supports the missing half: text read from conversation A may not be
sent to conversation B unless the caller states that the person operating it
asked for the forward.

It is a floor, not a proof. Verbatim relaying is caught, and so are the short
payloads worth stealing on their own -- a verification code, a phone number, an
email address, a link -- even when they are dropped into a sentence the
attacker wrote. A paraphrase composed from memory is not caught, because
nothing in the text ties it back to where it came from. Marking message text as
untrusted in the tool output is the other half of the defense, and neither half
is sufficient alone.
"""

import collections
import re
from collections.abc import Iterable

# How many message bodies to remember. Bounded on purpose: this is a
# long-running process, and the log is only useful for text the model has
# recently been shown. Remembering everything ever read would drift toward
# refusing to send anything the user has ever said.
_LOG_SIZE = 500

# The shortest run of words treated as reproduced content. Short enough to
# catch a relayed sentence, long enough that "on my way, see you soon" turning
# up in two conversations is not by itself suspicious. A false positive costs
# one confirmation; a false negative is a leak.
_SHINGLE_WORDS = 5

# One log for the process, shared by every session. That is right for a
# single-user server: the thing being protected is one person's archive, and a
# second session is the same person, or an attacker who should not benefit from
# opening one.
_read_log: collections.deque[tuple[str, str]] = collections.deque(maxlen=_LOG_SIZE)

_WORDS = re.compile(r"[^a-z0-9]+")
# Digits split by spaces, dots, dashes or brackets are one number: a phone
# number typed six ways is still the same phone number.
_DIGIT_GLUE = re.compile(r"(?<=\d)[\s.()\-–—]+(?=\d)")
_DIGIT_RUN = re.compile(r"\d{5,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[\w]")
_URL = re.compile(r"https?://\S+")


def record(entries: Iterable[tuple[str | None, str | None]]) -> None:
    """Note that ``text`` from ``chat_guid`` was shown to the model.

    Takes pairs rather than message dicts because the callers hold three
    different shapes: a chat_guid the tool was given, a chat_guid carried on
    each row, and a chat's last-message preview.
    """
    for chat_guid, text in entries:
        if chat_guid and text and text.strip():
            _read_log.append((chat_guid, text))


def reset() -> None:
    """Forget everything read so far. For tests."""
    _read_log.clear()


def _shingles(text: str) -> set[str]:
    words = [word for word in _WORDS.split(text.casefold()) if word]
    if len(words) < _SHINGLE_WORDS:
        return set()
    return {
        " ".join(words[i : i + _SHINGLE_WORDS])
        for i in range(len(words) - _SHINGLE_WORDS + 1)
    }


def _distinctive(text: str) -> set[str]:
    """Short strings that are worth stealing on their own.

    A verification code is five words long and would clear no shingle, but it
    is the single most valuable thing in the archive. These survive being
    quoted into a sentence the attacker composed, which is how a relay of a
    code actually looks.
    """
    lowered = text.casefold()
    found = set(_EMAIL.findall(lowered)) | set(_URL.findall(lowered))
    return found | set(_DIGIT_RUN.findall(_DIGIT_GLUE.sub("", lowered)))


def _fingerprints(text: str) -> set[str]:
    return _shingles(text) | _distinctive(text)


def cross_chat_sources(chat_guid: str, text: str) -> list[str]:
    """Chats other than ``chat_guid`` whose content ``text`` reproduces.

    Empty means nothing read from elsewhere is being repeated, which is the
    ordinary case for a reply written from what the user said.
    """
    wanted = _fingerprints(text)
    if not wanted:
        return []

    sources: list[str] = []
    for source_guid, source_text in _read_log:
        if source_guid == chat_guid or source_guid in sources:
            continue
        if wanted & _fingerprints(source_text):
            sources.append(source_guid)
    return sources
