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

Pure logic: no Hermes or ``meshtastic`` imports.
"""

from __future__ import annotations

import os
import threading
import time
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

__all__ = [
    "RATE_LIMIT_MAX_SENDS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "SendPolicy",
    "allow_all_enabled",
    "build_allowlist",
    "check_send_permitted",
    "rate_limit_ok",
    "reset_rate_limit",
]

# Airtime is a shared, legally regulated resource: the agent must not be
# able to flood it even if it decides sending is a good idea.
RATE_LIMIT_MAX_SENDS = 5
RATE_LIMIT_WINDOW_SECONDS = 60.0

_rate_lock = threading.Lock()
_send_times: List[float] = []


def rate_limit_ok() -> bool:
    """Token bucket over a sliding window.  Shared by every send path."""
    now = time.monotonic()
    with _rate_lock:
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        _send_times[:] = [t for t in _send_times if t > cutoff]
        if len(_send_times) >= RATE_LIMIT_MAX_SENDS:
            return False
        _send_times.append(now)
        return True


def reset_rate_limit() -> None:
    """Test helper — clears the sliding window."""
    with _rate_lock:
        _send_times.clear()


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
