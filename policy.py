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
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ChannelMatch",
    "MentionGate",
    "resolve_channel_match",
    "resolve_require_mention",
    "resolve_mention_gate",
    "mention_trigger_names",
    "strip_mention",
    "MIN_SHORT_NAME_LENGTH",
    "SHORT_NAME_STOPWORDS",
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


#: Short names below this many characters are ignored as mention triggers.
#: Meshtastic short names are ~4 characters and frequently cryptic, so a
#: 1-2 character name ("A", "K1") would fire on ordinary channel chatter.
#: Raise or lower it if your mesh disagrees.
MIN_SHORT_NAME_LENGTH = 3

#: Short names that are ordinary English words or common mesh chatter and
#: would wake the agent constantly.  Compared case-insensitively.
SHORT_NAME_STOPWORDS = frozenset(
    {
        "the", "and", "you", "yes", "no", "ok", "okay", "hi", "hey", "yo",
        "all", "any", "for", "not", "who", "how", "why", "what", "when",
        "sos", "cq", "qth", "test", "ping", "hello",
    }
)


def _mention_pattern(name: str) -> re.Pattern:
    """The addressing form recognised for *name*.

    Meshtastic has no mention protocol.  Addressing a node on a channel is
    just typing its name at the start of the message, so that is what this
    matches: the name literally at position 0, case-insensitive, with an
    optional leading ``@`` and optional trailing punctuation.  A trailing
    word boundary keeps ``hermesbot`` from matching ``hermes``.
    """
    escaped = re.escape(name.strip())
    return re.compile(rf"^\s*@?{escaped}(?![\w])[\s]*[:,]?[\s]*", re.IGNORECASE)


def _is_usable_short_name(short_name: Optional[str], long_name: Optional[str]) -> bool:
    """Whether *short_name* is safe to use as a mention trigger.

    Short names collide with ordinary words far more readily than long
    names do, and a false trigger costs real airtime.  A short name is
    rejected when it is empty/whitespace, shorter than
    ``MIN_SHORT_NAME_LENGTH``, or a known stopword.  It is also skipped
    when it is identical to the long name, since the long-name pattern
    already covers it.
    """
    if not isinstance(short_name, str):
        return False
    stripped = short_name.strip()
    if not stripped:
        return False
    if len(stripped) < MIN_SHORT_NAME_LENGTH:
        return False
    if stripped.lower() in SHORT_NAME_STOPWORDS:
        return False
    if isinstance(long_name, str) and stripped.lower() == long_name.strip().lower():
        return False
    return True


def mention_trigger_names(
    node_name: Optional[str],
    short_name: Optional[str] = None,
    extra_names: Tuple[Optional[str], ...] = (),
) -> Tuple[str, ...]:
    """Every name that addressing the bot may use, longest first.

    Longest-first matters: when a node's long name starts with its short
    name, matching the long name first strips more of the prefix.  Empty
    and duplicate names are dropped.
    """
    candidates: List[str] = []
    for value in (node_name, *extra_names):
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    if _is_usable_short_name(short_name, node_name):
        candidates.append(short_name.strip())  # type: ignore[union-attr]

    seen = set()
    unique: List[str] = []
    for name in sorted(candidates, key=len, reverse=True):
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return tuple(unique)


def strip_mention(
    text: str,
    node_name: Optional[str],
    short_name: Optional[str] = None,
    extra_names: Tuple[Optional[str], ...] = (),
) -> Tuple[str, bool]:
    """Remove a leading address from *text*.

    Returns ``(text_without_mention, was_mentioned)``.  Only a *leading*
    address counts — a name appearing mid-sentence is ordinary conversation
    ("tell Long Name of Node I said hi"), not an instruction to the bot.
    """
    if not text:
        return text, False

    for name in mention_trigger_names(node_name, short_name, extra_names):
        match = _mention_pattern(name).match(text)
        if match:
            return text[match.end():].strip(), True

    return text, False


def resolve_mention_gate(
    text: str,
    node_name: Optional[str],
    require_mention: bool,
    is_direct: bool = False,
    is_authorized_command: bool = False,
    short_name: Optional[str] = None,
    extra_names: Tuple[Optional[str], ...] = (),
) -> MentionGate:
    """Decide whether a message passes the mention gate.

    DMs always pass — addressing is implicit when someone messages the bot
    directly.  On a channel, when *require_mention* is set the message must
    address the bot, unless it is an authorized command (the bypass carried
    over from ``policy.ts``, so ``/help``-style commands work without the
    ceremony of a mention).
    """
    if is_direct:
        cleaned, mentioned = strip_mention(text, node_name, short_name, extra_names)
        return MentionGate(allowed=True, text=cleaned, was_mentioned=mentioned, reason="dm")

    cleaned, mentioned = strip_mention(text, node_name, short_name, extra_names)

    if not require_mention:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=mentioned, reason="mention_not_required")

    if mentioned:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=True, reason="mentioned")

    if is_authorized_command:
        return MentionGate(allowed=True, text=cleaned, was_mentioned=False, reason="authorized_command")

    return MentionGate(allowed=False, text=cleaned, was_mentioned=False, reason="not_mentioned")
