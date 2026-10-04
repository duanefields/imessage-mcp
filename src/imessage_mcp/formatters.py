"""Render query results as text for the model to read.

The tools return both a text channel and structured content. This module builds
the text half; the dicts from ``db`` are the structured half.
"""

from .contacts import ContactResolver

# A message can legitimately have no text: an attachment on its own, a sticker,
# an expired audio message. That is a fact worth showing, not an error.
NO_TEXT = "[no text]"
ATTACHMENT_ONLY = "[attachment]"


def _label(handle: str | None, resolver: ContactResolver | None) -> str:
    if not handle:
        return "me"
    if resolver is not None:
        return resolver.label_for(handle) or handle
    return handle


def _body(message: dict) -> str:
    text = message.get("text")
    if text:
        return text
    return ATTACHMENT_ONLY if message.get("has_attachments") else NO_TEXT


def format_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return "unknown size"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def chat_title(chat: dict, resolver: ContactResolver | None = None) -> str:
    """A group's name, or the other person's name, or the raw identifier."""
    if chat.get("display_name"):
        return chat["display_name"]
    return _label(chat.get("chat_identifier"), resolver)


def format_chat(chat: dict, resolver: ContactResolver | None = None) -> str:
    parts = [chat_title(chat, resolver)]
    if chat.get("filtered"):
        parts.append(f"[{chat['filtered']}]")
    if chat.get("unread_count"):
        parts.append(f"({chat['unread_count']} unread)")
    lines = [" ".join(parts)]
    if chat.get("last_activity"):
        lines.append(f"  last: {chat['last_activity']}")
    if chat.get("last_message"):
        lines.append(f"  {chat['last_message']}")
    lines.append(f"  guid: {chat['chat_guid']}")
    return "\n".join(lines)


def format_chats(chats: list[dict], resolver: ContactResolver | None = None) -> str:
    if not chats:
        return "No conversations found."
    return "\n\n".join(format_chat(chat, resolver) for chat in chats)


# Long enough to recognize the message being replied to, short enough that a
# thread of replies to one long message does not repeat it in full each time.
REPLY_QUOTE_LIMIT = 80


def _sender(message: dict, resolver: ContactResolver | None) -> str:
    return "me" if message.get("is_from_me") else _label(message.get("handle"), resolver)


def _quote(message: dict) -> str:
    body = _body(message) if "text" in message else "a message no longer here"
    if len(body) > REPLY_QUOTE_LIMIT:
        body = body[: REPLY_QUOTE_LIMIT - 1] + "…"
    return body


def _reaction(reaction: dict, resolver: ContactResolver | None) -> str:
    who = _sender(reaction, resolver)
    if reaction["reaction"] == "emoji" and reaction.get("emoji"):
        return f"{who} {reaction['emoji']}"
    if reaction["reaction"] == "sticker":
        return f"{who} sticker"
    return f"{who} {reaction['reaction']}"


def format_message(message: dict, resolver: ContactResolver | None = None) -> str:
    who = _sender(message, resolver)
    if message.get("filtered"):
        who += f" [{message['filtered']}]"
    when = message.get("date") or "unknown time"
    lines = [f"[{when}] {who}: {_body(message)}"]
    reply_to = message.get("reply_to")
    if reply_to:
        original = _sender(reply_to, resolver) if "text" in reply_to else "someone"
        lines.append(f"  ↳ replying to {original}: {_quote(reply_to)}")
    if message.get("reactions"):
        reactions = ", ".join(_reaction(r, resolver) for r in message["reactions"])
        lines.append(f"  ↳ reactions: {reactions}")
    return "\n".join(lines)


def format_messages(
    messages: list[dict], resolver: ContactResolver | None = None
) -> str:
    if not messages:
        return "No messages found."
    return "\n".join(format_message(message, resolver) for message in messages)


def format_participants(
    participants: list[dict], resolver: ContactResolver | None = None
) -> str:
    if not participants:
        return "No participants found."
    return "\n".join(
        f"{_label(p['handle'], resolver)} ({p['handle']}, {p.get('service') or 'unknown'})"
        for p in participants
    )


def format_attachment(attachment: dict) -> str:
    name = attachment.get("name") or "unnamed"
    direction = "sent" if attachment.get("is_outgoing") else "received"
    kind = attachment.get("mime_type") or attachment.get("uti") or "unknown type"
    when = attachment.get("date") or "unknown time"
    return f"[{when}] {name} — {kind}, {format_size(attachment.get('size_bytes'))}, {direction}"


def format_attachments(attachments: list[dict]) -> str:
    if not attachments:
        return "No attachments found."
    return "\n".join(format_attachment(a) for a in attachments)
