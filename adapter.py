"""Meshtastic platform adapter for Hermes Agent.

Bridges a LoRa mesh radio to the Hermes gateway.  Radio users send text
over the mesh; the agent replies over the mesh.

Deliberate non-goals, each of which would be a bug here:

- **No access control.**  ``_dm_policy``/``_group_policy`` are exposed for
  the gateway's authorization layer to enforce.  Re-implementing allowlists
  or pairing would double-gate and diverge from every other platform.
- **No reconnect loop.**  ``GatewayRunner._platform_reconnect_watcher``
  owns retry.  A second loop would race this one for the serial port.
- **Never sets the region, never renames the device.**  A partial
  ``setConfig`` can zero ``tx_enabled``; ``setOwner()`` reboots the radio.

Configuration lives in ``config.yaml`` under ``platforms.meshtastic.extra``
or in ``MESHTASTIC_*`` env vars, which take precedence.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

# Hermes loads this file as a member of a package rooted at the plugin
# directory, so the sibling modules are relative imports.  When the module is
# imported directly instead (tests, linting, ``python -c``) there is no parent
# package, so fall back to top-level imports of the same files.
try:
    from . import transport as tp
    from .chunking import (
        MESHTASTIC_CHUNK_LIMIT,
        MESHTASTIC_HARD_LIMIT,
        chunk_text,
        strip_markdown,
    )
    from .nodedb import DEFAULT_POSITION_PRECISION, NodeDB
    from .normalize import (
        BROADCAST_ID,
        BROADCAST_NUM,
        channel_name_from_target,
        is_channel_target,
        node_num_to_hex,
        normalize_node_id,
        normalize_target,
    )
    from .policy import (
        resolve_channel_match,
        resolve_mention_gate,
        resolve_require_mention,
    )
    from . import sendpolicy as sp
except ImportError:  # pragma: no cover - direct-import context
    import transport as tp  # type: ignore[no-redef]
    from chunking import (  # type: ignore[no-redef]
        MESHTASTIC_CHUNK_LIMIT,
        MESHTASTIC_HARD_LIMIT,
        chunk_text,
        strip_markdown,
    )
    from nodedb import DEFAULT_POSITION_PRECISION, NodeDB  # type: ignore[no-redef]
    from normalize import (  # type: ignore[no-redef]
        BROADCAST_ID,
        BROADCAST_NUM,
        channel_name_from_target,
        is_channel_target,
        node_num_to_hex,
        normalize_node_id,
        normalize_target,
    )
    from policy import (  # type: ignore[no-redef]
        resolve_channel_match,
        resolve_mention_gate,
        resolve_require_mention,
    )
    import sendpolicy as sp  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# LoRa airtime is a scarce, legally regulated shared resource.  Sending
# chunks back-to-back overruns the radio's transmit queue and drops
# messages; this delay is a real constraint, not an inefficiency.
DEFAULT_CHUNK_DELAY_SECONDS = 1.5

_MEDIA_UNSUPPORTED = "LoRa mesh does not support media"


class UnknownChannelError(ValueError):
    """Raised when a channel target is not configured on this radio."""

    def __init__(self, name: str) -> None:
        self.channel_name = name
        super().__init__(
            f"channel {name!r} is not configured on this radio"
        )


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class MeshtasticAdapter(BasePlatformAdapter):
    """Async adapter over the threaded ``meshtastic`` pubsub library."""

    def __init__(self, config: Any, **kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("meshtastic"))

        extra = getattr(config, "extra", {}) or {}
        self._extra = extra

        # Env overrides config.yaml, per Hermes convention.
        self.transport = (os.getenv("MESHTASTIC_TRANSPORT") or extra.get("transport") or "serial").strip().lower()
        self.serial_port = os.getenv("MESHTASTIC_SERIAL_PORT") or extra.get("serial_port", "")
        self.tcp_host = os.getenv("MESHTASTIC_TCP_HOST") or extra.get("tcp_host", "")
        self.node_name = os.getenv("MESHTASTIC_NODE_NAME") or extra.get("node_name", "")

        try:
            self.chunk_limit = int(extra.get("text_chunk_limit", MESHTASTIC_CHUNK_LIMIT))
        except (TypeError, ValueError):
            self.chunk_limit = MESHTASTIC_CHUNK_LIMIT
        try:
            self.chunk_delay = float(extra.get("chunk_delay_seconds", DEFAULT_CHUNK_DELAY_SECONDS))
        except (TypeError, ValueError):
            self.chunk_delay = DEFAULT_CHUNK_DELAY_SECONDS

        # Read by the gateway's authorization layer — never enforced here.
        self._dm_policy = str(extra.get("dm_policy") or "pairing").lower()
        self._group_policy = str(extra.get("group_policy") or "disabled").lower()
        raw_allow = extra.get("allow_from")
        if isinstance(raw_allow, str):
            # Never iterate a string into characters — see validate_config.
            logger.warning(
                "Meshtastic: allow_from is a string (%r); expected a list. "
                "Ignoring it rather than guessing.", raw_allow,
            )
            raw_allow = []
        self.allow_from: List[str] = list(raw_allow or [])

        # Per-channel overrides, e.g. {"LongFast": {"require_mention": true}}.
        self.channels: Dict[str, Any] = extra.get("channels") or {}

        self.expose_position = _env_flag("MESHTASTIC_EXPOSE_POSITION", True)
        if "expose_position" in extra and os.getenv("MESHTASTIC_EXPOSE_POSITION") is None:
            self.expose_position = bool(extra.get("expose_position"))
        try:
            precision = int(extra.get("position_precision", DEFAULT_POSITION_PRECISION))
        except (TypeError, ValueError):
            precision = DEFAULT_POSITION_PRECISION
        # Clamp: these are the coordinates of real people.  A config typo
        # (or an operator who has not thought it through) must not be able
        # to publish metre-accurate positions to the agent.  0 dp ≈ 111 km,
        # 4 dp ≈ 11 m.
        self.position_precision = max(0, min(precision, DEFAULT_POSITION_PRECISION))

        # Runtime state.
        self._iface: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._my_node_num: Optional[int] = None
        self._region: Optional[str] = None
        self._subscribed = False
        self.nodedb = NodeDB()

    @property
    def name(self) -> str:
        return "Meshtastic"

    # ── Identity helpers ──────────────────────────────────────────────────

    @property
    def my_node_id(self) -> Optional[str]:
        if self._my_node_num is None:
            return None
        try:
            return node_num_to_hex(self._my_node_num)
        except ValueError:
            return None

    def _mention_name(self) -> Optional[str]:
        """The name that triggers a mention on a channel.

        Configured ``node_name`` wins; otherwise the device's own
        ``longName``, read (never written) from the radio.
        """
        if self.node_name:
            return self.node_name
        return tp.get_my_long_name(self._iface) if self._iface is not None else None

    def _resolve_channel_name(self, index: Any) -> Optional[str]:
        """Map a packet's channel index to its configured name."""
        try:
            idx = int(index or 0)
        except (TypeError, ValueError):
            idx = 0
        iface = self._iface
        if iface is None:
            return None
        return tp.channel_name_at(iface, idx)

    def available_channel_names(self) -> List[str]:
        """Names of the channels actually configured on this radio."""
        iface = self._iface
        if iface is None:
            return []
        names = []
        for ch in tp.get_channels(iface):
            index = ch.get("index") if isinstance(ch, dict) else getattr(ch, "index", None)
            if not isinstance(ch, dict) and getattr(ch, "role", None) == 0:
                continue
            name = tp.channel_name_at(iface, index)
            if name:
                names.append(name)
        return names

    def _channel_index_for_name(self, name: str) -> Optional[int]:
        iface = self._iface
        if iface is None:
            return None
        return tp.channel_index_of(iface, name)

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Open the radio interface and subscribe to its pubsub topics.

        Returns True on success.  On failure, reports state via
        ``_set_fatal_error`` and lets the gateway's watcher decide whether
        to retry — this adapter never retries on its own.

        *is_reconnect* is accepted for the base-class contract; Meshtastic
        has no server-side offline queue to preserve, so it only affects
        logging.
        """
        # Capture the loop while we are on it — the pubsub callbacks fire on
        # the library's receive thread and need this reference to get back.
        self._loop = asyncio.get_running_loop()

        if not check_requirements() and _env_flag("MESHTASTIC_AUTO_INSTALL", False):
            logger.info("Meshtastic: MESHTASTIC_AUTO_INSTALL set — installing the meshtastic package")
            await asyncio.to_thread(ensure_deps)

        if self.transport in ("ble", "mqtt"):
            self._set_fatal_error(
                "unsupported_transport",
                f"transport '{self.transport}' is not supported in v1 — see ROADMAP.md",
                retryable=False,
            )
            return False

        if self.transport == "serial" and not self.serial_port and not self._extra.get("autodetect", True):
            self._set_fatal_error(
                "config_missing",
                "MESHTASTIC_SERIAL_PORT must be set for transport: serial",
                retryable=False,
            )
            return False

        if self.transport == "tcp" and not self.tcp_host:
            self._set_fatal_error(
                "config_missing",
                "MESHTASTIC_TCP_HOST must be set for transport: tcp",
                retryable=False,
            )
            return False

        try:
            self._iface = await tp.open_interface(
                transport=self.transport,
                serial_port=self.serial_port,
                tcp_host=self.tcp_host,
            )
        except tp.TransportError as e:
            # A bad port or an unsupported transport will not fix itself.
            retryable = "not supported" not in str(e)
            self._set_fatal_error("connect_failed", str(e), retryable=retryable)
            return False
        except Exception as e:
            logger.error("Meshtastic: failed to open interface: %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        self._my_node_num = tp.get_my_node_num(self._iface)
        self._region = tp.read_region(self._iface)

        if tp.region_is_unset(self._region):
            # Transmitting on an unset region is a legal hazard, and this
            # plugin must never set it for the user.
            await tp.close_interface(self._iface)
            self._iface = None
            self._set_fatal_error(
                "region_unset",
                "LoRa region is UNSET — run: meshtastic --set lora.region <REGION>",
                retryable=False,
            )
            return False

        self._subscribe_all()
        try:
            count = self.nodedb.snapshot_from_interface(self._iface)
        except Exception:
            count = 0

        self._mark_connected()
        logger.info(
            "Meshtastic: connected via %s as %s (region %s, %d nodes)%s",
            self.transport,
            self.my_node_id or "unknown",
            self._region or "unknown",
            count,
            " [reconnect]" if is_reconnect else "",
        )
        return True

    async def disconnect(self) -> None:
        """Unsubscribe and release the port.  Safe to call repeatedly."""
        self._mark_disconnected()
        self._unsubscribe_all()
        iface, self._iface = self._iface, None
        # Always close, even on error paths: a held serial port makes the
        # gateway's next reconnect fail on a busy device.
        await tp.close_interface(iface)

    def _subscribe_all(self) -> None:
        if self._subscribed:
            return
        tp.subscribe(self._on_receive_text, tp.TOPIC_RECEIVE_TEXT)
        tp.subscribe(self._on_connection_lost, tp.TOPIC_CONNECTION_LOST)
        tp.subscribe(self._on_node_updated, tp.TOPIC_NODE_UPDATED)
        tp.subscribe(self._on_telemetry, tp.TOPIC_RECEIVE_TELEMETRY)
        self._subscribed = True

    def _unsubscribe_all(self) -> None:
        if not self._subscribed:
            return
        tp.unsubscribe(self._on_receive_text, tp.TOPIC_RECEIVE_TEXT)
        tp.unsubscribe(self._on_connection_lost, tp.TOPIC_CONNECTION_LOST)
        tp.unsubscribe(self._on_node_updated, tp.TOPIC_NODE_UPDATED)
        tp.unsubscribe(self._on_telemetry, tp.TOPIC_RECEIVE_TELEMETRY)
        self._subscribed = False

    # ── Inbound: pubsub callbacks (RECEIVE THREAD) ────────────────────────
    #
    # Everything below runs on the meshtastic library's receive thread, not
    # the event loop.  Each callback must:
    #   1. filter on interface identity — pub.subscribe is global, so a
    #      second adapter instance would otherwise see our traffic;
    #   2. never touch a coroutine directly — bridge via bridge_to_loop();
    #   3. never raise — an exception here is invisible and permanently
    #      kills message flow.

    def _is_ours(self, interface: Any) -> bool:
        return interface is not None and interface is self._iface

    def _on_receive_text(self, packet: Any = None, interface: Any = None, **kwargs: Any) -> None:
        try:
            if not self._is_ours(interface) or not isinstance(packet, dict):
                return
            tp.bridge_to_loop(self._loop, self._dispatch_message, packet)
        except Exception:
            logger.exception("Meshtastic: error in receive-text callback")

    def _on_connection_lost(self, interface: Any = None, **kwargs: Any) -> None:
        """Report the loss and stop.  The gateway watcher owns the retry."""
        try:
            if not self._is_ours(interface):
                return
            logger.warning("Meshtastic: connection lost")
            self._mark_disconnected()
        except Exception:
            logger.exception("Meshtastic: error in connection-lost callback")

    def _on_node_updated(self, node: Any = None, interface: Any = None, **kwargs: Any) -> None:
        try:
            if not self._is_ours(interface):
                return
            if isinstance(node, dict):
                self.nodedb.update_node(node)
        except Exception:
            logger.exception("Meshtastic: error in node-updated callback")

    def _on_telemetry(self, packet: Any = None, interface: Any = None, **kwargs: Any) -> None:
        try:
            if not self._is_ours(interface) or not isinstance(packet, dict):
                return
            decoded = packet.get("decoded") or {}
            telemetry = decoded.get("telemetry") or {}
            node_id = packet.get("fromId") or normalize_node_id(packet.get("from"))
            if not node_id:
                return
            metrics: Dict[str, Any] = {}
            metrics.update(telemetry.get("deviceMetrics") or {})
            metrics.update(telemetry.get("environmentMetrics") or {})
            if metrics:
                self.nodedb.record_telemetry(str(node_id).lower(), metrics)
        except Exception:
            logger.exception("Meshtastic: error in telemetry callback")

    # ── Inbound: dispatch (EVENT LOOP) ────────────────────────────────────

    async def _dispatch_message(self, packet: Dict[str, Any]) -> None:
        """Turn an inbound packet into a MessageEvent for the gateway.

        Access control is intentionally absent: ``handle_message`` and the
        gateway's authorization layer apply ``_dm_policy``/``_group_policy``.
        """
        try:
            decoded = packet.get("decoded") or {}
            text = decoded.get("text")
            if not text or not isinstance(text, str):
                return  # not a text packet

            from_num = packet.get("from")
            # Drop our own packets or the bot replies to itself forever,
            # burning shared airtime.  This is the loop guard.
            if from_num is not None and self._my_node_num is not None and int(from_num) == int(self._my_node_num):
                return

            # Authorization is evaluated against this identity, so derive it
            # from the numeric packet-level node number rather than the
            # convenience string.  If both are present and disagree, the
            # packet is malformed or forged — drop it rather than authorize
            # one identity for traffic originating from another.
            numeric_id = normalize_node_id(from_num)
            string_id = normalize_node_id(packet.get("fromId"))
            if numeric_id and string_id and numeric_id != string_id:
                logger.warning(
                    "Meshtastic: dropping packet with mismatched sender identity "
                    "(from=%s, fromId=%s)",
                    numeric_id,
                    string_id,
                )
                return
            from_id = numeric_id or string_id
            if not from_id:
                return
            from_id = str(from_id).lower()

            to_num = packet.get("to")
            is_direct = (
                to_num is not None
                and self._my_node_num is not None
                and int(to_num) == int(self._my_node_num)
            )

            node_record = self.nodedb.get_node(from_id) or {}
            user = node_record.get("user") or {}
            user_name = user.get("longName") or user.get("shortName") or from_id

            mention_name = self._mention_name()

            if is_direct:
                chat_id = from_id
                chat_name = user_name
                chat_type = "dm"
                gate = resolve_mention_gate(
                    text, mention_name, require_mention=False, is_direct=True
                )
            else:
                channel_index = packet.get("channel", 0)
                channel_name = self._resolve_channel_name(channel_index) or f"channel{channel_index}"
                chat_id = channel_name
                chat_name = channel_name
                chat_type = "group"

                match = resolve_channel_match(self.channels, channel_name)
                require_mention = resolve_require_mention(match.config, match.wildcard)
                # No command bypass here.  A leading "/" is not evidence of
                # authorization — it is attacker-controlled text, and
                # honouring it would let any node on a shared channel wake
                # the agent despite require_mention.  The gate's
                # authorized-command path is reserved for callers that have
                # actually checked the sender.
                gate = resolve_mention_gate(
                    text,
                    mention_name,
                    require_mention=require_mention,
                    is_direct=False,
                    is_authorized_command=False,
                )

            if not gate.allowed:
                logger.debug("Meshtastic: dropping channel message (%s)", gate.reason)
                return

            body = gate.text or text
            if not body.strip():
                return

            # Loop prevention.  Two Hermes bots on one channel with
            # require_mention false will otherwise answer each other
            # forever.  Both controls are evaluated here, at the *reply
            # decision*, rather than at send time: suppressing before
            # handle_message() saves the agent round-trip as well as the
            # airtime, and the inbound channel and text — which is what
            # both controls key on — are only available here.  The hard
            # rate limit stays down in the send path as the backstop that
            # no caller can route around.  See sendpolicy's docstring.
            if chat_type == "group":
                if not sp.cooldown_ok(chat_id, was_mentioned=gate.was_mentioned):
                    return  # suppressed silently; sendpolicy logged why
                if sp.loop_signature_seen(chat_id, body):
                    return

            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=from_id,
                user_name=user_name,
            )

            event = MessageEvent(
                text=body,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(packet.get("id") or ""),
                timestamp=datetime.datetime.now(),
                raw_message=packet,
            )

            await self.handle_message(event)
        except Exception:
            logger.exception("Meshtastic: error dispatching inbound packet")

    # ── Outbound ──────────────────────────────────────────────────────────

    def _resolve_destination(self, chat_id: str) -> tuple[Optional[str], int]:
        """Map a chat_id to ``(destination_id, channel_index)``.

        A ``!hex`` node id is a DM; anything else is a channel name, sent as
        a broadcast on that channel's index.
        """
        target = normalize_target(chat_id)
        if target is None:
            return None, 0
        if target == BROADCAST_ID:
            return None, 0
        if is_channel_target(target):
            name = channel_name_from_target(target) or ""
            index = self._channel_index_for_name(name)
            if index is None:
                # Falling back to index 0 would transmit on the primary
                # channel while reporting success for a channel that does
                # not exist on this radio — a silent mis-send.
                raise UnknownChannelError(name)
            return None, index
        return target, 0

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send *content* over the radio, split into LoRa-sized chunks."""
        # _iface stays set after a connection-loss event (the gateway owns
        # reconnection and may reuse the adapter), so an _iface-only check
        # would transmit through a link already reported as down.
        if self._iface is None or not self.is_connected:
            return SendResult(success=False, error="Not connected")

        plain = strip_markdown(content or "")
        chunks = chunk_text(plain, limit=self.chunk_limit, hard_limit=MESHTASTIC_HARD_LIMIT)
        if not chunks:
            return SendResult(success=False, error="Empty message after stripping")

        # Resolve the target first.  An unresolvable chat_id must never fall
        # through to dest_id=None, which the library treats as a broadcast to
        # the entire mesh — a silent, airtime-burning mis-send.
        target = normalize_target(chat_id)
        if target is None:
            return SendResult(success=False, error=f"Invalid target: {chat_id!r}")

        try:
            dest_id, channel_index = self._resolve_destination(chat_id)
        except UnknownChannelError as e:
            available = ", ".join(self.available_channel_names()) or "none"
            return SendResult(
                success=False,
                error=f"{e}. Available channels: {available}",
            )

        # The hard backstop.  sendpolicy's docstring promises that all three
        # send paths share one gate, but this one — the gateway's own reply
        # path — was not calling it, which left the busiest path uncapped.
        # It is checked here, after the target resolves, so a mis-addressed
        # send does not spend a token it never transmits.
        if not sp.rate_limit_ok():
            return SendResult(
                success=False,
                error=(
                    f"rate limit exceeded ({sp.rate_limit_max_sends()} sends per "
                    f"{int(sp.rate_limit_window_seconds())}s) — airtime is a "
                    "shared, regulated resource"
                ),
            )

        # Start this channel's conversation cooldown.  Recorded for every
        # outbound path that reaches the radio (this covers the gateway
        # reply and mesh_send, which funnels through here), so an agent
        # cannot keep a channel hot by choosing a different tool.
        #
        # Deliberately recorded *before* transmitting rather than after a
        # confirmed success: a multi-chunk send that fails halfway has still
        # put frames on the air, and a loop guard that only counted fully
        # successful sends would let a flapping radio loop freely.  The cost
        # is that a failed send also silences the channel for the cooldown —
        # the safe direction to err in when the resource is regulated airtime.
        if is_channel_target(target):
            sp.note_channel_reply(channel_name_from_target(target) or chat_id)

        last_id: Optional[str] = None
        for index, chunk in enumerate(chunks):
            try:
                result = await tp.send_text(
                    self._iface, chunk, dest_id=dest_id, channel_index=channel_index
                )
                if isinstance(result, dict) and result.get("id") is not None:
                    last_id = str(result["id"])
            except Exception as e:
                logger.error("Meshtastic: send failed on chunk %d/%d: %s", index + 1, len(chunks), e)
                return SendResult(success=False, error=str(e))

            # Pace the radio queue.  Do not remove: airtime is scarce and
            # back-to-back frames overrun the transmit buffer.
            if index < len(chunks) - 1 and self.chunk_delay > 0:
                await asyncio.sleep(self.chunk_delay)

        logger.info(
            "Meshtastic: sent %d chunk(s) to %s (%d bytes)",
            len(chunks),
            dest_id or f"channel:{channel_index}",
            sum(len(c.encode("utf-8")) for c in chunks),
        )
        return SendResult(success=True, message_id=last_id or "sent")

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """No-op — a radio has no typing indicator."""
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        target = normalize_target(chat_id) or chat_id
        if is_channel_target(target):
            name = channel_name_from_target(target) or chat_id
            return {"name": name, "type": "group", "chat_id": chat_id}
        record = self.nodedb.get_node(str(target).lower()) or {}
        user = record.get("user") or {}
        return {
            "name": user.get("longName") or user.get("shortName") or chat_id,
            "type": "dm",
            "chat_id": chat_id,
        }

    # ── Media: not possible over LoRa ─────────────────────────────────────

    async def send_image(self, chat_id: str, image_url: str, caption: str = "", **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)

    async def send_image_file(self, chat_id: str, path: str, caption: str = "", **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)

    async def send_document(self, chat_id: str, path: str, caption: str = "", **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)

    async def send_voice(self, chat_id: str, path: str, **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)

    async def send_video(self, chat_id: str, path: str, caption: str = "", **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)

    async def send_animation(self, chat_id: str, path: str, caption: str = "", **kwargs: Any) -> SendResult:
        return SendResult(success=False, error=_MEDIA_UNSUPPORTED)


# ---------------------------------------------------------------------------
# Registration support
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """PASSIVE probe: is the ``meshtastic`` package importable?

    Called freely by status displays and config loading, so it must stay
    side-effect free — never install anything here.
    """
    try:
        import meshtastic  # noqa: F401

        return True
    except ImportError:
        return False


def ensure_deps() -> bool:
    """ACTIVE installer for the ``meshtastic`` package.

    There is no ``ensure_deps_fn`` hook on ``PlatformEntry`` — the registry
    only takes ``check_fn`` + ``install_hint`` — so nothing calls this
    automatically.  It runs from :meth:`MeshtasticAdapter.connect` only when
    the operator opts in with ``MESHTASTIC_AUTO_INSTALL=true``, following
    the pattern the bundled google_chat plugin uses.

    Kept separate from ``check_requirements`` so the passive probe, which
    status displays call freely, never installs anything.
    """
    if check_requirements():
        return True
    import subprocess
    import sys

    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "meshtastic"])
    except Exception as e:
        logger.error("Meshtastic: automatic install failed: %s", e)
        return False
    return check_requirements()


def _config_value(config: Any, env_name: str, key: str, default: Any = "") -> Any:
    extra = getattr(config, "extra", {}) or {}
    return os.getenv(env_name) or extra.get(key, default)


def validate_config(config: Any) -> bool:
    """Validate transport coherence.  Port of ``config-schema.ts``.

    Logs the specific reason on failure — the registry only sees a bool, so
    a silent False would leave the user with no idea what is wrong.
    """
    extra = getattr(config, "extra", {}) or {}
    transport = str(_config_value(config, "MESHTASTIC_TRANSPORT", "transport", "serial")).strip().lower()

    if transport in ("ble", "mqtt"):
        logger.error(
            "Meshtastic: transport '%s' is not supported in v1 — see ROADMAP.md. "
            "Use transport: serial or transport: tcp.",
            transport,
        )
        return False

    if transport not in ("serial", "tcp"):
        logger.error("Meshtastic: unknown transport '%s' (expected 'serial' or 'tcp')", transport)
        return False

    if transport == "tcp" and not _config_value(config, "MESHTASTIC_TCP_HOST", "tcp_host"):
        logger.error("Meshtastic: transport 'tcp' requires tcp_host (MESHTASTIC_TCP_HOST)")
        return False

    if transport == "serial":
        # A missing port is allowed: the library autodetects a single
        # attached radio, which is the common single-device case.
        pass

    dm_policy = str(extra.get("dm_policy") or "pairing").lower()
    if dm_policy not in ("open", "pairing", "allowlist", "disabled"):
        logger.error("Meshtastic: invalid dm_policy '%s'", dm_policy)
        return False

    group_policy = str(extra.get("group_policy") or "disabled").lower()
    if group_policy not in ("open", "allowlist", "disabled"):
        logger.error("Meshtastic: invalid group_policy '%s'", group_policy)
        return False

    raw_allow_from = extra.get("allow_from")
    if raw_allow_from is not None and not isinstance(raw_allow_from, (list, tuple)):
        # A bare string is a plausible YAML slip (allow_from: "*") and both
        # failure modes are silent: Python iterates "*" into ["*"], which
        # OPENS the radio to the whole mesh, while "!aabbccdd" becomes
        # per-character garbage that matches nothing.  Refuse instead.
        logger.error(
            "Meshtastic: allow_from must be a list, got %s — "
            'write allow_from: ["*"] rather than allow_from: "*"',
            type(raw_allow_from).__name__,
        )
        return False

    if dm_policy == "open":
        # MeshClaw's requireOpenAllowFrom: opening a radio to the entire
        # mesh must be deliberate, never a config typo.
        allow_from = [str(a).strip() for a in (raw_allow_from or [])]
        if "*" not in allow_from:
            logger.error(
                "Meshtastic: dm_policy 'open' requires an explicit allow_from "
                "containing '*' — refusing to open the radio implicitly"
            )
            return False

    limit = extra.get("text_chunk_limit", MESHTASTIC_CHUNK_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        logger.error("Meshtastic: text_chunk_limit must be an integer")
        return False
    if not (50 <= limit <= MESHTASTIC_HARD_LIMIT):
        logger.error(
            "Meshtastic: text_chunk_limit %d out of range [50, %d]", limit, MESHTASTIC_HARD_LIMIT
        )
        return False

    return True


def is_connected(config: Any) -> bool:
    """Whether Meshtastic is configured enough to be considered enabled."""
    transport = str(_config_value(config, "MESHTASTIC_TRANSPORT", "transport", "")).strip().lower()
    if not transport:
        return False
    if transport == "tcp":
        return bool(_config_value(config, "MESHTASTIC_TCP_HOST", "tcp_host"))
    if transport == "serial":
        return True
    return False


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env before adapter construction.

    Without this, an env-only setup does not appear in ``hermes gateway
    status`` until the adapter is instantiated.
    """
    transport = os.getenv("MESHTASTIC_TRANSPORT", "").strip().lower()
    if not transport:
        return None

    seed: dict = {"transport": transport}
    for env_name, key in (
        ("MESHTASTIC_SERIAL_PORT", "serial_port"),
        ("MESHTASTIC_TCP_HOST", "tcp_host"),
        ("MESHTASTIC_NODE_NAME", "node_name"),
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            seed[key] = value

    home = os.getenv("MESHTASTIC_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}

    return seed


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Deliver a message without a live gateway adapter.

    Used when cron runs in a separate process from the gateway; without
    this hook, ``deliver=meshtastic`` jobs fail with "No live adapter".
    Opens a short-lived interface, sends, and always closes it — a held
    serial port would block the gateway's own adapter.

    ``thread_id``/``media_files`` are accepted for signature parity; a LoRa
    mesh has neither threads nor attachments.
    """
    if not check_requirements():
        return {"error": "meshtastic package not installed (pip install meshtastic)"}

    extra = getattr(pconfig, "extra", {}) or {}
    transport = str(os.getenv("MESHTASTIC_TRANSPORT") or extra.get("transport") or "serial").lower()
    if transport in ("ble", "mqtt"):
        return {"error": f"transport '{transport}' is not supported in v1 — see ROADMAP.md"}

    serial_port = os.getenv("MESHTASTIC_SERIAL_PORT") or extra.get("serial_port", "")
    tcp_host = os.getenv("MESHTASTIC_TCP_HOST") or extra.get("tcp_host", "")

    target = normalize_target(chat_id)
    if target is None:
        return {"error": f"invalid Meshtastic target: {chat_id!r}"}

    # This path is reachable from tools/send_message_tool.py — the same
    # agent-facing surface as mesh_send.  Without the same gate, an agent
    # could bypass dm_policy/group_policy/rate limiting simply by routing
    # through send_message instead of mesh_send.
    denial = sp.check_send_permitted(sp.SendPolicy.from_config(pconfig), target)
    if denial:
        return {"error": denial}
    if not sp.rate_limit_ok():
        return {
            "error": (
                f"rate limit exceeded ({sp.rate_limit_max_sends()} sends per "
                f"{int(sp.rate_limit_window_seconds())}s) — airtime is a "
                "shared, regulated resource"
            )
        }

    # Cron and send_message reach the radio here.  They must arm the same
    # per-channel cooldown as the reply path, or a scheduled job could keep
    # a channel hot while the gateway believes it is quiet.
    if is_channel_target(target):
        sp.note_channel_reply(channel_name_from_target(target))

    try:
        limit = int(extra.get("text_chunk_limit", MESHTASTIC_CHUNK_LIMIT))
    except (TypeError, ValueError):
        limit = MESHTASTIC_CHUNK_LIMIT
    try:
        delay = float(extra.get("chunk_delay_seconds", DEFAULT_CHUNK_DELAY_SECONDS))
    except (TypeError, ValueError):
        delay = DEFAULT_CHUNK_DELAY_SECONDS

    chunks = chunk_text(strip_markdown(message or ""), limit=limit, hard_limit=MESHTASTIC_HARD_LIMIT)
    if not chunks:
        return {"error": "empty message after stripping"}

    iface = None
    try:
        iface = await tp.open_interface(
            transport=transport, serial_port=serial_port, tcp_host=tcp_host
        )
    except Exception as e:
        return {"error": f"Meshtastic standalone connect failed: {e}"}

    try:
        region = tp.read_region(iface)
        if tp.region_is_unset(region):
            return {"error": "LoRa region is UNSET — run: meshtastic --set lora.region <REGION>"}

        dest_id: Optional[str] = None
        channel_index = 0
        if is_channel_target(target):
            name = channel_name_from_target(target) or ""
            resolved = tp.channel_index_of(iface, name)
            if resolved is None:
                return {"error": f"channel {name!r} is not configured on this radio"}
            channel_index = resolved
        elif target != BROADCAST_ID:
            dest_id = target

        last_id = None
        for index, chunk in enumerate(chunks):
            result = await tp.send_text(iface, chunk, dest_id=dest_id, channel_index=channel_index)
            if isinstance(result, dict) and result.get("id") is not None:
                last_id = str(result["id"])
            if index < len(chunks) - 1 and delay > 0:
                await asyncio.sleep(delay)

        return {"success": True, "message_id": last_id or "sent"}
    except Exception as e:
        return {"error": f"Meshtastic standalone send failed: {e}"}
    finally:
        await tp.close_interface(iface)


PLATFORM_HINT = (
    "You are responding over a LoRa mesh radio (Meshtastic). Each message "
    "is limited to ~200 bytes and takes seconds to transmit. Keep responses "
    "extremely concise — plain text only, no markdown, no emoji, no bullet "
    "lists. Use short sentences. Omit filler words. Put the most important "
    "information first. Long replies are split across multiple slow radio "
    "transmissions, so brevity is a hard requirement, not a style preference."
)


def register(ctx: Any) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    ctx.register_platform(
        name="meshtastic",
        label="Meshtastic",
        adapter_factory=lambda cfg: MeshtasticAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MESHTASTIC_TRANSPORT"],
        install_hint="pip install meshtastic",
        setup_fn=_interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESHTASTIC_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MESHTASTIC_ALLOWED_USERS",
        allow_all_env="MESHTASTIC_ALLOW_ALL_USERS",
        max_message_length=MESHTASTIC_CHUNK_LIMIT,
        emoji="📻",
        pii_safe=False,
        # Stated rather than inherited: /update over a slow, shared radio
        # link is a deliberate choice, so make it visible in the registration.
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )

    try:
        from .mesh_tools import register_tools
    except ImportError:  # pragma: no cover
        from mesh_tools import register_tools  # type: ignore[no-redef]

    register_tools(ctx)


def _interactive_setup() -> None:
    """Deferred import so the wizard's CLI deps stay out of the runtime."""
    try:
        from .setup_wizard import interactive_setup
    except ImportError:  # pragma: no cover
        from setup_wizard import interactive_setup  # type: ignore[no-redef]

    interactive_setup()
