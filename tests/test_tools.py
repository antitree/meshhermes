"""Tests for the mesh_* tools.

The invariants here are contractual: every handler returns a JSON *string*,
and no handler ever raises.  A dict return or an escaped exception breaks
the agent's tool loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

import mesh_tools as tools_mod
import transport as tp
from adapter import MeshtasticAdapter
from fake_mesh import MY_NODE_ID, PEER_NODE_ID, FakeMeshInterface

ALL_HANDLERS = [
    tools_mod.mesh_nodes_handler,
    tools_mod.mesh_telemetry_handler,
    tools_mod.mesh_channels_handler,
    tools_mod.mesh_send_handler,
]


@dataclass
class FakeConfig:
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    tools_mod._reset_rate_limit()
    yield
    tools_mod._reset_rate_limit()


@pytest.fixture
def no_gateway(monkeypatch):
    monkeypatch.setattr(tools_mod, "_live_adapter", lambda: None)


@pytest.fixture
async def live(monkeypatch):
    """A connected adapter installed as the live gateway adapter."""

    async def _build(**extra: Any):
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)

        config: Dict[str, Any] = {
            "transport": "serial",
            "serial_port": "/dev/ttyFAKE",
            "chunk_delay_seconds": 0,
            "dm_policy": "open",
            "allow_from": ["*"],
        }
        config.update(extra)
        adapter = MeshtasticAdapter(FakeConfig(extra=config))
        await adapter.connect()
        monkeypatch.setattr(tools_mod, "_live_adapter", lambda: adapter)
        return adapter, iface

    return _build


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestHandlerContract:
    @pytest.mark.parametrize("handler", ALL_HANDLERS)
    def test_returns_json_string_without_gateway(self, handler, no_gateway):
        result = handler({})
        assert isinstance(result, str), "handlers must return a JSON string, never a dict"
        json.loads(result)

    @pytest.mark.parametrize("handler", ALL_HANDLERS)
    def test_never_raises_on_garbage(self, handler, no_gateway):
        for args in ({}, None, {"limit": "abc"}, {"node_id": 12345}, {"target": None},
                     {"unexpected": object()}):
            result = handler(args)
            assert isinstance(result, str)
            json.loads(result)

    @pytest.mark.parametrize("handler", ALL_HANDLERS)
    def test_accepts_forward_compatible_kwargs(self, handler, no_gateway):
        result = handler({}, session_id="abc", future_arg=123)
        assert isinstance(result, str)

    @pytest.mark.parametrize("handler", ALL_HANDLERS)
    def test_no_gateway_is_a_clean_error(self, handler, no_gateway):
        payload = json.loads(handler({"target": PEER_NODE_ID, "message": "x"}))
        assert "not running" in payload["error"]


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


class TestMeshNodes:
    async def test_lists_known_nodes(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_nodes_handler({}))
        assert payload["node_count"] >= 2
        ids = {n["id"] for n in payload["nodes"]}
        assert PEER_NODE_ID in ids

    async def test_limit_respected(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_nodes_handler({"limit": 1}))
        assert len(payload["nodes"]) == 1

    async def test_position_rounded_by_default(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_nodes_handler({}))
        peer = next(n for n in payload["nodes"] if n["id"] == PEER_NODE_ID)
        # Raw fixture value is 47.620506; 4 dp ≈ 11 m.
        assert peer["position"]["latitude"] == 47.6205
        assert peer["position"]["longitude"] == -122.3493

    async def test_position_suppressed_when_disabled(self, live, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_EXPOSE_POSITION", "false")
        await live()
        payload = json.loads(tools_mod.mesh_nodes_handler({}))
        assert payload["position_exposed"] is False
        peer = next(n for n in payload["nodes"] if n["id"] == PEER_NODE_ID)
        assert peer["position"] == "suppressed (MESHTASTIC_EXPOSE_POSITION=false)"

    async def test_custom_precision(self, live):
        await live(position_precision=1)
        payload = json.loads(tools_mod.mesh_nodes_handler({}))
        peer = next(n for n in payload["nodes"] if n["id"] == PEER_NODE_ID)
        assert peer["position"]["latitude"] == 47.6

    async def test_sort_by_name(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_nodes_handler({"sort_by": "name"}))
        names = [n.get("long_name", "") for n in payload["nodes"]]
        assert names == sorted(names, key=str.lower)


class TestMeshTelemetry:
    async def test_all_nodes_by_default(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_telemetry_handler({}))
        assert payload["count"] >= 2

    async def test_single_node(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_telemetry_handler({"node_id": PEER_NODE_ID}))
        assert payload["count"] == 1
        entry = payload["telemetry"][0]
        assert entry["node_id"] == PEER_NODE_ID
        assert entry["current"]["battery_level"] == 72

    async def test_accepts_alternate_node_id_spellings(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_telemetry_handler({"node_id": "AABBCCDD"}))
        assert payload["telemetry"][0]["node_id"] == PEER_NODE_ID

    async def test_invalid_node_id_is_an_error_not_a_crash(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_telemetry_handler({"node_id": "not-a-node"}))
        assert "invalid node_id" in payload["error"]

    async def test_history_included(self, live):
        adapter, iface = await live()
        iface.inject_telemetry(from_id=PEER_NODE_ID, battery=55)
        import asyncio

        await asyncio.sleep(0.1)
        payload = json.loads(tools_mod.mesh_telemetry_handler({"node_id": PEER_NODE_ID}))
        assert payload["telemetry"][0]["history"][-1]["batteryLevel"] == 55

    async def test_unknown_node_reported_cleanly(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_telemetry_handler({"node_id": "!deadbeef"}))
        assert payload["telemetry"][0]["note"] == "node not known to this radio"


class TestMeshChannels:
    async def test_lists_channels(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_channels_handler({}))
        assert payload["count"] == 2
        names = [c["name"] for c in payload["channels"]]
        assert "LongFast" in names and "Emergency" in names
        assert payload["channels"][0]["primary"] is True


# ---------------------------------------------------------------------------
# mesh_send — gating and rate limiting
# ---------------------------------------------------------------------------


class TestMeshSendValidation:
    async def test_requires_target_and_message(self, live):
        await live()
        assert "target is required" in json.loads(tools_mod.mesh_send_handler({}))["error"]
        assert "message is required" in json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID})
        )["error"]

    async def test_invalid_target_rejected(self, live):
        await live()
        payload = json.loads(tools_mod.mesh_send_handler({"target": "   ", "message": "x"}))
        assert "error" in payload


class TestMeshSendPolicy:
    async def test_dm_disabled_refuses(self, live):
        await live(dm_policy="disabled")
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload["permitted"] is False
        assert "disabled" in payload["error"]

    async def test_allowlist_permits_listed_node(self, live):
        await live(dm_policy="allowlist", allow_from=[PEER_NODE_ID])
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload.get("success") is True

    async def test_allowlist_refuses_unlisted_node(self, live):
        await live(dm_policy="allowlist", allow_from=[PEER_NODE_ID])
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": "!deadbeef", "message": "hi"})
        )
        assert payload["permitted"] is False

    async def test_allowlist_matches_across_id_spellings(self, live):
        await live(dm_policy="allowlist", allow_from=["AABBCCDD"])
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload.get("success") is True

    async def test_env_allowlist_honoured(self, live, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_ALLOWED_USERS", PEER_NODE_ID)
        await live(dm_policy="allowlist", allow_from=[])
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "hi"})
        )
        assert payload.get("success") is True

    async def test_group_disabled_refuses_channel(self, live):
        await live(group_policy="disabled")
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": "channel:LongFast", "message": "hi"})
        )
        assert payload["permitted"] is False

    async def test_group_allowlist_permits_configured_channel(self, live):
        await live(group_policy="allowlist", channels={"LongFast": {}})
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": "channel:LongFast", "message": "hi"})
        )
        assert payload.get("success") is True

    async def test_group_allowlist_refuses_other_channel(self, live):
        await live(group_policy="allowlist", channels={"LongFast": {}})
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": "channel:Secret", "message": "hi"})
        )
        assert payload["permitted"] is False


class TestMeshSendDelivery:
    async def test_message_reaches_the_radio(self, live):
        import asyncio

        adapter, iface = await live()
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "hello radio"})
        )
        assert payload["success"] is True
        # Called from inside the gateway's own loop, the handler schedules
        # the transmission and reports acceptance rather than blocking the
        # loop it is running on.
        if payload.get("queued"):
            await asyncio.sleep(0.1)
        assert iface.sent_texts == ["hello radio"]
        assert iface.sent[0].destination_id == PEER_NODE_ID

    async def test_byte_count_reported(self, live):
        await live()
        payload = json.loads(
            tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": "héllo"})
        )
        assert payload["bytes"] == len("héllo".encode("utf-8"))

    async def test_rate_limit_trips(self, live):
        await live()
        results = [
            json.loads(
                tools_mod.mesh_send_handler({"target": PEER_NODE_ID, "message": f"m{i}"})
            )
            for i in range(tools_mod._RATE_LIMIT_MAX_SENDS + 2)
        ]
        assert any("rate limit" in r.get("error", "") for r in results), (
            "airtime is a shared regulated resource — sends must be rate limited"
        )
        successes = [r for r in results if r.get("success")]
        assert len(successes) == tools_mod._RATE_LIMIT_MAX_SENDS
