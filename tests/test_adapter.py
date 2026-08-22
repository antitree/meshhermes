"""Adapter unit tests: config, validation, lifecycle, outbound, media."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

import adapter as adapter_mod
import transport as tp
from adapter import MeshtasticAdapter, check_requirements, is_connected, validate_config
from chunking import MESHTASTIC_HARD_LIMIT
from fake_mesh import MY_NODE_ID, PEER_NODE_ID, FakeMeshInterface


@dataclass
class FakeConfig:
    """Stand-in for Hermes' PlatformConfig."""

    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


def make_adapter(**extra: Any) -> MeshtasticAdapter:
    base: Dict[str, Any] = {"transport": "serial", "serial_port": "/dev/ttyFAKE"}
    base.update(extra)
    return MeshtasticAdapter(FakeConfig(extra=base))


@pytest.fixture
def connected(monkeypatch):
    """An adapter connected to a FakeMeshInterface."""
    iface = FakeMeshInterface()

    async def _open(**kwargs):
        return iface

    monkeypatch.setattr(tp, "open_interface", _open)
    return iface


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_defaults(self):
        a = make_adapter()
        assert a.transport == "serial"
        assert a.chunk_limit == 200
        assert a.chunk_delay == 1.5
        # Safe defaults: DMs need pairing, groups are off.
        assert a._dm_policy == "pairing"
        assert a._group_policy == "disabled"

    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_TCP_HOST", "radio.local")
        a = MeshtasticAdapter(FakeConfig(extra={"transport": "serial", "tcp_host": "ignored"}))
        assert a.transport == "tcp"
        assert a.tcp_host == "radio.local"

    def test_policies_exposed_for_gateway(self):
        # The gateway's authz layer reads these; the adapter never enforces them.
        a = make_adapter(dm_policy="ALLOWLIST", group_policy="Open")
        assert a._dm_policy == "allowlist"
        assert a._group_policy == "open"

    def test_channels_and_allow_from(self):
        a = make_adapter(channels={"LongFast": {"require_mention": False}}, allow_from=["!aabbccdd"])
        assert a.channels == {"LongFast": {"require_mention": False}}
        assert a.allow_from == ["!aabbccdd"]

    def test_invalid_numbers_fall_back_to_defaults(self):
        a = make_adapter(text_chunk_limit="nonsense", chunk_delay_seconds="nope")
        assert a.chunk_limit == 200
        assert a.chunk_delay == 1.5

    def test_expose_position_env(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_EXPOSE_POSITION", "false")
        assert make_adapter().expose_position is False

    def test_name_property(self):
        assert make_adapter().name == "Meshtastic"


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_serial_ok_without_port(self):
        # A single attached radio is autodetected by the library.
        assert validate_config(FakeConfig(extra={"transport": "serial"})) is True

    def test_tcp_requires_host(self):
        assert validate_config(FakeConfig(extra={"transport": "tcp"})) is False
        assert validate_config(FakeConfig(extra={"transport": "tcp", "tcp_host": "r.local"})) is True

    @pytest.mark.parametrize("unsupported", ["ble", "mqtt", "BLE", "MQTT"])
    def test_ble_and_mqtt_rejected_explicitly(self, unsupported):
        # Must fail loudly, never silently.
        assert validate_config(FakeConfig(extra={"transport": unsupported})) is False

    def test_unknown_transport_rejected(self):
        assert validate_config(FakeConfig(extra={"transport": "carrier-pigeon"})) is False

    def test_open_dm_policy_requires_explicit_wildcard(self):
        # Guard against accidentally opening a radio to the whole mesh.
        assert validate_config(FakeConfig(extra={"transport": "serial", "dm_policy": "open"})) is False
        assert validate_config(
            FakeConfig(extra={"transport": "serial", "dm_policy": "open", "allow_from": ["*"]})
        ) is True

    def test_invalid_policies_rejected(self):
        assert validate_config(FakeConfig(extra={"transport": "serial", "dm_policy": "yolo"})) is False
        assert validate_config(FakeConfig(extra={"transport": "serial", "group_policy": "yolo"})) is False

    @pytest.mark.parametrize("limit,ok", [(49, False), (50, True), (200, True), (230, True), (231, False)])
    def test_chunk_limit_range(self, limit, ok):
        assert validate_config(FakeConfig(extra={"transport": "serial", "text_chunk_limit": limit})) is ok

    def test_non_integer_chunk_limit_rejected(self):
        assert validate_config(FakeConfig(extra={"transport": "serial", "text_chunk_limit": "big"})) is False


class TestRequirementsAndStatus:
    def test_check_requirements_is_a_bool(self):
        assert isinstance(check_requirements(), bool)

    def test_is_connected_needs_transport(self):
        assert is_connected(FakeConfig(extra={})) is False
        assert is_connected(FakeConfig(extra={"transport": "serial"})) is True
        assert is_connected(FakeConfig(extra={"transport": "tcp"})) is False
        assert is_connected(FakeConfig(extra={"transport": "tcp", "tcp_host": "r"})) is True


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_connect_marks_connected(self, connected):
        a = make_adapter()
        assert await a.connect() is True
        assert a.is_connected is True
        assert a.my_node_id == MY_NODE_ID
        await a.disconnect()

    async def test_connect_seeds_nodedb(self, connected):
        a = make_adapter()
        await a.connect()
        assert a.nodedb.node_count() >= 2
        await a.disconnect()

    async def test_disconnect_releases_the_port(self, connected):
        a = make_adapter()
        await a.connect()
        await a.disconnect()
        assert connected.closed is True
        assert a.is_connected is False

    async def test_disconnect_is_idempotent(self, connected):
        a = make_adapter()
        await a.connect()
        await a.disconnect()
        await a.disconnect()  # must not raise

    async def test_ble_transport_is_fatal_and_not_retryable(self):
        a = make_adapter(transport="ble")
        assert await a.connect() is False
        assert a.has_fatal_error is True
        assert a.fatal_error_retryable is False
        assert "not supported" in (a.fatal_error_message or "")

    async def test_tcp_without_host_is_fatal(self):
        a = MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        assert await a.connect() is False
        assert a.fatal_error_code == "config_missing"
        assert a.fatal_error_retryable is False

    async def test_unset_region_refuses_and_releases_port(self, monkeypatch):
        iface = FakeMeshInterface(region="UNSET")

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        a = make_adapter()
        assert await a.connect() is False
        assert a.fatal_error_code == "region_unset"
        assert a.fatal_error_retryable is False
        # Must not hold the port open after refusing.
        assert iface.closed is True

    async def test_open_failure_is_retryable(self, monkeypatch):
        async def _open(**kwargs):
            raise tp.TransportError("could not open /dev/ttyUSB0")

        monkeypatch.setattr(tp, "open_interface", _open)
        a = make_adapter()
        assert await a.connect() is False
        assert a.fatal_error_retryable is True

    async def test_reconnect_flag_accepted(self, connected):
        a = make_adapter()
        assert await a.connect(is_reconnect=True) is True
        await a.disconnect()

    async def test_no_self_retry_task_is_created(self, connected, monkeypatch):
        # The gateway owns reconnection; a second loop would fight for the port.
        created = []
        real_create_task = asyncio.create_task

        def spy(coro, *args, **kwargs):
            created.append(coro)
            return real_create_task(coro, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", spy)
        a = make_adapter()
        await a.connect()
        await a.disconnect()
        assert created == []


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


class TestSend:
    async def test_send_when_disconnected(self):
        result = await make_adapter().send(PEER_NODE_ID, "hi")
        assert result.success is False
        assert "Not connected" in result.error

    async def test_dm_reaches_the_radio(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        result = await a.send(PEER_NODE_ID, "hello there")
        assert result.success is True
        assert connected.sent[0].text == "hello there"
        assert connected.sent[0].destination_id == PEER_NODE_ID
        await a.disconnect()

    async def test_channel_send_broadcasts_on_the_right_index(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        await a.send("Emergency", "evac now")
        assert connected.sent[0].destination_id is None
        assert connected.sent[0].channel_index == 1
        await a.disconnect()

    async def test_markdown_stripped_before_transmit(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        await a.send(PEER_NODE_ID, "**bold** and `code`")
        assert connected.sent[0].text == "bold and code"
        await a.disconnect()

    async def test_long_message_chunked_within_frame(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        await a.send(PEER_NODE_ID, " ".join(["word"] * 200))
        assert len(connected.sent) > 1
        for msg in connected.sent:
            assert msg.byte_length <= MESHTASTIC_HARD_LIMIT
        await a.disconnect()

    async def test_chunks_are_paced(self, connected, monkeypatch):
        # The inter-chunk delay is a real LoRa constraint, not incidental.
        slept: list = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        a = make_adapter()
        await a.connect()
        await a.send(PEER_NODE_ID, " ".join(["word"] * 200))
        assert slept and all(s == 1.5 for s in slept)
        # One fewer sleep than chunks — no trailing delay.
        assert len(slept) == len(connected.sent) - 1
        await a.disconnect()

    async def test_empty_message_refused(self, connected):
        a = make_adapter()
        await a.connect()
        result = await a.send(PEER_NODE_ID, "   ")
        assert result.success is False
        await a.disconnect()

    async def test_send_failure_reported(self, monkeypatch):
        iface = FakeMeshInterface(fail_send=True)

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        result = await a.send(PEER_NODE_ID, "hello")
        assert result.success is False
        assert "radio queue full" in result.error
        await a.disconnect()

    async def test_send_typing_is_a_noop(self, connected):
        a = make_adapter()
        await a.connect()
        assert await a.send_typing(PEER_NODE_ID) is None
        assert connected.sent == []
        await a.disconnect()


class TestMediaRefused:
    @pytest.mark.parametrize(
        "method,args",
        [
            ("send_image", ("http://x/y.png",)),
            ("send_image_file", ("/tmp/x.png",)),
            ("send_document", ("/tmp/x.pdf",)),
            ("send_voice", ("/tmp/x.ogg",)),
            ("send_video", ("/tmp/x.mp4",)),
            ("send_animation", ("/tmp/x.gif",)),
        ],
    )
    async def test_media_methods_refuse_clearly(self, method, args):
        a = make_adapter()
        result = await getattr(a, method)(PEER_NODE_ID, *args)
        assert result.success is False
        assert "does not support media" in result.error


class TestChatInfo:
    async def test_dm_chat_info(self, connected):
        a = make_adapter()
        await a.connect()
        info = await a.get_chat_info(PEER_NODE_ID)
        assert info["type"] == "dm"
        assert info["name"] == "Alice Radio"
        await a.disconnect()

    async def test_channel_chat_info(self, connected):
        a = make_adapter()
        await a.connect()
        info = await a.get_chat_info("LongFast")
        assert info["type"] == "group"
        assert info["name"] == "LongFast"
        await a.disconnect()


class TestAccidentalBroadcastGuard:
    """An unresolvable target must never fall through to a mesh-wide broadcast."""

    @pytest.mark.parametrize("bad_target", ["", "   ", None])
    async def test_unresolvable_target_refused(self, connected, bad_target):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()

        result = await a.send(bad_target, "important message")

        assert result.success is False
        assert "Invalid target" in result.error
        # The critical assertion: nothing went over the air.
        assert connected.sent == [], "an invalid target must not broadcast to the whole mesh"
        await a.disconnect()

    async def test_valid_channel_still_broadcasts(self, connected):
        # The guard must not break legitimate channel sends.
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        result = await a.send("LongFast", "hello mesh")
        assert result.success is True
        assert connected.sent[0].destination_id is None
        assert connected.sent[0].channel_index == 0
        await a.disconnect()


class TestRealDeviceShapes:
    """Regression tests for the shapes a REAL radio exposes.

    Verified against a RAK4631: ``iface.localConfig`` and ``iface.channels``
    are both None on a real device — region and channels live under
    ``iface.localNode``.  The original fake mirrored an interface shape the
    library does not actually use, so these bugs passed every test while
    being broken on hardware.
    """

    class _ProtoSettings:
        def __init__(self, name):
            self.name = name

    class _ProtoChannel:
        def __init__(self, index, name, role):
            self.index = index
            self.settings = TestRealDeviceShapes._ProtoSettings(name)
            self.role = role  # 0=DISABLED, 1=PRIMARY, 2=SECONDARY

    class _LoRa:
        def __init__(self, region):
            self.region = region

    class _LocalConfig:
        def __init__(self, region):
            self.lora = TestRealDeviceShapes._LoRa(region)

    class _LocalNode:
        def __init__(self, region, channels):
            self.localConfig = TestRealDeviceShapes._LocalConfig(region)
            self.channels = channels

    class RealShapedInterface:
        """Mimics a real device: config/channels only under localNode."""

        def __init__(self, region=1):
            self.localConfig = None   # real devices leave this None
            self.channels = None      # ...and this
            self.localNode = TestRealDeviceShapes._LocalNode(
                region,
                [
                    TestRealDeviceShapes._ProtoChannel(0, "PrivateChan", 1),
                    TestRealDeviceShapes._ProtoChannel(1, "", 2),
                    TestRealDeviceShapes._ProtoChannel(2, "", 0),
                ],
            )

    def test_region_read_from_local_node(self):
        iface = self.RealShapedInterface(region=1)
        # The point of this test is that the region is found at all: a None
        # means the unset-check silently no-ops and the adapter would
        # transmit without verifying the region.
        #
        # The exact rendering depends on the environment.  With the
        # meshtastic package installed, the protobuf enum resolves to its
        # name ("US"); without it, read_region falls back to the raw value
        # ("1").  Both are correct, and region_is_unset() understands both,
        # so accept either rather than requiring meshtastic in CI.
        region = tp.read_region(iface)
        assert region in ("US", "1"), region
        assert tp.region_is_unset(region) is False

    def test_unset_region_detected_on_real_shape(self):
        # Region 0 is UNSET.  This must be detected whether it renders as
        # the protobuf name "UNSET" or the raw "0" — transmitting on an
        # unset region is the regulatory hazard this check exists to stop.
        iface = self.RealShapedInterface(region=0)
        region = tp.read_region(iface)
        assert region in ("UNSET", "0"), region
        assert tp.region_is_unset(region) is True

    def test_channel_name_from_local_node(self):
        iface = self.RealShapedInterface()
        assert tp.channel_name_at(iface, 0) == "PrivateChan"

    def test_unnamed_primary_reports_as_longfast(self):
        iface = self.RealShapedInterface()
        iface.localNode.channels[0].settings.name = ""
        assert tp.channel_name_at(iface, 0) == "LongFast"

    def test_channel_index_lookup_is_case_insensitive(self):
        iface = self.RealShapedInterface()
        assert tp.channel_index_of(iface, "PRIVATECHAN") == 0

    def test_disabled_channels_have_no_name(self):
        iface = self.RealShapedInterface()
        assert tp.channel_name_at(iface, 2) is None

    async def test_adapter_resolves_real_channel_names(self, monkeypatch):
        iface = self.RealShapedInterface()
        iface.myInfo = type("MI", (), {"my_node_num": 0x11223344})()
        iface.nodes = {}
        iface.sent = []
        iface.close = lambda: None

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        a = make_adapter()
        assert await a.connect() is True
        assert a._resolve_channel_name(0) == "PrivateChan"
        assert a._channel_index_for_name("PrivateChan") == 0
        await a.disconnect()


class TestUnknownChannelGuard:
    """A channel that isn't on the radio must fail, not silently retarget.

    Found on real hardware: a radio whose only channel was a custom
    private one accepted a send to 'LongFast' and reported success, because
    the index lookup returned None and fell back to 0 — transmitting on the
    primary channel instead.
    """

    async def test_unknown_channel_refused(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()

        result = await a.send("NoSuchChannel", "hello")

        assert result.success is False
        assert "not configured on this radio" in result.error
        # The critical part: it must not have gone out on channel 0.
        assert connected.sent == []
        await a.disconnect()

    async def test_error_lists_available_channels(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        result = await a.send("Nope", "hello")
        assert "LongFast" in result.error  # the fake's real channels
        await a.disconnect()

    async def test_known_channel_still_sends(self, connected):
        a = make_adapter(chunk_delay_seconds=0)
        await a.connect()
        result = await a.send("Emergency", "evac")
        assert result.success is True
        assert connected.sent[0].channel_index == 1
        await a.disconnect()

    async def test_available_channel_names(self, connected):
        a = make_adapter()
        await a.connect()
        assert a.available_channel_names() == ["LongFast", "Emergency"]
        await a.disconnect()


class TestAllowFromMustBeAList:
    """A string allow_from fails silently in two opposite directions.

    Adopted from the independent Kimi build, which type-checked allow_from
    where this one did not.  Python iterates a string into characters, so
    ``allow_from: "*"`` becomes ``["*"]`` and OPENS the radio to the whole
    mesh, while ``allow_from: "!aabbccdd"`` becomes per-character garbage
    that matches nothing.  Both are plausible YAML slips.
    """

    def test_string_wildcard_does_not_open_the_radio(self):
        # The dangerous direction: this must NOT satisfy the open-policy guard.
        assert validate_config(FakeConfig(extra={
            "transport": "serial", "dm_policy": "open", "allow_from": "*",
        })) is False

    def test_string_node_id_rejected(self):
        assert validate_config(FakeConfig(extra={
            "transport": "serial", "dm_policy": "allowlist", "allow_from": "!aabbccdd",
        })) is False

    def test_proper_list_still_accepted(self):
        assert validate_config(FakeConfig(extra={
            "transport": "serial", "dm_policy": "open", "allow_from": ["*"],
        })) is True

    def test_tuple_accepted(self):
        assert validate_config(FakeConfig(extra={
            "transport": "serial", "dm_policy": "allowlist", "allow_from": ("!aabbccdd",),
        })) is True

    def test_absent_allow_from_is_fine(self):
        assert validate_config(FakeConfig(extra={"transport": "serial"})) is True

    def test_adapter_ignores_a_string_rather_than_splitting_it(self):
        # Constructed directly (no validate_config), the adapter must not
        # end up with a per-character allowlist.
        a = make_adapter(allow_from="!aabbccdd")
        assert a.allow_from == []

    def test_empty_transport_rejected(self):
        assert validate_config(FakeConfig(extra={"transport": ""})) is False


class TestAutoInstallIsOptIn:
    """ensure_deps() exists but no registry hook calls it.

    PlatformEntry has no ensure_deps_fn field, so passing one raises at
    registration.  It runs from connect() only when the operator opts in.
    """

    async def test_no_install_attempted_by_default(self, connected, monkeypatch):
        called = []
        monkeypatch.setattr(adapter_mod, "ensure_deps", lambda: called.append(True))
        monkeypatch.setattr(adapter_mod, "check_requirements", lambda: False)
        a = make_adapter()
        await a.connect()
        assert called == [], "must not install anything without explicit opt-in"
        await a.disconnect()

    async def test_install_attempted_when_opted_in(self, connected, monkeypatch):
        called = []
        monkeypatch.setenv("MESHTASTIC_AUTO_INSTALL", "true")
        monkeypatch.setattr(adapter_mod, "ensure_deps", lambda: called.append(True))
        monkeypatch.setattr(adapter_mod, "check_requirements", lambda: False)
        a = make_adapter()
        await a.connect()
        assert called == [True]
        await a.disconnect()


class TestRegistrationFieldsAreReal:
    """Every kwarg passed to register_platform must exist on PlatformEntry.

    The independent build failed to load because it passed
    apply_yaml_config_fn (and ensure_deps_fn), neither of which is a real
    field.  This test pins the whole kwarg set against the live dataclass so
    the same class of defect cannot ship here.
    """

    def test_all_kwargs_are_valid_platform_entry_fields(self):
        import dataclasses

        try:
            from gateway.platform_registry import PlatformEntry
        except Exception:
            pytest.skip("Hermes not importable")

        valid = {f.name for f in dataclasses.fields(PlatformEntry)}
        recorded = {}

        class Ctx:
            def register_platform(self, **kwargs):
                recorded.update(kwargs)

            def register_tool(self, **kwargs):
                pass

        adapter_mod.register(Ctx())
        unknown = set(recorded) - valid
        assert not unknown, f"register_platform passes non-existent fields: {unknown}"


class TestMentionNames:
    """Name resolution feeding the mention gate."""

    def test_device_names_used_when_unconfigured(self, connected):
        a = make_adapter()
        asyncio.run(a.connect())
        try:
            primary, short, extra = a._mention_names()
            assert primary == "HermesBot"
            assert short == "HB"
            assert extra == ()
        finally:
            asyncio.run(a.disconnect())

    def test_configured_name_wins_but_device_name_still_triggers(self, monkeypatch):
        iface = FakeMeshInterface()

        async def _open(**kwargs):
            return iface

        monkeypatch.setattr(tp, "open_interface", _open)
        a = make_adapter(node_name="Hermes")
        asyncio.run(a.connect())
        try:
            primary, short, extra = a._mention_names()
            assert primary == "Hermes"
            assert short == "HB"
            assert "HermesBot" in extra
        finally:
            asyncio.run(a.disconnect())

    def test_no_interface_yields_no_names(self):
        a = make_adapter()
        assert a._mention_names() == (None, None, ())
