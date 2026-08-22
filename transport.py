"""Meshtastic interface construction and the thread → asyncio bridge.

Replaces MeshClaw's ``client.ts`` + ``monitor.ts``.

**The central hazard this module manages.**  The ``meshtastic`` PyPI library
is threaded pubsub: interfaces run a background receive thread and publish
events through ``pypubsub``.  Hermes adapters are asyncio.  Callbacks
therefore fire on the *wrong* thread and must never touch coroutines
directly — see :func:`bridge_to_loop`.

Two further consequences shape this module:

- Interface construction and ``sendText`` **block** (opening a serial port,
  waiting for the node DB).  Both are wrapped in ``asyncio.to_thread`` so
  the gateway event loop keeps running.
- ``pub.subscribe`` is **global**, not per-interface.  Two adapter instances
  would cross-talk, so subscriptions are bound methods that are explicitly
  unsubscribed on disconnect, and every callback filters on interface
  identity.

**Reconnection is deliberately absent.**  Hermes' ``GatewayRunner.
_platform_reconnect_watcher`` owns retry with its own backoff and re-invokes
``connect()``.  A second loop here would mean two loops racing for one
serial port.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Callable, Optional

# Shared with the setup wizard and the install-time checks so the default
# port is stated in exactly one place.
try:
    from .envcheck import DEFAULT_TCP_PORT
except ImportError:  # pragma: no cover - direct-import context
    from envcheck import DEFAULT_TCP_PORT  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TCP_PORT",
    "TOPIC_RECEIVE_TEXT",
    "TOPIC_CONNECTION_ESTABLISHED",
    "TOPIC_CONNECTION_LOST",
    "TOPIC_NODE_UPDATED",
    "TOPIC_RECEIVE_TELEMETRY",
    "TOPIC_RECEIVE_POSITION",
    "TransportError",
    "open_interface",
    "close_interface",
    "send_text",
    "bridge_to_loop",
    "subscribe",
    "unsubscribe",
    "get_my_node_num",
    "read_region",
    "get_channels",
    "channel_name_at",
    "channel_index_of",
]

# pubsub topics published by the meshtastic library.
TOPIC_RECEIVE_TEXT = "meshtastic.receive.text"
TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
TOPIC_NODE_UPDATED = "meshtastic.node.updated"
TOPIC_RECEIVE_TELEMETRY = "meshtastic.receive.telemetry"
TOPIC_RECEIVE_POSITION = "meshtastic.receive.position"


class TransportError(RuntimeError):
    """Raised when an interface cannot be opened or used."""


# ---------------------------------------------------------------------------
# Interface lifecycle
# ---------------------------------------------------------------------------


def _build_interface(
    transport: str,
    serial_port: str,
    tcp_host: str,
    tcp_port: int = DEFAULT_TCP_PORT,
) -> Any:
    """Construct a meshtastic interface.  **Blocking** — call in a thread.

    Imported lazily so the plugin module stays importable when the
    ``meshtastic`` package is absent (``check_requirements`` reports that
    case, and the tests exercise the fake transport instead).
    """
    kind = (transport or "serial").strip().lower()

    if kind == "serial":
        try:
            from meshtastic.serial_interface import SerialInterface
        except ImportError as e:  # pragma: no cover - depends on env
            raise TransportError(f"meshtastic package not installed: {e}") from e
        # devPath=None lets the library autodetect a single attached radio.
        return SerialInterface(devPath=serial_port or None)

    if kind == "tcp":
        try:
            from meshtastic.tcp_interface import TCPInterface
        except ImportError as e:  # pragma: no cover - depends on env
            raise TransportError(f"meshtastic package not installed: {e}") from e
        if not tcp_host:
            raise TransportError(
                "tcp transport requires a host — set MESHTASTIC_TCP_HOST "
                "(e.g. meshtastic.local) in ~/.hermes/.env, or reconfigure "
                "with: hermes gateway setup"
            )
        # portNumber is keyword-only on TCPInterface and defaults to 4403;
        # pass it explicitly so a radio behind a tunnel or reverse proxy is
        # reachable.
        return TCPInterface(hostname=tcp_host, portNumber=tcp_port)

    if kind in ("ble", "mqtt"):
        raise TransportError(
            f"transport '{kind}' is not supported in v1 — see ROADMAP.md. "
            "Use transport: serial or transport: tcp."
        )

    raise TransportError(f"unknown transport '{transport}' (expected 'serial' or 'tcp')")


async def open_interface(
    transport: str = "serial",
    serial_port: str = "",
    tcp_host: str = "",
    tcp_port: int = DEFAULT_TCP_PORT,
    timeout: float = 60.0,
) -> Any:
    """Open a Meshtastic interface without blocking the event loop.

    Construction opens the port and waits for the device's node database,
    which can take many seconds; it runs in a worker thread.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _build_interface, transport, serial_port, tcp_host, tcp_port
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise TransportError(
            f"timed out after {timeout}s opening the {transport} interface"
        ) from e


async def close_interface(iface: Any) -> None:
    """Close *iface*, releasing the serial port.  Never raises.

    Always call this on the way out — including error paths — or the port
    stays held and the gateway's reconnect attempt fails on a busy device.
    """
    if iface is None:
        return
    try:
        await asyncio.to_thread(iface.close)
    except Exception as e:
        logger.warning("Meshtastic: error closing interface: %s", e)


async def send_text(
    iface: Any,
    text: str,
    dest_id: Optional[str] = None,
    channel_index: int = 0,
) -> Any:
    """Send one text frame.  Blocking library call, run in a thread.

    *dest_id* is a ``!hex`` node id for a DM, or ``None``/``^all`` to
    broadcast on *channel_index*.
    """
    if iface is None:
        raise TransportError("not connected")

    def _send() -> Any:
        kwargs: dict = {"channelIndex": channel_index}
        if dest_id:
            kwargs["destinationId"] = dest_id
        return iface.sendText(text, **kwargs)

    return await asyncio.to_thread(_send)


# ---------------------------------------------------------------------------
# Thread → asyncio bridge
# ---------------------------------------------------------------------------


def bridge_to_loop(
    loop: asyncio.AbstractEventLoop,
    coro_factory: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Schedule a coroutine on *loop* from the library's receive thread.

    This is the single most important function in the plugin.  Calling
    ``handle_message`` directly from a pubsub callback silently does
    nothing — the coroutine is never awaited and message flow dies without
    an error.  Everything inbound goes through here.

    Exceptions are swallowed and logged: an exception raised on the receive
    thread is invisible and kills the callback for good.
    """
    if loop is None or loop.is_closed():
        logger.debug("Meshtastic: dropping callback — event loop unavailable")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(coro_factory(*args, **kwargs), loop)
    except Exception:
        logger.exception("Meshtastic: failed to bridge callback to the event loop")
        return

    # Observe the result.  Without this the future is discarded, so any
    # exception escaping the coroutine is swallowed with no log line at all
    # — a silent stop to message flow that is very hard to diagnose.
    def _log_failure(fut: "concurrent.futures.Future") -> None:
        try:
            error = fut.exception()
        except concurrent.futures.CancelledError:
            return
        except Exception:  # pragma: no cover - defensive
            return
        if error is not None:
            logger.error("Meshtastic: bridged callback raised: %s", error, exc_info=error)

    future.add_done_callback(_log_failure)


def subscribe(callback: Callable, topic: str) -> None:
    """Subscribe *callback* to a pubsub *topic*.

    ``pub.subscribe`` holds only a weak reference to the callback, so bound
    methods of a live adapter are required — a lambda or local function
    would be garbage-collected and the subscription would evaporate.
    """
    from pubsub import pub

    pub.subscribe(callback, topic)


def unsubscribe(callback: Callable, topic: str) -> None:
    """Unsubscribe *callback* from *topic*.  Never raises.

    Subscriptions are global and process-wide; failing to unsubscribe makes
    a second adapter instance receive this one's traffic.
    """
    try:
        from pubsub import pub

        pub.unsubscribe(callback, topic)
    except Exception as e:
        logger.debug("Meshtastic: unsubscribe(%s) failed: %s", topic, e)


# ---------------------------------------------------------------------------
# Device introspection
# ---------------------------------------------------------------------------


def get_my_node_num(iface: Any) -> Optional[int]:
    """Our own node number, or ``None`` when unavailable.

    Used to drop self-originated packets: without that check the bot
    replies to its own messages forever over a shared radio band.
    """
    try:
        my_info = getattr(iface, "myInfo", None)
        if my_info is None:
            return None
        num = getattr(my_info, "my_node_num", None)
        if num is None:
            num = getattr(my_info, "myNodeNum", None)
        return int(num) if num is not None else None
    except Exception:
        return None


def get_my_long_name(iface: Any) -> Optional[str]:
    """The device's configured ``longName``, used as the mention trigger.

    Read-only: this plugin never calls ``setOwner()``, which would reboot
    the radio.  If the user wants a different name they set it with the
    ``meshtastic`` CLI.
    """
    try:
        my_num = get_my_node_num(iface)
        nodes = getattr(iface, "nodes", None) or {}
        for node in nodes.values():
            user = (node or {}).get("user") or {}
            if my_num is not None and (node or {}).get("num") == my_num:
                return user.get("longName") or user.get("shortName")
        # Fall back to the library's own accessor when present.
        get_ln = getattr(iface, "getLongName", None)
        if callable(get_ln):
            return get_ln()
    except Exception:
        pass
    return None


def read_region(iface: Any) -> Optional[str]:
    """Read the configured LoRa region, e.g. ``"US"`` or ``"UNSET"``.

    This plugin **never sets** the region: a partial ``setConfig`` can zero
    ``tx_enabled`` and silently disable transmission, and picking a region
    for the user is a legal hazard.  We read it so we can refuse to
    transmit when it is ``UNSET`` and tell the user to run
    ``meshtastic --set lora.region <REGION>``.
    """
    try:
        # The region lives on iface.localNode.localConfig, NOT on iface
        # itself — iface.localConfig is None on a real device.  Verified
        # against a RAK4631; getting this wrong silently skips the
        # region-unset check and lets the adapter transmit illegally.
        local_config = getattr(iface, "localConfig", None)
        if local_config is None:
            local_node = getattr(iface, "localNode", None)
            local_config = getattr(local_node, "localConfig", None)
        lora = getattr(local_config, "lora", None) if local_config is not None else None
        region = getattr(lora, "region", None)
        if region is None:
            return None
        # protobuf enums stringify via their descriptor when available.
        try:
            from meshtastic.protobuf import config_pb2

            return config_pb2.Config.LoRaConfig.RegionCode.Name(region)
        except Exception:
            return str(region)
    except Exception:
        return None


def get_channels(iface: Any) -> list:
    """Return the radio's channel table as a list.

    Real interfaces expose channels on ``iface.localNode.channels``;
    ``iface.channels`` is ``None``.  Both are checked so the function works
    against a real device and against test doubles.
    """
    table = getattr(iface, "channels", None)
    if not table:
        local_node = getattr(iface, "localNode", None)
        table = getattr(local_node, "channels", None)
    return list(table) if table else []


def channel_name_at(iface: Any, index: int) -> Optional[str]:
    """Name of the channel at *index*, or None.

    Handles both the protobuf shape (``ch.settings.name`` with ``ch.index``)
    and the plain-dict shape used by the fake interface.  An unnamed
    primary channel reports as ``LongFast``, which is what Meshtastic shows
    for the default channel.
    """
    for ch in get_channels(iface):
        if isinstance(ch, dict):
            if ch.get("index") == index:
                return ch.get("name") or None
            continue
        if getattr(ch, "index", None) != index:
            continue
        settings = getattr(ch, "settings", None)
        name = getattr(settings, "name", None) if settings is not None else None
        if name:
            return name
        # role 1 == PRIMARY; an unnamed primary is the LongFast default.
        if getattr(ch, "role", None) == 1:
            return "LongFast"
        return None
    return None


def channel_index_of(iface: Any, name: str) -> Optional[int]:
    """Index of the channel called *name* (case-insensitive), or None."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for ch in get_channels(iface):
        if isinstance(ch, dict):
            if str(ch.get("name", "")).lower() == wanted:
                return int(ch.get("index", 0))
            continue
        settings = getattr(ch, "settings", None)
        ch_name = getattr(settings, "name", "") if settings is not None else ""
        index = getattr(ch, "index", None)
        if str(ch_name).lower() == wanted:
            return int(index or 0)
        if not ch_name and getattr(ch, "role", None) == 1 and wanted == "longfast":
            return int(index or 0)
    return None


def region_is_unset(region: Optional[str]) -> bool:
    """True when *region* means "not configured"."""
    if region is None:
        return False  # unknown != unset; do not block on a failed read
    return str(region).strip().upper() in ("UNSET", "0")
