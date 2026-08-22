"""Shared outbound-send authorization.

There are three ways a message reaches the radio, and all three must be
gated the same way:

1. ``adapter.send()``          — gateway replies to an inbound message
2. ``mesh_send_handler()``     — the agent calling the mesh_send tool
3. ``_standalone_send()``      — cron / ``send_message`` when the gateway
                                 is not co-resident with the caller

Path 3 is reachable from ``tools/send_message_tool.py``, i.e. from the same
agent tool surface as path 2.  Keeping the policy check in only one of them
would let an agent bypass every gate by choosing the other tool, so the
decision lives here and every caller uses it.

There is also a second class of gate here: **loop prevention**.  With
``require_mention`` false a bot replies to everything on a channel, so two
Hermes bots sharing a channel will answer each other forever, burning
airtime that is legally shared.  Three independent controls bound that,
each sufficient on its own (see README "Running more than one bot"):

* a per-channel **conversation cooldown** (on, 60s) — evaluated at the
  reply decision, so it also saves the agent round-trip;
* **loop-signature detection** (off by default) — refuses to answer the
  same text twice on a channel;
* the **hard rate limit** above — the last-resort backstop on the wire.

The cooldown and the signature cache are evaluated at the *reply decision*
(inbound, before the agent wakes) but their state lives here so that every
send path can mark a channel as recently-answered.  A control that only
some paths honoured would be bypassable exactly the way the send gate
would be.

Pure logic: no Hermes or ``meshtastic`` imports.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

try:
    from .normalize import (
        channel_name_from_target,
        is_channel_target,
        normalize_allow_entry,
        normalize_node_id,
    )
except ImportError:  # pragma: no cover - direct-import context
    from normalize import (  # type: ignore[no-redef]
        channel_name_from_target,
        is_channel_target,
        normalize_allow_entry,
        normalize_node_id,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "COOLDOWN_EXEMPT_MENTIONS",
    "DEFAULT_CONVERSATION_COOLDOWN_SECONDS",
    "DEFAULT_LOOP_SIGNATURE_MAX_ENTRIES",
    "DEFAULT_LOOP_SIGNATURE_TTL_SECONDS",
    "DEFAULT_RATE_LIMIT_MAX_SENDS",
    "DEFAULT_RATE_LIMIT_WINDOW_SECONDS",
    "RATE_LIMIT_MAX_SENDS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "SendPolicy",
    "allow_all_enabled",
    "build_allowlist",
    "check_send_permitted",
    "conversation_cooldown_seconds",
    "cooldown_exempt_mentions",
    "cooldown_ok",
    "loop_detection_enabled",
    "loop_signature",
    "loop_signature_count",
    "loop_signature_max_entries",
    "loop_signature_seen",
    "loop_signature_ttl_seconds",
    "note_channel_reply",
    "rate_limit_max_sends",
    "rate_limit_ok",
    "rate_limit_window_seconds",
    "reset_loop_state",
    "reset_rate_limit",
]

# Airtime is a shared, legally regulated resource: the agent must not be
# able to flood it even if it decides sending is a good idea.
#
# These are the *defaults*.  The effective values are read per call from the
# environment by rate_limit_max_sends() / rate_limit_window_seconds(), so an
# operator override takes effect without a reimport.  The bare names below
# stay part of the published API because mesh_tools aliases them and tests
# read them for their error messages.
DEFAULT_RATE_LIMIT_MAX_SENDS = 5
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60.0

#: Conversation cooldown: after the bot replies on a channel it stays quiet
#: on that channel for this long.  This is the control that actually breaks
#: a two-bot loop, so it is ON by default.
DEFAULT_CONVERSATION_COOLDOWN_SECONDS = 60.0

#: When True, a message that explicitly mentions the bot bypasses the
#: conversation cooldown.  Deliberately **False**: the strict reading was
#: chosen so that two bots which address each other by name cannot ping-pong
#: straight through the cooldown.  The cost, accepted knowingly, is that a
#: human who mentions the bot inside the cooldown window is silently
#: ignored.  Flip it with MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS=true.
COOLDOWN_EXEMPT_MENTIONS = False

#: Loop-signature cache bounds.  This is a long-running gateway process, so
#: the cache must never grow without limit: entries expire after the TTL and
#: the oldest are evicted once the cap is reached.
DEFAULT_LOOP_SIGNATURE_TTL_SECONDS = 600.0
DEFAULT_LOOP_SIGNATURE_MAX_ENTRIES = 256

# Indirection so tests can inject a clock without monkeypatching the real
# time module out from under asyncio.  Always call this, never time.monotonic.
_monotonic = time.monotonic

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _env_number(name: str, default: float, *, integer: bool = False) -> float:
    """Read a numeric override, falling back to *default* with a warning.

    A malformed value must never crash the gateway or, worse, be read as
    "no limit" — an operator typo here would uncap the radio.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip()) if integer else float(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "Meshtastic: %s=%r is not a number - using the default (%s)",
            name, raw, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "Meshtastic: %s=%r must be positive - using the default (%s)",
            name, raw, default,
        )
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    low = raw.strip().lower()
    if low in _TRUTHY:
        return True
    if low in _FALSY:
        return False
    logger.warning(
        "Meshtastic: %s=%r is not a boolean - using the default (%s)",
        name, raw, default,
    )
    return default


def rate_limit_max_sends() -> int:
    """Effective hard cap on sends per window."""
    return int(_env_number(
        "MESHTASTIC_RATE_LIMIT_MAX_SENDS",
        float(DEFAULT_RATE_LIMIT_MAX_SENDS),
        integer=True,
    ))


def rate_limit_window_seconds() -> float:
    """Effective hard-limit window, in seconds."""
    return float(_env_number(
        "MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS",
        DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    ))


class _DynamicNumber:
    """Mixin for a number that re-reads its env override on every use.

    ``RATE_LIMIT_MAX_SENDS`` is published API: ``mesh_tools`` aliases it and
    the tests read it.  Making the override dynamic while leaving a plain
    int here would freeze the value at import time and make every one of
    those readers silently wrong.  Subclassing the builtin keeps
    ``int(x)``/``float(x)``, arithmetic and formatting working, while the
    comparison and display methods reflect the current setting.
    """

    __slots__ = ()

    def _now(self):
        return self._reader()  # type: ignore[attr-defined]

    def __int__(self): return int(self._now())
    # NOTE: CPython reads the *stored* value for a builtin int subclass in
    # some C fast paths (range(), list indexing), so __index__ cannot be
    # relied on to reflect an override.  Anywhere the live value matters,
    # call rate_limit_max_sends() / rate_limit_window_seconds() directly;
    # these attributes exist for display and for equality in tests.
    def __index__(self): return int(self._now())
    def __float__(self): return float(self._now())
    def __repr__(self): return repr(self._now())
    def __str__(self): return str(self._now())
    def __format__(self, spec): return format(self._now(), spec)
    def __eq__(self, other): return self._now() == other
    def __ne__(self, other): return self._now() != other
    def __lt__(self, other): return self._now() < other
    def __le__(self, other): return self._now() <= other
    def __gt__(self, other): return self._now() > other
    def __ge__(self, other): return self._now() >= other
    def __hash__(self): return hash(self._now())
    def __add__(self, other): return self._now() + other
    def __radd__(self, other): return other + self._now()
    def __sub__(self, other): return self._now() - other
    def __rsub__(self, other): return other - self._now()
    def __mul__(self, other): return self._now() * other
    def __rmul__(self, other): return other * self._now()
    def __bool__(self): return bool(self._now())


class _DynamicInt(_DynamicNumber, int):
    def __new__(cls, reader):
        obj = super().__new__(cls, int(reader()))
        obj._reader = reader
        return obj


class _DynamicFloat(_DynamicNumber, float):
    def __new__(cls, reader):
        obj = super().__new__(cls, float(reader()))
        obj._reader = reader
        return obj


#: Live views of the effective limits.  Read them like plain numbers.
RATE_LIMIT_MAX_SENDS = _DynamicInt(rate_limit_max_sends)
RATE_LIMIT_WINDOW_SECONDS = _DynamicFloat(rate_limit_window_seconds)

_rate_lock = threading.Lock()
_send_times: List[float] = []


def rate_limit_ok() -> bool:
    """Token bucket over a sliding window.  Shared by every send path.

    This is the backstop that holds even when the cooldown and the
    signature cache are both disabled or misconfigured: nothing reaches the
    radio through any path without passing here first.
    """
    max_sends = rate_limit_max_sends()
    window = rate_limit_window_seconds()
    now = _monotonic()
    with _rate_lock:
        cutoff = now - window
        _send_times[:] = [t for t in _send_times if t > cutoff]
        if len(_send_times) >= max_sends:
            logger.info(
                "Meshtastic: suppressed by rate limit (%d sends per %.0fs "
                "already used) - airtime is a shared, regulated resource",
                max_sends, window,
            )
            return False
        _send_times.append(now)
        return True


def reset_rate_limit() -> None:
    """Test helper - clears the sliding window."""
    with _rate_lock:
        _send_times.clear()


# -- Loop prevention: shared state ----------------------------------------
#
# All of the state below is touched from both the radio receive thread (via
# the adapter's pubsub callbacks) and the event loop, so it lives under a
# plain threading.Lock.  Every critical section here is a few dict/deque
# operations with no I/O and no ``await``, so holding a threading lock from
# async code is safe: it can never block long enough to stall the loop, and
# it is never held across a suspension point.

_loop_lock = threading.Lock()
_last_reply_at: Dict[str, float] = {}
_seen_signatures: "OrderedDict[str, float]" = OrderedDict()


def _channel_key(channel: Optional[str]) -> str:
    return (channel or "").strip().lower()


# -- Loop prevention: conversation cooldown -------------------------------


def conversation_cooldown_seconds() -> float:
    """Effective cooldown.  ``<= 0`` disables the control entirely."""
    raw = os.getenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_CONVERSATION_COOLDOWN_SECONDS
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "Meshtastic: MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS=%r is not a "
            "number - using the default (%s)",
            raw, DEFAULT_CONVERSATION_COOLDOWN_SECONDS,
        )
        return DEFAULT_CONVERSATION_COOLDOWN_SECONDS
    # Zero or negative means "disabled" - an explicit, documented opt-out
    # rather than a validation failure, because an operator who has read the
    # README may genuinely want it off on a private channel.
    return value


def cooldown_exempt_mentions() -> bool:
    """Whether an explicit mention bypasses the conversation cooldown."""
    return _env_bool("MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS", COOLDOWN_EXEMPT_MENTIONS)


def cooldown_ok(channel: Optional[str], *, was_mentioned: bool = False) -> bool:
    """Whether a reply on *channel* is allowed by the conversation cooldown.

    Read-only: it does not start a new cooldown.  ``note_channel_reply()``
    does that, and is called when a reply actually goes out.
    """
    cooldown = conversation_cooldown_seconds()
    if cooldown <= 0:
        return True
    if was_mentioned and cooldown_exempt_mentions():
        return True
    key = _channel_key(channel)
    if not key:
        return True
    now = _monotonic()
    with _loop_lock:
        last = _last_reply_at.get(key)
        if last is None:
            return True
        remaining = cooldown - (now - last)
        if remaining <= 0:
            # Expired; drop the entry so the map does not accumulate a row
            # per channel ever spoken on.
            _last_reply_at.pop(key, None)
            return True
    logger.info(
        "Meshtastic: suppressed reply on channel %r - conversation cooldown "
        "active (%.1fs of %.0fs remaining%s)",
        key, remaining, cooldown,
        "; the mention did not exempt it" if was_mentioned else "",
    )
    return False


def note_channel_reply(channel: Optional[str]) -> None:
    """Record that the bot just spoke on *channel*, starting its cooldown.

    Called from every outbound path, not only the reply path: a cooldown
    that only ``adapter.send()`` refreshed could be walked around by an
    agent choosing ``mesh_send`` instead.
    """
    key = _channel_key(channel)
    if not key:
        return
    with _loop_lock:
        _last_reply_at[key] = _monotonic()


# -- Loop prevention: message-signature detection -------------------------


def loop_detection_enabled() -> bool:
    """Whether signature detection is on.  Off unless explicitly enabled."""
    return _env_bool("MESHTASTIC_LOOP_DETECTION", False)


def loop_signature_ttl_seconds() -> float:
    return float(_env_number(
        "MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS",
        DEFAULT_LOOP_SIGNATURE_TTL_SECONDS,
    ))


def loop_signature_max_entries() -> int:
    return int(_env_number(
        "MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES",
        float(DEFAULT_LOOP_SIGNATURE_MAX_ENTRIES),
        integer=True,
    ))


def loop_signature(channel: Optional[str], text: str) -> str:
    """The identity of a message for repeat detection.

    Deliberately ``(channel, normalized_text)`` and **not** the sender: in
    the loop this control exists to break, bot A and bot B are different
    senders saying the same thing, so including the sender would defeat it
    outright.  Excluding it means a second human repeating a phrase on the
    same channel is also ignored - acceptable, and the reason this control
    is opt-in rather than on by default.

    Normalization folds case and collapses whitespace runs, so that
    re-wrapping across LoRa chunk boundaries does not mint a new signature.
    It stops there: anything more aggressive (stripping punctuation, say)
    would start conflating genuinely different messages.
    """
    normalized = " ".join((text or "").split()).lower()
    return _channel_key(channel) + "\x00" + normalized


def _evict_locked(now: float) -> None:
    """Drop expired and surplus entries.  Caller holds ``_loop_lock``."""
    cutoff = now - loop_signature_ttl_seconds()
    while _seen_signatures:
        oldest_key = next(iter(_seen_signatures))
        if _seen_signatures[oldest_key] <= cutoff:
            _seen_signatures.pop(oldest_key)
        else:
            break
    # Hard cap regardless of TTL: this process runs for months, and a busy
    # channel could fill memory well inside a single TTL window.
    max_entries = loop_signature_max_entries()
    while len(_seen_signatures) > max_entries:
        _seen_signatures.popitem(last=False)


def loop_signature_seen(channel: Optional[str], text: str) -> bool:
    """Record *text* on *channel*; True if it was already seen.

    A repeat suppresses only this reply.  It deliberately does **not** also
    start the channel cooldown: the two controls are meant to be
    independent, and letting one arm the other would make a false positive
    here silence the whole channel for a full minute.
    """
    if not loop_detection_enabled():
        return False
    if not (text or "").strip():
        return False
    sig = loop_signature(channel, text)
    now = _monotonic()
    with _loop_lock:
        _evict_locked(now)
        seen = sig in _seen_signatures
        # Re-insert at the end so a message repeating steadily stays
        # suppressed rather than ageing out mid-loop and re-opening it.
        _seen_signatures.pop(sig, None)
        _seen_signatures[sig] = now
        _evict_locked(now)
    if seen:
        logger.info(
            "Meshtastic: suppressed reply on channel %r - loop signature "
            "already seen (repeated message content)",
            _channel_key(channel),
        )
    return seen


def loop_signature_count() -> int:
    """Test/introspection helper - entries currently cached."""
    with _loop_lock:
        return len(_seen_signatures)


def reset_loop_state() -> None:
    """Test helper - clears cooldowns and cached signatures."""
    with _loop_lock:
        _last_reply_at.clear()
        _seen_signatures.clear()


def allow_all_enabled() -> bool:
    """Whether ``MESHTASTIC_ALLOW_ALL_USERS`` is set truthy.

    Hermes reads this for *inbound* authorization via ``allow_all_env``.
    Honouring it here too keeps outbound behaviour consistent with what the
    operator configured, rather than silently applying to only one
    direction.
    """
    return os.getenv("MESHTASTIC_ALLOW_ALL_USERS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def build_allowlist(allow_from: Optional[List[Any]] = None) -> Set[str]:
    """Normalized allowlist from config plus ``MESHTASTIC_ALLOWED_USERS``."""
    entries: List[Any] = list(allow_from or [])
    env_users = os.getenv("MESHTASTIC_ALLOWED_USERS", "")
    entries.extend(u for u in env_users.split(",") if u.strip())
    out: Set[str] = set()
    for entry in entries:
        normalized = normalize_allow_entry(entry)
        if normalized:
            out.add(normalized)
    return out


class SendPolicy:
    """The policy inputs needed to authorize one outbound send."""

    def __init__(
        self,
        dm_policy: str = "pairing",
        group_policy: str = "disabled",
        allow_from: Optional[List[Any]] = None,
        channels: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dm_policy = str(dm_policy or "pairing").lower()
        self.group_policy = str(group_policy or "disabled").lower()
        self.allow_from = list(allow_from or [])
        self.channels = channels or {}

    @classmethod
    def from_adapter(cls, adapter: Any) -> "SendPolicy":
        return cls(
            dm_policy=getattr(adapter, "_dm_policy", "pairing"),
            group_policy=getattr(adapter, "_group_policy", "disabled"),
            allow_from=getattr(adapter, "allow_from", None),
            channels=getattr(adapter, "channels", None),
        )

    @classmethod
    def from_config(cls, pconfig: Any) -> "SendPolicy":
        """Build from a Hermes ``PlatformConfig`` (the standalone path).

        Env overrides config, matching how the adapter reads its settings.
        """
        extra = getattr(pconfig, "extra", {}) or {}
        return cls(
            dm_policy=os.getenv("MESHTASTIC_DM_POLICY") or extra.get("dm_policy") or "pairing",
            group_policy=os.getenv("MESHTASTIC_GROUP_POLICY") or extra.get("group_policy") or "disabled",
            allow_from=extra.get("allow_from"),
            channels=extra.get("channels"),
        )


def _paired_nodes() -> Set[str]:
    """Approved node IDs from the Hermes pairing store, if reachable."""
    try:
        from gateway.pairing import PairingStore
    except Exception:
        return set()
    try:
        store = PairingStore()
        approved = store.list_approved("meshtastic")  # type: ignore[attr-defined]
    except Exception:
        return set()
    out: Set[str] = set()
    for item in approved or []:
        user_id = item if isinstance(item, str) else (item or {}).get("user_id")
        normalized = normalize_node_id(user_id) if user_id else None
        if normalized:
            out.add(normalized)
    return out


def check_send_permitted(policy: SendPolicy, target: str) -> Optional[str]:
    """Return an error string when sending to *target* is not permitted.

    ``None`` means permitted.  Used by every outbound path so an agent
    cannot bypass a gate by choosing a different tool.
    """
    if is_channel_target(target):
        if policy.group_policy == "disabled":
            return "group_policy is 'disabled' — sending to channels is not permitted"
        if policy.group_policy == "allowlist":
            name = (channel_name_from_target(target) or "").lower()
            configured = {str(k).lower() for k in (policy.channels or {})}
            if "*" not in configured and name not in configured:
                return (
                    f"channel {name!r} is not in the configured channel allowlist "
                    "(group_policy is 'allowlist')"
                )
        return None

    if policy.dm_policy == "disabled":
        return "dm_policy is 'disabled' — sending direct messages is not permitted"

    allowed = build_allowlist(policy.allow_from)

    if policy.dm_policy == "open":
        # validate_config() refuses dm_policy 'open' without an explicit "*"
        # in allow_from.  Enforce the same invariant at send time: an
        # adapter constructed directly (tests, a drifting config path, a
        # future caller that skips validation) must not silently open the
        # radio to every node in range.
        if "*" in allowed or allow_all_enabled():
            return None
        return (
            "dm_policy is 'open' but allow_from does not contain '*' — "
            "refusing to treat the radio as open without an explicit opt-in"
        )

    if allow_all_enabled() or "*" in allowed or target in allowed:
        return None

    if policy.dm_policy == "pairing" and target in _paired_nodes():
        return None

    return (
        f"node {target} is not permitted under dm_policy {policy.dm_policy!r} — "
        "add it to allow_from / MESHTASTIC_ALLOWED_USERS, or pair it first"
    )
