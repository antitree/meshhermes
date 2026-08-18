"""Node ID and target normalization for Meshtastic.

Port of MeshClaw's ``src/normalize.ts``.  Pure logic — no Hermes or
``meshtastic`` imports, so this module is importable (and testable)
anywhere.

Meshtastic identifies nodes by a 32-bit number.  Users, configs and the
Meshtastic CLI all write that number as ``!`` followed by 8 lowercase hex
digits (e.g. ``!a1b2c3d4``).  This module converts between the two forms
and normalizes the many ways a human might write a messaging target.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "BROADCAST_NUM",
    "BROADCAST_ID",
    "node_num_to_hex",
    "hex_to_node_num",
    "normalize_node_id",
    "looks_like_node_id",
    "normalize_target",
    "normalize_allow_entry",
    "is_channel_target",
    "channel_name_from_target",
]

# Meshtastic broadcast address (0xffffffff): a packet addressed here goes to
# every node on the channel rather than to one node.
BROADCAST_NUM = 0xFFFFFFFF
BROADCAST_ID = "^all"

# A bare hex node id: 1-8 hex digits.  Note this also matches pure-decimal
# strings like "12345678", which is exactly why the disambiguation rule in
# normalize_node_id() exists.
_HEX_RE = re.compile(r"^[0-9a-f]{1,8}$")
_DECIMAL_RE = re.compile(r"^[0-9]+$")

# Prefixes we accept and strip from a target string.  ``meshtastic:`` is the
# platform qualifier used by Hermes' send_message tool; ``user:`` and
# ``node:`` are MeshClaw-compatible spellings.
_TARGET_PREFIXES = ("meshtastic:", "user:", "node:")
_CHANNEL_PREFIX = "channel:"


def node_num_to_hex(num: int) -> str:
    """Convert a numeric node id to its canonical ``!aabbccdd`` form.

    >>> node_num_to_hex(2882400001)
    '!abcd0001'

    Raises ``ValueError`` when *num* is not a valid 32-bit node number.
    """
    if isinstance(num, bool) or not isinstance(num, int):
        raise ValueError(f"node number must be an int, got {type(num).__name__}")
    if num < 0 or num > 0xFFFFFFFF:
        raise ValueError(f"node number out of 32-bit range: {num}")
    return f"!{num:08x}"


def hex_to_node_num(value: str) -> int:
    """Convert ``!aabbccdd`` (or bare ``aabbccdd``) to its numeric form.

    Inverse of :func:`node_num_to_hex`.  Raises ``ValueError`` on garbage.
    """
    if not isinstance(value, str):
        raise ValueError(f"node id must be a str, got {type(value).__name__}")
    cleaned = value.strip().lower()
    if cleaned.startswith("!"):
        cleaned = cleaned[1:]
    if not cleaned or not _HEX_RE.match(cleaned):
        raise ValueError(f"not a valid hex node id: {value!r}")
    return int(cleaned, 16)


def normalize_node_id(value: object) -> Optional[str]:
    """Normalize any accepted node-id spelling to canonical ``!aabbccdd``.

    Accepts:

    - ``!abcd0001``       — canonical hex, the common case
    - ``ABCD0001``        — bare hex, any case
    - ``2882400001``      — decimal node number

    Returns ``None`` when *value* cannot be interpreted as a node id.

    **The disambiguation rule** (carried over from MeshClaw verbatim,
    because getting it wrong silently misroutes DMs): a ``!``-prefixed
    string is *always* hex.  A bare string is treated as hex only when it
    matches ``^[0-9a-f]{1,8}$`` **and** is not all-decimal; an all-decimal
    bare string is parsed as a decimal node number.  So ``"12345678"`` is
    the decimal node 12345678, while ``"!12345678"`` and ``"abcd0001"``
    are hex.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            return node_num_to_hex(value)
        except ValueError:
            return None
    if not isinstance(value, str):
        return None

    cleaned = value.strip().lower()
    if not cleaned:
        return None

    # Explicit "!" marker: unambiguously hex.
    if cleaned.startswith("!"):
        try:
            return node_num_to_hex(hex_to_node_num(cleaned))
        except ValueError:
            return None

    # Bare string: decimal wins over hex when it could be either.
    if _DECIMAL_RE.match(cleaned):
        try:
            return node_num_to_hex(int(cleaned, 10))
        except ValueError:
            return None

    if _HEX_RE.match(cleaned):
        try:
            return node_num_to_hex(int(cleaned, 16))
        except ValueError:
            return None

    return None


def looks_like_node_id(value: object) -> bool:
    """True when *value* can be interpreted as a Meshtastic node id."""
    return normalize_node_id(value) is not None


def is_channel_target(value: object) -> bool:
    """True when *value* is an explicit ``channel:<name>`` target."""
    if not isinstance(value, str):
        return False
    return value.strip().lower().startswith(_CHANNEL_PREFIX)


def channel_name_from_target(value: str) -> Optional[str]:
    """Extract the channel name from a ``channel:<name>`` target.

    Case is preserved — Meshtastic channel names are case-sensitive on the
    device even though we match them case-insensitively.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.lower().startswith(_CHANNEL_PREFIX):
        name = stripped[len(_CHANNEL_PREFIX):].strip()
        return name or None
    return None


def normalize_target(value: object) -> Optional[str]:
    """Normalize a messaging target to a node id or ``channel:<name>``.

    Strips the ``meshtastic:``/``user:``/``node:`` qualifiers, resolves
    node ids to canonical ``!hex`` form, and passes channel targets through
    as ``channel:<name>``.  A bare non-node-id string is treated as a
    channel name, which is what makes ``target="LongFast"`` work.

    Returns ``None`` for empty or uninterpretable input.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return normalize_node_id(value)
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    # Strip platform/user qualifiers, possibly stacked ("meshtastic:user:!ab").
    changed = True
    while changed:
        changed = False
        low = stripped.lower()
        for prefix in _TARGET_PREFIXES:
            if low.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                changed = True
                break

    if not stripped:
        return None

    # Explicit channel target.
    if stripped.lower().startswith(_CHANNEL_PREFIX):
        name = channel_name_from_target(stripped)
        return f"{_CHANNEL_PREFIX}{name}" if name else None

    # Broadcast spellings resolve to the broadcast node id.
    if stripped.lower() in (BROADCAST_ID, "all", "broadcast"):
        return BROADCAST_ID

    node = normalize_node_id(stripped)
    if node is not None:
        return node

    # Anything else is a bare channel name.
    return f"{_CHANNEL_PREFIX}{stripped}"


def normalize_allow_entry(value: object) -> Optional[str]:
    """Normalize an allowlist entry for comparison.

    Allowlists may contain node ids (in any spelling), ``channel:`` targets,
    or the ``*`` wildcard.  Node ids are canonicalized so ``ABCD0001``,
    ``!abcd0001`` and ``2882400001`` all compare equal.
    """
    if not isinstance(value, str):
        if isinstance(value, int) and not isinstance(value, bool):
            return normalize_node_id(value)
        return None

    stripped = value.strip().lower()
    if not stripped:
        return None
    if stripped == "*":
        return "*"
    return normalize_target(stripped)
