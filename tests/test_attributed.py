import pytest

from imessage_mcp.attributed import (
    decode_attributed_body,
    decode_transcription,
    message_text,
)

from .support.typedstream_writer import attributed_body


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "",
        "café — naïve ☕",
        "🎲 game night? 👍",
        "x" * 127,
        # 128-145 encode their length differently: a single byte would be read
        # as a reserved tag, so the length has to widen.
        "x" * 128,
        "x" * 145,
        "x" * 146,
        "x" * 300,
        "line one\nline two\ttabbed",
    ],
)
def test_round_trips(text):
    assert decode_attributed_body(attributed_body(text)) == text


def test_empty_body_does_not_return_an_attribute_name():
    """The regression this guards is subtle and silent.

    The archive holds several strings: the text first, then attribute names such
    as ``__kIMMessagePartAttributeName``. Taking the first *non-empty* string
    instead of the first one returns that attribute name as the body of every
    message with no text -- every attachment-only message in the database.
    """
    decoded = decode_attributed_body(attributed_body(""))
    assert decoded == ""
    assert "kIMMessagePart" not in (decoded or "")


def test_missing_and_malformed_blobs_are_not_fatal():
    assert decode_attributed_body(None) is None
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(b"not a typedstream at all") is None
    assert decode_attributed_body(b"\x04\x0bstreamtyped truncated") is None


def test_blob_wins_over_text_column():
    assert message_text("stale", attributed_body("current")) == "current"


def test_falls_back_to_text_column():
    assert message_text("legacy text", None) == "legacy text"
    assert message_text("legacy text", attributed_body("")) == "legacy text"


def test_attachment_placeholder_is_not_text():
    """An attachment-only message decodes to a lone U+FFFC, not to an empty string."""
    assert message_text(None, attributed_body("￼")) is None
    assert message_text(None, attributed_body("￼￼")) is None
    assert message_text(None, attributed_body("�")) is None


def test_attachment_placeholder_is_removed_from_a_caption():
    assert message_text(None, attributed_body("￼\nLook at this")) == "Look at this"
    assert message_text(None, attributed_body("before ￼ after")) == "before  after"


def test_attachment_placeholder_is_removed_from_the_text_column():
    assert message_text("￼", None) is None
    assert message_text("￼old caption", None) == "old caption"


def test_transcription_is_read_from_its_attribute():
    blob = attributed_body("\ufffc", "Picking up milk on the way home")
    assert decode_transcription(blob) == "Picking up milk on the way home"


def test_a_blob_without_a_transcription_has_none():
    assert decode_transcription(attributed_body("hello")) is None
    assert decode_transcription(None) is None
    assert decode_transcription(b"IMAudioTranscription but not a typedstream") is None


def test_a_voice_message_reads_as_its_transcript():
    """Its body is only a placeholder, so the transcript is all the text it has."""
    blob = attributed_body("\ufffc", "Picking up milk on the way home")
    assert message_text(None, blob) == "Picking up milk on the way home"


def test_a_voice_message_not_yet_transcribed_has_no_text():
    assert message_text(None, attributed_body("\ufffc", "")) is None


def test_no_text_anywhere():
    assert message_text(None, None) is None
    assert message_text("", None) is None
    assert message_text(None, attributed_body("")) is None
