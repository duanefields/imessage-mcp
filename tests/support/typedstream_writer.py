"""Build ``attributedBody`` blobs of the shape Messages.app actually writes.

``pytypedstream`` reads typedstreams; nothing writes them, and the fixtures need
blobs that exercise the real decode path. This is a deliberately narrow encoder:
it emits the one archive shape iMessage uses for a plain text message, rather
than a general typedstream writer.

The layout was transcribed byte for byte from a real message on macOS 26.4.1:

    NSAttributedString
      NSString            <- the message text
      int, int            <- attribute run count, run length
      NSDictionary
        NSString          <- "__kIMMessagePartAttributeName"
        NSNumber          <- 0

Shared strings are emitted in the same order as the original, so the
back-reference bytes copied from it (0x92, 0x94, 0x96, ...) resolve to the same
entries. Change the order of anything below and those references break.
"""

# Head-byte tags, from the typedstream format. Values are signed.
_TAG_INTEGER_2 = 0x81
_TAG_INTEGER_4 = 0x82

# Reference bytes copied from a real blob. Valid only because the shared strings
# above them are emitted in the original's order.
_REF_TYPE_OBJECT = b"\x92"  # -> shared string "@"
_REF_CLASS_NSOBJECT = b"\x94"

_ATTRIBUTE_NAME = b"__kIMMessagePartAttributeName"


def _signed_int(value: int) -> bytes:
    """Encode a signed integer as a head byte, widening when it would look like a tag."""
    if -110 <= value <= 127:
        return value.to_bytes(1, "little", signed=True)
    if -0x8000 <= value <= 0x7FFF:
        return bytes([_TAG_INTEGER_2]) + value.to_bytes(2, "little", signed=True)
    return bytes([_TAG_INTEGER_4]) + value.to_bytes(4, "little", signed=True)


def _length(value: int) -> bytes:
    """Encode a string length, which is read unsigned.

    A single byte carries 0-127 and 146-255. The gap is not arbitrary: 128-145
    are the values whose signed reading falls in the reserved tag range, so they
    have to be widened or the reader sees a tag where a length belongs.
    """
    if 0 <= value <= 127 or 146 <= value <= 255:
        return bytes([value])
    if value <= 0x7FFF:
        return bytes([_TAG_INTEGER_2]) + value.to_bytes(2, "little", signed=True)
    return bytes([_TAG_INTEGER_4]) + value.to_bytes(4, "little", signed=True)


def _shared_string(raw: bytes) -> bytes:
    """A literal shared string: NEW, length, bytes. Appends to the reader's table."""
    return b"\x84" + _length(len(raw)) + raw


def _class(name: bytes, version: int) -> bytes:
    return b"\x84" + _shared_string(name) + _signed_int(version)


def attributed_body(text: str) -> bytes:
    """Return an ``attributedBody`` blob carrying ``text``."""
    raw = text.encode("utf-8")

    out = bytearray()
    out += b"\x04\x0bstreamtyped" + _signed_int(1000)
    out += _shared_string(b"@")

    # NSAttributedString : NSObject
    out += b"\x84" + _class(b"NSAttributedString", 0) + _class(b"NSObject", 0) + b"\x85"

    # The text, as an NSString : NSObject.
    out += _REF_TYPE_OBJECT
    out += b"\x84" + _class(b"NSString", 1) + _REF_CLASS_NSOBJECT
    out += _shared_string(b"+") + _length(len(raw)) + raw
    out += b"\x86"

    # One attribute run spanning the whole string. The run length is in bytes,
    # not characters, which is why it is measured on the encoded form.
    out += _shared_string(b"iI") + _signed_int(1) + _signed_int(len(raw))

    # NSDictionary : NSObject, holding one entry.
    out += _REF_TYPE_OBJECT
    out += b"\x84" + _class(b"NSDictionary", 0) + _REF_CLASS_NSOBJECT
    out += _shared_string(b"i") + _signed_int(1)

    # Key: an NSString, by reference to the class emitted above.
    out += _REF_TYPE_OBJECT + b"\x84\x96"
    out += b"\x96" + _length(len(_ATTRIBUTE_NAME)) + _ATTRIBUTE_NAME
    out += b"\x86"

    # Value: NSNumber : NSValue : NSObject, wrapping the integer 0.
    out += _REF_TYPE_OBJECT
    out += b"\x84" + _class(b"NSNumber", 0) + _class(b"NSValue", 0) + _REF_CLASS_NSOBJECT
    out += _shared_string(b"*") + b"\x84\x99"
    out += b"\x99" + _signed_int(0)

    out += b"\x86\x86\x86"
    return bytes(out)
