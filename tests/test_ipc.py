"""Tests for the generic local application gateway."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

import adapter as adapter_mod
import transport as tp
from fake_mesh import PEER_NODE_ID, FakeMeshInterface
from ipc import hello_payload, message_payload, validate_send_request


@dataclass
class FakeConfig:
    extra: dict[str, Any] = field(default_factory=dict)


def test_send_request_is_pinned_to_configured_channel():
    request = {"op": "send", "version": 1, "text": "hello",
               "channel_name": "Emergency", "channel_index": 1}
    assert validate_send_request(request, channel_name="Emergency", channel_index=1) == ("hello", None)
    assert validate_send_request({**request, "channel_index": 0}, channel_name="Emergency", channel_index=1)[1]
    assert validate_send_request({**request, "channel_name": "LongFast"}, channel_name="Emergency", channel_index=1)[1]
    assert validate_send_request({**request, "version": 99}, channel_name="Emergency", channel_index=1)[1]


def test_payloads_include_protocol_version():
    assert hello_payload("!aabbccdd", "Emergency", 1)["version"] == 1
    payload = message_payload({"text": "/status", "from_id": PEER_NODE_ID}, "Emergency", 1)
    assert payload["type"] == "message"
    assert payload["channel_index"] == 1


@pytest.mark.asyncio
async def test_application_ipc_forwards_configured_channel(monkeypatch, tmp_path):
    socket_path = tmp_path / "gateway.sock"
    monkeypatch.setenv("MESHTASTIC_IPC_SOCKET", str(socket_path))
    monkeypatch.setenv("MESHTASTIC_IPC_CHANNEL", "Emergency")
    iface = FakeMeshInterface()

    async def open_interface(**kwargs):
        return iface

    monkeypatch.setattr(tp, "open_interface", open_interface)
    adapter = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "serial"}))
    await adapter.connect()

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    hello = json.loads((await reader.readline()).decode())
    assert hello == {
        "type": "hello",
        "version": 1,
        "node_id": "!11223344",
        "channel_name": "Emergency",
        "channel_index": 1,
    }

    writer.write((json.dumps({
        "op": "send",
        "version": 1,
        "text": "reply",
        "channel_name": "Emergency",
        "channel_index": 1,
    }) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    assert response["ok"] is True, response
    assert iface.sent[-1].channel_index == 1
    assert iface.sent[-1].text == "reply"

    await adapter._dispatch_message({
        "from": int(PEER_NODE_ID[1:], 16),
        "fromId": PEER_NODE_ID,
        "to": 0xFFFFFFFF,
        "id": 42,
        "channel": 1,
        "decoded": {"text": "/status"},
    })
    event = json.loads(await reader.readline())
    assert event["type"] == "message"
    assert event["text"] == "/status"
    assert event["channel_index"] == 1

    writer.close()
    await writer.wait_closed()
    await adapter.disconnect()
