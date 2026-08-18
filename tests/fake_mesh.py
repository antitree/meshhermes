"""A fake Meshtastic interface for tests.

This is the backbone of the test suite.  It mimics only the parts of
``meshtastic.mesh_interface.MeshInterface`` the adapter actually touches,
and — crucially — it publishes on the **real** pubsub topics a radio would.
That means the adapter's genuine subscription wiring and its
thread → asyncio bridge are exercised, rather than mocked away.  A mock
that called the adapter's methods directly would pass while the real
plugin silently dropped every message.

No hardware and no ``meshtastic`` package are required.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from normalize import BROADCAST_NUM, hex_to_node_num, node_num_to_hex
from transport import (
    TOPIC_CONNECTION_ESTABLISHED,
    TOPIC_CONNECTION_LOST,
    TOPIC_NODE_UPDATED,
    TOPIC_RECEIVE_TELEMETRY,
    TOPIC_RECEIVE_TEXT,
)

MY_NODE_NUM = 0x11223344
MY_NODE_ID = node_num_to_hex(MY_NODE_NUM)
PEER_NODE_NUM = 0xAABBCCDD
PEER_NODE_ID = node_num_to_hex(PEER_NODE_NUM)
STRANGER_NODE_NUM = 0x99887766
STRANGER_NODE_ID = node_num_to_hex(STRANGER_NODE_NUM)


@dataclass
class SentMessage:
    """One recorded ``sendText`` call."""

    text: str
    destination_id: Optional[str] = None
    channel_index: int = 0

    @property
    def byte_length(self) -> int:
        return len(self.text.encode("utf-8"))


class _FakeMyInfo:
    def __init__(self, num: int) -> None:
        self.my_node_num = num
        self.myNodeNum = num


class _FakeLoRaConfig:
    def __init__(self, region: Any) -> None:
        self.region = region


class _FakeLocalConfig:
    def __init__(self, region: Any) -> None:
        self.lora = _FakeLoRaConfig(region)


def _default_nodes() -> Dict[str, Any]:
    """A small node DB in the library's real shape."""
    return {
        MY_NODE_ID: {
            "num": MY_NODE_NUM,
            "user": {"id": MY_NODE_ID, "longName": "HermesBot", "shortName": "HB"},
            "deviceMetrics": {"batteryLevel": 100, "voltage": 4.1,
                              "channelUtilization": 3.5, "airUtilTx": 1.2},
            "lastHeard": 1_700_000_000,
        },
        PEER_NODE_ID: {
            "num": PEER_NODE_NUM,
            "user": {"id": PEER_NODE_ID, "longName": "Alice Radio", "shortName": "ALIC"},
            "snr": 6.25,
            "rssi": -95,
            "hopsAway": 1,
            "lastHeard": 1_700_000_100,
            "deviceMetrics": {"batteryLevel": 72, "voltage": 3.9,
                              "channelUtilization": 4.0, "airUtilTx": 0.8},
            "position": {"latitude": 47.620506, "longitude": -122.349274, "altitude": 56},
        },
        STRANGER_NODE_ID: {
            "num": STRANGER_NODE_NUM,
            "user": {"id": STRANGER_NODE_ID, "longName": "Unknown Node", "shortName": "UNK"},
            "snr": -3.0,
            "hopsAway": 3,
            "lastHeard": 1_700_000_200,
        },
    }


class FakeMeshInterface:
    """Stand-in for a real Meshtastic interface.

    Records outbound sends, exposes a node DB, and can inject inbound
    packets on the real pubsub topics via the ``inject_*`` helpers.
    """

    def __init__(
        self,
        my_node_num: int = MY_NODE_NUM,
        region: str = "US",
        nodes: Optional[Dict[str, Any]] = None,
        fail_send: bool = False,
    ) -> None:
        self.myInfo = _FakeMyInfo(my_node_num)
        self.nodes: Dict[str, Any] = nodes if nodes is not None else _default_nodes()
        self.localConfig = _FakeLocalConfig(region)
        self.sent: List[SentMessage] = []
        self.closed = False
        self.close_count = 0
        self.fail_send = fail_send
        # Channel table as the library exposes it (index → name).
        self.channels = [
            {"index": 0, "name": "LongFast", "primary": True},
            {"index": 1, "name": "Emergency", "primary": False},
        ]
        self._lock = threading.Lock()

    # ── Library surface the adapter uses ──────────────────────────────────

    def sendText(self, text: str, **kwargs: Any) -> Any:
        if self.fail_send:
            raise RuntimeError("radio queue full")
        with self._lock:
            self.sent.append(
                SentMessage(
                    text=text,
                    destination_id=kwargs.get("destinationId"),
                    channel_index=kwargs.get("channelIndex", 0),
                )
            )
        return {"id": len(self.sent)}

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    def getLongName(self) -> str:
        return "HermesBot"

    # ── Test helpers ──────────────────────────────────────────────────────

    @property
    def sent_texts(self) -> List[str]:
        return [m.text for m in self.sent]

    def channel_name(self, index: int) -> Optional[str]:
        for ch in self.channels:
            if ch["index"] == index:
                return ch["name"]
        return None

    def inject_text(
        self,
        text: str,
        from_id: str = PEER_NODE_ID,
        to: Optional[str] = None,
        channel: int = 0,
        packet_id: int = 4242,
    ) -> None:
        """Publish an inbound text packet exactly as the radio would.

        *to* defaults to broadcast; pass our own node id for a DM.
        """
        from pubsub import pub

        from_num = hex_to_node_num(from_id)
        to_num = hex_to_node_num(to) if to else BROADCAST_NUM
        packet = {
            "from": from_num,
            "to": to_num,
            "id": packet_id,
            "channel": channel,
            "rxSnr": 6.25,
            "rxRssi": -95,
            "hopLimit": 3,
            "fromId": from_id,
            "toId": to if to else "^all",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
        }
        pub.sendMessage(TOPIC_RECEIVE_TEXT, packet=packet, interface=self)

    def inject_text_from_thread(self, text: str, **kwargs: Any) -> threading.Thread:
        """Inject from a non-loop thread, as the library's receive thread does.

        Returns the (already started) thread so tests can join it.
        """
        thread = threading.Thread(target=self.inject_text, args=(text,), kwargs=kwargs)
        thread.start()
        return thread

    def inject_connection_established(self) -> None:
        from pubsub import pub

        pub.sendMessage(TOPIC_CONNECTION_ESTABLISHED, interface=self)

    def inject_connection_lost(self) -> None:
        from pubsub import pub

        pub.sendMessage(TOPIC_CONNECTION_LOST, interface=self)

    def inject_node_updated(self, node: Optional[Dict[str, Any]] = None) -> None:
        from pubsub import pub

        payload = node or self.nodes[PEER_NODE_ID]
        pub.sendMessage(TOPIC_NODE_UPDATED, node=payload, interface=self)

    def inject_telemetry(
        self,
        from_id: str = PEER_NODE_ID,
        battery: int = 71,
        voltage: float = 3.88,
        temperature: Optional[float] = None,
    ) -> None:
        from pubsub import pub

        metrics: Dict[str, Any] = {
            "batteryLevel": battery,
            "voltage": voltage,
            "channelUtilization": 4.2,
            "airUtilTx": 0.9,
        }
        telemetry: Dict[str, Any] = {"deviceMetrics": metrics}
        if temperature is not None:
            telemetry["environmentMetrics"] = {
                "temperature": temperature,
                "relativeHumidity": 44.0,
                "barometricPressure": 1013.2,
            }
        packet = {
            "from": hex_to_node_num(from_id),
            "fromId": from_id,
            "to": BROADCAST_NUM,
            "id": 777,
            "decoded": {"portnum": "TELEMETRY_APP", "telemetry": telemetry},
        }
        pub.sendMessage(TOPIC_RECEIVE_TELEMETRY, packet=packet, interface=self)
