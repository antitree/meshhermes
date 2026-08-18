"""Agent-callable tools for mesh state and sending.

Handler rules, from the Hermes plugin guide and non-negotiable:

1. Signature ``handler(args: dict, **kwargs) -> str``
2. **Always** return a JSON string — never a dict, never an object
3. **Never** raise — catch everything and return ``{"error": ...}``
4. Accept ``**kwargs`` for forward compatibility

Tools run outside the adapter (often in a terminal session with no gateway
at all), so every handler tolerates the radio simply not being there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from .normalize import (
        channel_name_from_target,
        is_channel_target,
        normalize_allow_entry,
        normalize_node_id,
        normalize_target,
    )
    from . import sendpolicy as sp
except ImportError:  # pragma: no cover - direct-import context
    from normalize import (  # type: ignore[no-redef]
        channel_name_from_target,
        is_channel_target,
        normalize_allow_entry,
        normalize_node_id,
        normalize_target,
    )
    import sendpolicy as sp  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

__all__ = [
    "set_live_adapter",
    "mesh_nodes_handler",
    "mesh_telemetry_handler",
    "mesh_channels_handler",
    "mesh_send_handler",
    "register_tools",
]

_NO_GATEWAY = "Meshtastic gateway is not running"

# Rate limiting and send authorization live in sendpolicy so that every
# outbound path shares one gate — see that module's docstring.
_RATE_LIMIT_MAX_SENDS = sp.RATE_LIMIT_MAX_SENDS
_RATE_LIMIT_WINDOW_SECONDS = sp.RATE_LIMIT_WINDOW_SECONDS
_rate_limit_ok = sp.rate_limit_ok
_reset_rate_limit = sp.reset_rate_limit


# Strong references to in-flight fire-and-forget sends.  asyncio only holds a
# weak reference to a task, so without this a send could be garbage-collected
# mid-transmission.
_pending_sends: set = set()


def _log_send_result(task: Any) -> None:
    """Surface the outcome of a queued send; never raises."""
    try:
        if task.cancelled():
            logger.warning("mesh_send: queued transmission was cancelled")
            return
        error = task.exception()
        if error is not None:
            logger.error("mesh_send: queued transmission failed: %s", error)
            return
        result = task.result()
        if not getattr(result, "success", False):
            logger.error(
                "mesh_send: queued transmission failed: %s",
                getattr(result, "error", "unknown error"),
            )
    except Exception:
        logger.debug("mesh_send: could not read queued send result", exc_info=True)


# An adapter registered by a non-gateway host (a one-shot CLI run, a test
# harness) that still wants the mesh_* tools to work.  The gateway path is
# tried first; this is the fallback.
_registered_adapter: Optional[Any] = None


def set_live_adapter(adapter: Optional[Any]) -> None:
    """Register *adapter* as the one the mesh_* tools should drive.

    The gateway does not need this — ``_live_adapter`` finds its adapters
    through the runner.  It exists for hosts that run an adapter outside a
    gateway process (``hermes -z`` one-shots, hardware test scripts), where
    the runner reference is absent and the tools would otherwise report
    "gateway is not running".
    """
    global _registered_adapter
    _registered_adapter = adapter


def _live_adapter() -> Optional[Any]:
    """Return the running Meshtastic adapter, or None.

    Mirrors how Hermes reaches live adapters internally.  ``runner.adapters``
    is keyed by the ``Platform`` enum, so a plain string lookup would always
    miss; both spellings are tried.  Falls back to an adapter registered via
    :func:`set_live_adapter` for non-gateway hosts.
    """
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        adapters = getattr(runner, "adapters", None) or {}
        try:
            from gateway.config import Platform

            adapter = adapters.get(Platform("meshtastic"))
            if adapter is not None:
                return adapter
        except Exception:
            pass
        adapter = adapters.get("meshtastic")
        if adapter is not None:
            return adapter

    return _registered_adapter


def _transport():
    """Import the transport module in either package or direct context."""
    try:
        from . import transport as tp
    except ImportError:  # pragma: no cover - direct-import context
        import transport as tp  # type: ignore[no-redef]
    return tp


def _err(message: str, **extra: Any) -> str:
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


def mesh_nodes_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """List known mesh nodes."""
    try:
        args = args or {}
        adapter = _live_adapter()
        if adapter is None:
            return _err(_NO_GATEWAY)

        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))
        sort_by = str(args.get("sort_by") or "last_heard")

        records = adapter.nodedb.all_nodes()
        described = [
            adapter.nodedb.describe_node(
                r,
                expose_position=getattr(adapter, "expose_position", True),
                precision=getattr(adapter, "position_precision", 4),
            )
            for r in records
        ]

        if sort_by == "name":
            described.sort(key=lambda n: (n.get("long_name") or "").lower())
        elif sort_by == "hops":
            described.sort(key=lambda n: n.get("hops_away") if n.get("hops_away") is not None else 999)
        elif sort_by == "snr":
            described.sort(key=lambda n: n.get("snr") if n.get("snr") is not None else -999, reverse=True)
        else:
            described.sort(key=lambda n: n.get("last_heard") or 0, reverse=True)

        return json.dumps(
            {
                "node_count": len(described),
                "nodes": described[:limit],
                "position_exposed": bool(getattr(adapter, "expose_position", True)),
            }
        )
    except Exception as e:
        logger.debug("mesh_nodes failed", exc_info=True)
        return _err(f"mesh_nodes failed: {e}")


def mesh_telemetry_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return device/environment telemetry, with recent history."""
    try:
        args = args or {}
        adapter = _live_adapter()
        if adapter is None:
            return _err(_NO_GATEWAY)

        try:
            history = int(args.get("history", 5))
        except (TypeError, ValueError):
            history = 5
        history = max(0, min(history, 100))

        raw_target = args.get("node_id")
        node_ids: List[str]
        if raw_target:
            normalized = normalize_node_id(raw_target)
            if normalized is None:
                return _err(f"invalid node_id: {raw_target!r}")
            node_ids = [normalized]
        else:
            node_ids = [n.get("id") for n in
                        (adapter.nodedb.describe_node(r, expose_position=False)
                         for r in adapter.nodedb.all_nodes())
                        if n.get("id")]

        out: List[Dict[str, Any]] = []
        for node_id in node_ids:
            record = adapter.nodedb.get_node(node_id)
            entry: Dict[str, Any] = {"node_id": node_id}
            if record:
                user = record.get("user") or {}
                entry["long_name"] = user.get("longName")
                metrics = record.get("deviceMetrics") or {}
                if metrics:
                    entry["current"] = {
                        "battery_level": metrics.get("batteryLevel"),
                        "voltage": metrics.get("voltage"),
                        "channel_utilization": metrics.get("channelUtilization"),
                        "air_util_tx": metrics.get("airUtilTx"),
                    }
            samples = adapter.nodedb.telemetry_history(node_id, limit=history) if history else []
            if samples:
                entry["history"] = samples
            if record is None and not samples:
                entry["note"] = "node not known to this radio"
            out.append(entry)

        return json.dumps({"count": len(out), "telemetry": out})
    except Exception as e:
        logger.debug("mesh_telemetry failed", exc_info=True)
        return _err(f"mesh_telemetry failed: {e}")


def mesh_channels_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """List configured channels on the radio."""
    try:
        adapter = _live_adapter()
        if adapter is None:
            return _err(_NO_GATEWAY)

        iface = getattr(adapter, "_iface", None)
        if iface is None:
            return _err("Meshtastic radio is not connected")

        channels: List[Dict[str, Any]] = []
        for ch in _transport().get_channels(iface):
            if isinstance(ch, dict):
                channels.append(
                    {
                        "index": ch.get("index"),
                        "name": ch.get("name"),
                        "primary": bool(ch.get("primary")),
                    }
                )
                continue
            index = getattr(ch, "index", None)
            role = getattr(ch, "role", None)
            # role 0 == DISABLED: not a usable channel, so leave it out.
            if role == 0:
                continue
            channels.append(
                {
                    "index": index,
                    "name": _transport().channel_name_at(iface, index),
                    "primary": role == 1,
                }
            )

        return json.dumps({"count": len(channels), "channels": channels})
    except Exception as e:
        logger.debug("mesh_channels failed", exc_info=True)
        return _err(f"mesh_channels failed: {e}")


# ---------------------------------------------------------------------------
# mesh_send — the one tool that transmits
# ---------------------------------------------------------------------------


def _allowlist(adapter: Any) -> set:
    """Normalized allowlist for *adapter* (config + env)."""
    return sp.build_allowlist(getattr(adapter, "allow_from", None))


def _check_send_permitted(adapter: Any, target: str) -> Optional[str]:
    """Return an error string when sending to *target* is not permitted."""
    return sp.check_send_permitted(sp.SendPolicy.from_adapter(adapter), target)


def mesh_send_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Transmit a message over the radio, subject to policy and rate limit."""
    try:
        args = args or {}
        raw_target = args.get("target")
        message = args.get("message")

        if not raw_target or not str(raw_target).strip():
            return _err("target is required")
        if not message or not str(message).strip():
            return _err("message is required")

        target = normalize_target(raw_target)
        if target is None:
            return _err(f"invalid target: {raw_target!r}")

        adapter = _live_adapter()
        if adapter is None:
            return _err(_NO_GATEWAY)
        # Check is_connected, not just _iface: on connection loss the
        # adapter reports disconnected while _iface is still set, so an
        # _iface-only check would keep transmitting through a dead link.
        if getattr(adapter, "_iface", None) is None or not getattr(adapter, "is_connected", False):
            return _err("Meshtastic radio is not connected")

        denial = _check_send_permitted(adapter, target)
        if denial:
            return _err(denial, target=target, permitted=False)

        if not _rate_limit_ok():
            return _err(
                f"rate limit exceeded ({_RATE_LIMIT_MAX_SENDS} sends per "
                f"{int(_RATE_LIMIT_WINDOW_SECONDS)}s) — airtime is a shared, "
                "regulated resource",
                target=target,
            )

        chat_id = channel_name_from_target(target) if is_channel_target(target) else target

        # Resolve the destination BEFORE scheduling.  When called from the
        # gateway loop this handler cannot await the send, so it reports
        # acceptance; without this check an unresolvable target (e.g. a
        # channel absent from the radio) would be reported as sent while
        # nothing went over the air.
        resolve = getattr(adapter, "_resolve_destination", None)
        if callable(resolve):
            try:
                resolve(chat_id)
            except Exception as e:
                return _err(str(e), target=target)

        loop = getattr(adapter, "_loop", None)
        coro = adapter.send(chat_id, str(message))
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                # Called from within the gateway loop: the handler is sync,
                # so we cannot await.  Schedule and report acceptance —
                # ``queued`` tells the caller this is not yet confirmed
                # delivery.
                task = asyncio.ensure_future(coro)
                # Keep a strong reference (the loop only holds a weak one) and
                # log any failure — otherwise a failed transmission would
                # vanish silently.
                _pending_sends.add(task)
                task.add_done_callback(_pending_sends.discard)
                task.add_done_callback(_log_send_result)
                byte_len = len(str(message).encode("utf-8"))
                logger.info("mesh_send: queued %d bytes to %s", byte_len, target)
                return json.dumps(
                    {"success": True, "target": target, "bytes": byte_len, "queued": True}
                )
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            result = future.result(timeout=120)
        else:
            result = asyncio.run(coro)

        byte_len = len(str(message).encode("utf-8"))
        success = bool(getattr(result, "success", False))
        logger.info(
            "mesh_send: %s %d bytes to %s",
            "sent" if success else "failed sending",
            byte_len,
            target,
        )
        if not success:
            return _err(getattr(result, "error", "send failed") or "send failed", target=target)
        return json.dumps(
            {
                "success": True,
                "target": target,
                "bytes": byte_len,
                "message_id": getattr(result, "message_id", None),
            }
        )
    except Exception as e:
        logger.debug("mesh_send failed", exc_info=True)
        return _err(f"mesh_send failed: {e}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOOLSET = "hermes-meshtastic"


def register_tools(ctx: Any) -> None:
    """Register the mesh_* tools with the Hermes tool registry."""
    try:
        from .schemas import (
            MESH_CHANNELS_SCHEMA,
            MESH_NODES_SCHEMA,
            MESH_SEND_SCHEMA,
            MESH_TELEMETRY_SCHEMA,
        )
    except ImportError:  # pragma: no cover - direct-import context
        from schemas import (  # type: ignore[no-redef]
            MESH_CHANNELS_SCHEMA,
            MESH_NODES_SCHEMA,
            MESH_SEND_SCHEMA,
            MESH_TELEMETRY_SCHEMA,
        )

    specs = (
        (MESH_NODES_SCHEMA, mesh_nodes_handler, "📻"),
        (MESH_TELEMETRY_SCHEMA, mesh_telemetry_handler, "📊"),
        (MESH_CHANNELS_SCHEMA, mesh_channels_handler, "📡"),
        (MESH_SEND_SCHEMA, mesh_send_handler, "📤"),
    )
    for schema, handler, emoji in specs:
        try:
            ctx.register_tool(
                name=schema["name"],
                toolset=_TOOLSET,
                schema=schema,
                handler=handler,
                description=schema["description"],
                emoji=emoji,
            )
        except Exception as e:
            logger.warning("Meshtastic: failed to register tool %s: %s", schema["name"], e)
