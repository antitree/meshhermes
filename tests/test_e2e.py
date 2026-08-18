"""Full-path tests through the fake transport.

These are the tests that matter most.  They drive inbound packets onto the
**real** pubsub topics and assert on what actually reaches the radio, so the
adapter's genuine subscription wiring and thread bridge are exercised rather
than mocked away.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

import transport as tp
from adapter import MeshtasticAdapter
from chunking import MESHTASTIC_HARD_LIMIT
from fake_mesh import (
    MY_NODE_ID,
    PEER_NODE_ID,
    STRANGER_NODE_ID,
    FakeMeshInterface,
)


@dataclass
class FakeConfig:
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


class Harness:
    """A connected adapter plus the events it dispatched."""

    def __init__(self, adapter: MeshtasticAdapter, iface: FakeMeshInterface) -> None:
        self.adapter = adapter
        self.iface = iface
        self.events: List[Any] = []

    async def wait_for_events(self, count: int = 1, timeout: float = 2.0) -> bool:
        """Wait until *count* events arrive, or time out.

        Inbound packets cross a thread boundary, so the assertion cannot be
        made synchronously right after injection.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.events) < count:
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def settle(self, seconds: float = 0.15) -> None:
        """Give any in-flight bridged callback a chance to run."""
        await asyncio.sleep(seconds)


@pytest.fixture
async def harness(monkeypatch):
    """Factory for a connected adapter wired to a fake radio."""
    created: List[Harness] = []

    async def _build(**extra: Any) -> Harness:
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)

        config: Dict[str, Any] = {
            "transport": "serial",
            "serial_port": "/dev/ttyFAKE",
            "node_name": "HermesBot",
            "chunk_delay_seconds": 0,
        }
        config.update(extra)
        adapter = MeshtasticAdapter(FakeConfig(extra=config))

        h = Harness(adapter, iface)

        # Capture events at the adapter's own boundary rather than at
        # ``_message_handler``.  The real ``handle_message`` spawns
        # background tasks and serialises same-session messages behind an
        # active-session guard, so asserting on the handler would be
        # testing the base class's queueing, not this plugin's dispatch.
        original_handle = adapter.handle_message

        async def capturing_handle(event):
            h.events.append(event)
            return await original_handle(event)

        adapter.handle_message = capturing_handle  # type: ignore[method-assign]

        async def handler(event):
            return None

        adapter.set_message_handler(handler)
        assert await adapter.connect() is True
        created.append(h)
        return h

    yield _build

    for h in created:
        await h.adapter.disconnect()


# ---------------------------------------------------------------------------
# 1. DM round-trip
# ---------------------------------------------------------------------------


class TestDMRoundTrip:
    async def test_inbound_dm_dispatches_correctly(self, harness):
        h = await harness()
        h.iface.inject_text("what is the battery level?", from_id=PEER_NODE_ID, to=MY_NODE_ID)

        assert await h.wait_for_events(1), "DM did not reach the adapter"
        event = h.events[0]
        assert event.text == "what is the battery level?"
        assert event.source.chat_type == "dm"
        assert event.source.user_id == PEER_NODE_ID
        assert event.source.user_name == "Alice Radio"
        assert event.source.chat_id == PEER_NODE_ID

    async def test_outbound_reply_is_chunked_plain_text(self, harness):
        h = await harness()
        reply = "**Status report:** " + " ".join(f"item{i}" for i in range(80))

        result = await h.adapter.send(PEER_NODE_ID, reply)

        assert result.success is True
        assert len(h.iface.sent) > 1, "a long reply must be split across frames"
        for msg in h.iface.sent:
            assert msg.byte_length <= MESHTASTIC_HARD_LIMIT
            assert "**" not in msg.text
            assert msg.destination_id == PEER_NODE_ID
        # Order preserved and content intact.
        joined = " ".join(m.text for m in h.iface.sent)
        assert joined.startswith("Status report:")
        assert "item79" in joined

    async def test_full_round_trip(self, harness):
        h = await harness()
        h.iface.inject_text("ping", from_id=PEER_NODE_ID, to=MY_NODE_ID)
        assert await h.wait_for_events(1)

        await h.adapter.send(h.events[0].source.chat_id, "pong")
        assert h.iface.sent_texts == ["pong"]


# ---------------------------------------------------------------------------
# 2. Channel mention gating
# ---------------------------------------------------------------------------


class TestChannelGating:
    async def test_unaddressed_channel_message_is_ignored(self, harness):
        h = await harness(
            group_policy="allowlist",
            channels={"LongFast": {"require_mention": True}},
        )
        h.iface.inject_text("just chatting with friends", from_id=PEER_NODE_ID, channel=0)

        await h.settle()
        assert h.events == [], "an unaddressed channel message must not wake the agent"

    async def test_addressed_channel_message_dispatches_stripped(self, harness):
        h = await harness(
            group_policy="allowlist",
            channels={"LongFast": {"require_mention": True}},
        )
        h.iface.inject_text("@HermesBot what is the weather?", from_id=PEER_NODE_ID, channel=0)

        assert await h.wait_for_events(1)
        event = h.events[0]
        assert event.text == "what is the weather?", "the mention must be stripped"
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "LongFast"

    async def test_require_mention_false_lets_everything_through(self, harness):
        h = await harness(channels={"Emergency": {"require_mention": False}})
        h.iface.inject_text("flooding on main street", from_id=PEER_NODE_ID, channel=1)

        assert await h.wait_for_events(1)
        assert h.events[0].source.chat_id == "Emergency"

    async def test_wildcard_channel_config_applies(self, harness):
        h = await harness(channels={"*": {"require_mention": False}})
        h.iface.inject_text("anything at all", from_id=PEER_NODE_ID, channel=0)
        assert await h.wait_for_events(1)

    async def test_colon_addressing_works(self, harness):
        h = await harness(channels={"LongFast": {"require_mention": True}})
        h.iface.inject_text("HermesBot: status", from_id=PEER_NODE_ID, channel=0)
        assert await h.wait_for_events(1)
        assert h.events[0].text == "status"


# ---------------------------------------------------------------------------
# 3. Self-message loop guard
# ---------------------------------------------------------------------------


class TestSelfMessageGuard:
    async def test_own_packets_are_dropped(self, harness):
        h = await harness()
        # Without this guard the bot answers itself forever, burning the
        # shared band.
        h.iface.inject_text("my own broadcast", from_id=MY_NODE_ID, channel=0)

        await h.settle()
        assert h.events == [], "the adapter must never dispatch its own packets"

    async def test_own_dm_also_dropped(self, harness):
        h = await harness()
        h.iface.inject_text("echo", from_id=MY_NODE_ID, to=MY_NODE_ID)
        await h.settle()
        assert h.events == []

    async def test_other_nodes_still_dispatch(self, harness):
        h = await harness()
        h.iface.inject_text("hello", from_id=STRANGER_NODE_ID, to=MY_NODE_ID)
        assert await h.wait_for_events(1)


# ---------------------------------------------------------------------------
# 4. Disconnect reporting — the gateway owns the retry
# ---------------------------------------------------------------------------


class TestDisconnectReporting:
    async def test_connection_lost_marks_disconnected(self, harness):
        h = await harness()
        assert h.adapter.is_connected is True

        h.iface.inject_connection_lost()
        await h.settle()

        assert h.adapter.is_connected is False

    async def test_reconnect_by_the_gateway_succeeds(self, harness, monkeypatch):
        h = await harness()
        h.iface.inject_connection_lost()
        await h.settle()
        assert h.adapter.is_connected is False

        # Exactly what GatewayRunner._platform_reconnect_watcher does.
        assert await h.adapter.connect(is_reconnect=True) is True
        assert h.adapter.is_connected is True

    async def test_messages_flow_again_after_reconnect(self, harness):
        h = await harness()
        h.iface.inject_connection_lost()
        await h.settle()
        await h.adapter.connect(is_reconnect=True)

        h.iface.inject_text("still there?", from_id=PEER_NODE_ID, to=MY_NODE_ID)
        assert await h.wait_for_events(1)


# ---------------------------------------------------------------------------
# 5. Thread bridge — the easiest thing to get wrong
# ---------------------------------------------------------------------------


class TestThreadBridge:
    async def test_packet_injected_from_another_thread_reaches_the_loop(self, harness):
        h = await harness()
        # The real library always calls back from its receive thread.  If
        # run_coroutine_threadsafe were missing, this would silently do
        # nothing — no error, no message.
        thread = h.iface.inject_text_from_thread(
            "from the receive thread", from_id=PEER_NODE_ID, to=MY_NODE_ID
        )
        thread.join(timeout=2)

        assert await h.wait_for_events(1), "thread-bridged packet never reached the event loop"
        assert h.events[0].text == "from the receive thread"

    async def test_many_threaded_packets_all_arrive(self, harness):
        h = await harness()
        threads = [
            h.iface.inject_text_from_thread(f"msg{i}", from_id=PEER_NODE_ID, to=MY_NODE_ID)
            for i in range(5)
        ]
        for t in threads:
            t.join(timeout=2)

        assert await h.wait_for_events(5)
        assert {e.text for e in h.events} == {f"msg{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# Cross-talk: pub.subscribe is global
# ---------------------------------------------------------------------------


class TestInterfaceIsolation:
    async def test_packets_from_another_interface_are_ignored(self, harness):
        h = await harness()
        other = FakeMeshInterface()  # a second radio, not ours

        other.inject_text("not for us", from_id=PEER_NODE_ID, to=MY_NODE_ID)
        await h.settle()

        assert h.events == [], "callbacks must filter on interface identity"

    async def test_unsubscribe_on_disconnect_stops_delivery(self, harness):
        h = await harness()
        await h.adapter.disconnect()

        h.iface.inject_text("after disconnect", from_id=PEER_NODE_ID, to=MY_NODE_ID)
        await h.settle()

        assert h.events == []


# ---------------------------------------------------------------------------
# Telemetry feeds the node cache
# ---------------------------------------------------------------------------


class TestTelemetryIngest:
    async def test_telemetry_packet_recorded(self, harness):
        h = await harness()
        h.iface.inject_telemetry(from_id=PEER_NODE_ID, battery=64, voltage=3.7)
        await h.settle()

        samples = h.adapter.nodedb.telemetry_history(PEER_NODE_ID)
        assert samples and samples[-1]["batteryLevel"] == 64

    async def test_node_update_refreshes_cache(self, harness):
        h = await harness()
        h.iface.inject_node_updated(
            {"num": 0xAABBCCDD, "user": {"id": PEER_NODE_ID, "longName": "Renamed"}}
        )
        await h.settle()

        record = h.adapter.nodedb.get_node(PEER_NODE_ID)
        assert record["user"]["longName"] == "Renamed"
