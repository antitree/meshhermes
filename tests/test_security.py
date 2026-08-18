"""Regression tests for the MH-SEC-* security review findings.

Each class pins one finding.  These are the tests that would have caught
the issue, so they are written to fail against the pre-fix behaviour.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

import adapter as adapter_mod
import mesh_tools
import sendpolicy as sp
import transport as tp
from adapter import MeshtasticAdapter
from fake_mesh import (
    MY_NODE_ID,
    PEER_NODE_ID,
    PEER_NODE_NUM,
    STRANGER_NODE_ID,
    FakeMeshInterface,
)


@dataclass
class FakeConfig:
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    sp.reset_rate_limit()
    yield
    sp.reset_rate_limit()


@pytest.fixture
def live(monkeypatch):
    """Build a connected adapter registered as the live one for tools."""

    async def _build(**extra: Any):
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        cfg: Dict[str, Any] = {
            "transport": "serial",
            "serial_port": "/dev/ttyFAKE",
            "chunk_delay_seconds": 0,
            "dm_policy": "open",
            "allow_from": ["*"],
        }
        cfg.update(extra)
        adapter = MeshtasticAdapter(FakeConfig(extra=cfg))
        await adapter.connect()
        monkeypatch.setattr(mesh_tools, "_live_adapter", lambda: adapter)
        return adapter, iface

    return _build


class TestQueuedSendPreconditions:
    """MH-SEC-001: queued success must not outrun validation.

    Called from the gateway loop, mesh_send cannot await the send, so it
    reports acceptance.  Anything checkable synchronously must therefore be
    checked BEFORE that report, or the agent is told a message was sent
    when nothing reached the radio.
    """

    async def test_unknown_channel_is_an_error_not_queued_success(self, live):
        adapter, iface = await live(group_policy="allowlist", channels={"Missing": {}})

        payload = json.loads(
            mesh_tools.mesh_send_handler({"target": "channel:Missing", "message": "hi"})
        )

        assert "error" in payload, f"expected an error, got {payload}"
        assert payload.get("success") is not True
        await asyncio.sleep(0.05)
        assert iface.sent == [], "nothing must reach the radio"

    async def test_valid_channel_still_accepted(self, live):
        adapter, iface = await live(group_policy="allowlist", channels={"LongFast": {}})
        payload = json.loads(
            mesh_tools.mesh_send_handler({"target": "channel:LongFast", "message": "hi"})
        )
        assert payload.get("success") is True
        if payload.get("queued"):
            await asyncio.sleep(0.1)
        assert iface.sent_texts == ["hi"]


class TestSendAfterConnectionLoss:
    """MH-SEC-003: a lost connection must stop outbound traffic.

    _iface stays set after connection loss (the gateway owns reconnection),
    so an _iface-only check kept transmitting through a dead link.
    """

    async def test_tool_refuses_after_connection_lost(self, live):
        adapter, iface = await live()
        iface.inject_connection_lost()
        await asyncio.sleep(0.1)
        assert adapter.is_connected is False

        payload = json.loads(
            mesh_tools.mesh_send_handler({"target": PEER_NODE_ID, "message": "after loss"})
        )

        assert "error" in payload
        assert "not connected" in payload["error"].lower()
        await asyncio.sleep(0.05)
        assert iface.sent == []

    async def test_adapter_send_refuses_after_connection_lost(self, live):
        adapter, iface = await live()
        iface.inject_connection_lost()
        await asyncio.sleep(0.1)

        result = await adapter.send(PEER_NODE_ID, "after loss")

        assert result.success is False
        assert "not connected" in (result.error or "").lower()
        assert iface.sent == []


class TestStandaloneSendIsGated:
    """MH-SEC-002: the cron/standalone path shares the same gate.

    _standalone_send is reachable from tools/send_message_tool.py — the same
    agent surface as mesh_send — so an ungated path there would let an agent
    bypass every policy by choosing the other tool.
    """

    @pytest.fixture
    def no_radio(self, monkeypatch):
        """Fail loudly if a denied send ever reaches the transport."""
        opened = []

        async def _open(**kwargs):
            opened.append(kwargs)
            raise AssertionError("policy-denied send opened the radio")

        monkeypatch.setattr(tp, "open_interface", _open)
        monkeypatch.setattr(adapter_mod, "check_requirements", lambda: True)
        return opened

    async def test_dm_policy_disabled_blocks_standalone(self, no_radio):
        cfg = FakeConfig(extra={"transport": "serial", "dm_policy": "disabled"})
        result = await adapter_mod._standalone_send(cfg, PEER_NODE_ID, "hi")
        assert "error" in result
        assert "disabled" in result["error"]
        assert no_radio == []

    async def test_group_policy_disabled_blocks_standalone(self, no_radio):
        cfg = FakeConfig(extra={"transport": "serial", "group_policy": "disabled"})
        result = await adapter_mod._standalone_send(cfg, "channel:LongFast", "hi")
        assert "error" in result
        assert "disabled" in result["error"]
        assert no_radio == []

    async def test_unlisted_node_blocked_under_allowlist(self, no_radio):
        cfg = FakeConfig(extra={
            "transport": "serial", "dm_policy": "allowlist", "allow_from": [PEER_NODE_ID],
        })
        result = await adapter_mod._standalone_send(cfg, "!deadbeef", "hi")
        assert "error" in result
        assert no_radio == []

    async def test_rate_limit_applies_to_standalone(self, no_radio):
        cfg = FakeConfig(extra={
            "transport": "serial", "dm_policy": "open", "allow_from": ["*"],
        })
        # Exhaust the shared bucket, then confirm standalone respects it.
        for _ in range(sp.RATE_LIMIT_MAX_SENDS):
            assert sp.rate_limit_ok() is True
        result = await adapter_mod._standalone_send(cfg, PEER_NODE_ID, "hi")
        assert "rate limit" in result.get("error", "")
        assert no_radio == []


class TestOpenPolicyRequiresExplicitWildcard:
    """MH-SEC-004: 'open' without an explicit '*' must not open the radio.

    validate_config() enforces this, but an adapter constructed directly
    (tests, a drifting config path) skipped it entirely at send time.
    """

    async def test_open_without_wildcard_is_denied(self, live):
        adapter, iface = await live(dm_policy="open", allow_from=[])
        payload = json.loads(
            mesh_tools.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload.get("permitted") is False
        assert "explicit opt-in" in payload["error"]
        await asyncio.sleep(0.05)
        assert iface.sent == []

    async def test_open_with_wildcard_is_permitted(self, live):
        adapter, iface = await live(dm_policy="open", allow_from=["*"])
        payload = json.loads(
            mesh_tools.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload.get("success") is True

    def test_policy_layer_directly(self):
        denied = sp.check_send_permitted(
            sp.SendPolicy(dm_policy="open", allow_from=[]), PEER_NODE_ID
        )
        assert denied is not None
        allowed = sp.check_send_permitted(
            sp.SendPolicy(dm_policy="open", allow_from=["*"]), PEER_NODE_ID
        )
        assert allowed is None


class TestSenderIdentity:
    """MH-SEC-005: numeric `from` is the canonical sender identity."""

    @pytest.fixture
    async def harness(self, monkeypatch):
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        adapter = MeshtasticAdapter(FakeConfig(extra={
            "transport": "serial", "node_name": "HermesBot",
            "chunk_delay_seconds": 0, "group_policy": "open",
            "channels": {"*": {"require_mention": False}},
        }))
        events = []
        original = adapter.handle_message

        async def capture(event):
            events.append(event)
            return await original(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter.set_message_handler(lambda e: asyncio.sleep(0))
        await adapter.connect()
        return adapter, iface, events

    async def test_mismatched_identity_is_dropped(self, harness):
        adapter, iface, events = harness
        # numeric `from` says PEER, string `fromId` claims STRANGER.
        packet = {
            "from": PEER_NODE_NUM,
            "fromId": STRANGER_NODE_ID,
            "to": 0xFFFFFFFF,
            "id": 1,
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "spoofed"},
        }
        await adapter._dispatch_message(packet)
        assert events == [], "a packet with conflicting sender identity must be dropped"

    async def test_numeric_from_used_when_fromid_is_garbage(self, harness):
        adapter, iface, events = harness
        packet = {
            "from": PEER_NODE_NUM,
            "fromId": "not-a-node-id",
            "to": 0xFFFFFFFF,
            "id": 2,
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }
        await adapter._dispatch_message(packet)
        assert len(events) == 1
        assert events[0].source.user_id == PEER_NODE_ID

    async def test_agreeing_identities_dispatch(self, harness):
        adapter, iface, events = harness
        packet = {
            "from": PEER_NODE_NUM,
            "fromId": PEER_NODE_ID,
            "to": 0xFFFFFFFF,
            "id": 3,
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }
        await adapter._dispatch_message(packet)
        assert len(events) == 1
        assert events[0].source.user_id == PEER_NODE_ID


class TestNoSlashCommandBypass:
    """MH-SEC-006: a leading '/' is not authorization.

    Attacker-controlled text must not bypass require_mention, or any node
    on a shared channel can wake the agent and burn airtime.
    """

    @pytest.fixture
    async def harness(self, monkeypatch):
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        adapter = MeshtasticAdapter(FakeConfig(extra={
            "transport": "serial", "node_name": "HermesBot",
            "chunk_delay_seconds": 0, "group_policy": "open",
            "channels": {"LongFast": {"require_mention": True}},
        }))
        events = []
        original = adapter.handle_message

        async def capture(event):
            events.append(event)
            return await original(event)

        adapter.handle_message = capture  # type: ignore[method-assign]
        adapter.set_message_handler(lambda e: asyncio.sleep(0))
        await adapter.connect()
        return adapter, iface, events

    async def test_slash_command_does_not_bypass_mention_gate(self, harness):
        adapter, iface, events = harness
        iface.inject_text("/help", from_id=PEER_NODE_ID, channel=0)
        await asyncio.sleep(0.2)
        assert events == [], "an unaddressed /command must not wake the agent"

    async def test_addressed_slash_command_still_works(self, harness):
        adapter, iface, events = harness
        iface.inject_text("@HermesBot /help", from_id=PEER_NODE_ID, channel=0)
        await asyncio.sleep(0.2)
        assert len(events) == 1
        assert events[0].text == "/help"


class TestPositionPrecisionClamped:
    """MH-SEC-008: GPS precision must not be configurable past ~11 m."""

    def test_high_precision_is_clamped(self):
        a = MeshtasticAdapter(FakeConfig(extra={
            "transport": "serial", "position_precision": 12,
        }))
        assert a.position_precision == 4

    def test_negative_precision_clamped_to_zero(self):
        a = MeshtasticAdapter(FakeConfig(extra={
            "transport": "serial", "position_precision": -5,
        }))
        assert a.position_precision == 0

    def test_coarser_precision_is_allowed(self):
        a = MeshtasticAdapter(FakeConfig(extra={
            "transport": "serial", "position_precision": 1,
        }))
        assert a.position_precision == 1

    async def test_tool_output_never_exceeds_safe_precision(self, live):
        adapter, iface = await live(position_precision=12)
        payload = json.loads(mesh_tools.mesh_nodes_handler({}))
        peer = next(n for n in payload["nodes"] if n["id"] == PEER_NODE_ID)
        lat = peer["position"]["latitude"]
        # 47.620506 rounded to 4 dp; more decimals would mean the clamp failed.
        assert lat == 47.6205
        assert len(str(lat).split(".")[-1]) <= 4


class TestBridgeObservesFailures:
    """MH-SEC-009: a raising bridged coroutine must be logged, not silent."""

    async def test_exception_is_logged_and_does_not_escape(self, caplog):
        loop = asyncio.get_running_loop()

        async def boom():
            raise RuntimeError("bridged failure")

        with caplog.at_level("ERROR"):
            tp.bridge_to_loop(loop, boom)
            await asyncio.sleep(0.15)

        assert any("bridged callback raised" in r.message or "bridged failure" in str(r.msg)
                   for r in caplog.records), f"no error logged: {[r.message for r in caplog.records]}"

    async def test_successful_bridge_logs_nothing(self, caplog):
        loop = asyncio.get_running_loop()
        ran = []

        async def fine():
            ran.append(True)

        with caplog.at_level("ERROR"):
            tp.bridge_to_loop(loop, fine)
            await asyncio.sleep(0.15)

        assert ran == [True]
        assert [r for r in caplog.records if "raised" in r.message] == []
