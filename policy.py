"""Channel matching and mention gating.

Port of the Meshtastic-specific half of MeshClaw's ``src/policy.ts``.

**Deliberately not ported:** ``resolveMeshtasticGroupAccessGate`` and
``resolveMeshtasticGroupSenderAllowed``.  Access control (allowlists,
pairing, DM/group policy) is owned by Hermes — the adapter exposes
``_dm_policy``/``_group_policy`` and the gateway's authorization layer
enforces them.  Re-implementing it here would double-gate and diverge from
every other Hermes platform.

What remains is genuinely Meshtastic-specific: which channel config applies
to a message, and whether an unaddressed channel message should wake the
agent at all.  On a shared radio channel, replying to everything is both
rude and a waste of regulated airtime, so the default is to require a
mention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "ChannelMatch",
    "MentionGate",
    "resolve_channel_match",
    "resolve_require_mention",
    "resolve_mention_gate",
    "strip_mention",
]


@dataclass(frozen=True)
class ChannelMatch:
    """Which per-channel config applies to a message.

    ``config`` is the entry matched by name (exact, then case-insensitive);
    ``wildcard`` is the ``*`` entry when present.  Either may be ``None``.
    """

    config: Optional[Dict[str, Any]]
    wildcard: Optional[Dict[str, Any]]
    matched_name: Optional[str] = None


@dataclass(frozen=True)
class MentionGate:
    """Outcome of the mention gate.

    ``allowed`` is False when the message should be dropped without waking
    the agent.  ``text`` is the message with any addressing prefix removed.
    """

    allowed: bool
    text: str
    was_mentioned: bool = False
    reason: str = ""


def resolve_channel_match(
    channels: Optional[Dict[str, Any]],
    target: Optional[str],
) -> ChannelMatch:
    """Find the per-channel config for *target*.

    Resolution order (from ``policy.ts``): exact name match, then
    case-insensitive match, then the ``*`` wildcard entry.  Meshtastic
    channel names are case-sensitive on the device but users routinely
    mistype the case in config, so the case-insensitive pass is a usability
    affordance rather than a correctness one.
    """
    if not isinstance(channels, dict) or not channels:
        return ChannelMatch(config=None, wildcard=None)

    wildcard = channels.get("*")
    if not isinstance(wildcard, dict):
        wildcard = None if wildcard is None else {}

    if not target:
        return ChannelMatch(config=None, wildcard=wildcard)

    # Exact match.
    if target in channels and target != "*":
        cfg = channels[target]
        return ChannelMatch(
            config=cfg if isinstance(cfg, dict) else {},
            wildcard=wildcard,
            matched_name=target,
        )

    # Case-insensitive match.
    lowered = target.lower()
    for name, cfg in channels.items():
        if name == "*":
            continue
        if isinstance(name, str) and name.lower() == lowered:
            return ChannelMatch(
                config=cfg if isinstance(cfg, dict) else {},
                wildcard=wildcard,
                matched_name=name,
            )

    return ChannelMatch(config=None, wildcard=wildcard, matched_name=None)


def _read_bool(cfg: Optional[Dict[str, Any]], key: str) -> Optional[bool]:
    """Read *key* from *cfg* as a tri-state bool (None = not specified)."""
    if not isinstance(cfg, dict) or key not in cfg:
        return None
    value = cfg[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def resolve_require_mention(
    channel_cfg: Optional[Dict[str, Any]],
    wildcard_cfg: Optional[Dict[str, Any]] = None,
    default: bool = True,
) -> bool:
    """Whether a channel message must address the bot to be dispatched.

    Precedence: the channel's own value wins, then the ``*`` wildcard, then
    *default* — which is ``True``.  Defaulting to "require a mention" is the
    safe choice on a shared radio channel.
    """
    own = _read_bool(channel_cfg, "require_mention")
    if own is not None:
        return own
    wild = _read_bool(wildcard_cfg, "require_mention")
    if wild is not None:
        return wild
    return default


def _mention_patterns(node_name: str) -> Tuple[re.Pattern, ...]:
    """Addressing forms recognised for *node_name*.

    Follows IRC's convention so behaviour is consistent across Hermes
    platforms: ``@name``, ``name:`` and ``name,`` prefixes.
    """
    escaped = re.escape(node_name)
    return (
        re.compile(rf"^\s*@{escaped}\b[\s,:]*", re.IGNORECASE),
        re.compile(rf"^\s*{escaped}\s*[:,]\s*", re.IGNORECASE),
    )


def strip_mention(text: str, node_name: Optional[str]) -> Tuple[str, bool]:
    """Remove an addressing prefix from *text*.

    Returns ``(text_without_mention, was_mentioned)``.  Only a *leading*
    address is stripped; a mid-sentence ``@name`` still counts as a mention
    but is left in place so the agent sees the full message.
    """
    if not text or not node_name:
        return text, False

    for pattern in _mention_patterns(node_name):
        match = pattern.match(text)
        if match:
            return text[match.end():].strip(), True

    # Mentioned but not addressed at the start (e.g. "ping @bot please").
    if re.search(rf"@{re.escape(node_name)}\b", text, re.IGNORECASE):
        return text.strip(), True

    return text, False


def resolve_mention_gate(
    text: str,
    node_name: Optional[str],
    require_mention: bool,
    is_direct: bool = False,
    is_authorized_command: bool = False,
) -> MentionGate:
    """Decide whether a message passes the mention gate.

    DMs always pass — addressing is implicit when someone messages the bot
    directly.  On a channel, when *require_mention* is set the message must
    address the bot, unless it is an authorized command (the bypass carried
    over from ``policy.ts``, so ``/help``-style commands work without the
    ceremony of a mention).
    """
    if is_direct:
        cleaned, mentioned = strip_mention(text, node_name)
        return MentionGate(allowed=True, text=cleaned, was_mentioned=mentioned, reason="dm")

    cleaned, mentioned = strip_mention(text, node_name)

    if not require_mention:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=mentioned, reason="mention_not_required")

    if mentioned:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=True, reason="mentioned")

    if is_authorized_command:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=False, reason="authorized_command")

    return MentionGate(allowed=False, text=cleaned, was_mentioned=False, reason="not_mentioned")
