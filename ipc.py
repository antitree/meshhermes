"""Generic local IPC contract for applications using the Meshtastic gateway."""

from __future__ import annotations

from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 200
MAX_REQUEST_BYTES = 8192


def hello_payload(node_id: str | None, channel_name: str, channel_index: int) -> dict[str, Any]:
    return {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "node_id": node_id,
        "channel_name": channel_name,
        "channel_index": channel_index,
    }


def message_payload(inbound: dict[str, Any], channel_name: str, channel_index: int) -> dict[str, Any]:
    return {
        "type": "message",
        "version": PROTOCOL_VERSION,
        "text": str(inbound.get("text", "")),
        "sender_id": str(inbound.get("from_id", "")),
        "message_id": str(inbound.get("message_id", "")),
        "channel_name": channel_name,
        "channel_index": channel_index,
        "is_self": False,
    }


def validate_send_request(
    request: Any,
    *,
    channel_name: str,
    channel_index: int,
    max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> tuple[str | None, str | None]:
    """Return ``(text, error)`` and fail closed on malformed client requests."""
    if not isinstance(request, dict) or request.get("op") != "send":
        return None, "unsupported IPC operation"
    if request.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        return None, "unsupported IPC protocol version"
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "text must be a non-empty string"
    if len(text.encode("utf-8")) > max_bytes:
        return None, f"text exceeds {max_bytes} UTF-8 bytes"
    if request.get("channel_name") != channel_name:
        return None, "channel name is not allowed"
    requested_index = request.get("channel_index")
    if isinstance(requested_index, bool) or not isinstance(requested_index, int):
        return None, "channel index must be an integer"
    if requested_index != channel_index:
        return None, "channel index is not allowed"
    return text.strip(), None
