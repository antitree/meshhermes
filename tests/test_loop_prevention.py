"""Loop prevention: the controls that stop two bots talking forever.

The scenario these exist for: with ``require_mention`` false a Hermes bot
answers every message on a channel.  Put two such bots on one channel and
each one's reply wakes the other, forever, burning airtime that is a
shared and legally regulated resource.

The test that matters most is :class:`TestTwoBotsOnOneChannel` — two real
adapters on one shared fake-mesh channel, one seed message, run out.  The
unit tests around it exist to show that each of the three controls
independently bounds that exchange, so no single misconfiguration reopens
the hole.

Time is injected rather than slept: ``sendpolicy`` reads
``time.monotonic``, so the tests advance a fake clock instead of waiting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

import sendpolicy as sp
import transport as tp
from adapter import MeshtasticAdapter
from normalize import node_num_to_hex
from fake_mesh import (
    MY_NODE_NUM,
    PEER_NODE_NUM,
    FakeMeshInterface,
    SharedAir,
    TransmissionBudgetExceeded,
)


@dataclass
class FakeConfig:
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replace ``sendpolicy``'s clock so cooldown/TTL are instant to test."""
    c = FakeClock()
    # Patch sendpolicy's own indirection, not time.monotonic itself —
    # replacing the real clock globally would break asyncio's scheduling.
    monkeypatch.setattr(sp, "_monotonic", c)
    return c


# ---------------------------------------------------------------------------
# Control 1: conversation cooldown
# ---------------------------------------------------------------------------


class TestConversationCooldown:
    def test_on_by_default_at_60_seconds(self):
        assert sp.conversation_cooldown_seconds() == 60.0

    def test_first_reply_allowed_then_suppressed(self, clock):
        assert sp.cooldown_ok("LongFast") is True
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast") is False

    def test_expires_on_the_boundary(self, clock):
        sp.note_channel_reply("LongFast")
        clock.advance(59.9)
        assert sp.cooldown_ok("LongFast") is False
        clock.advance(0.2)  # now past 60s
        assert sp.cooldown_ok("LongFast") is True

    def test_scoped_per_channel(self, clock):
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast") is False
        assert sp.cooldown_ok("Emergency") is True

    def test_channel_name_is_case_insensitive(self, clock):
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("longfast") is False

    def test_mentions_are_throttled_too_by_default(self, clock):
        # The strict reading, chosen deliberately: two bots addressing each
        # other by name must not ping-pong straight through the cooldown.
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast", was_mentioned=True) is False

    def test_mention_exemption_flag_relaxes_it(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS", "true")
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast", was_mentioned=True) is True
        # An unmentioned message is still throttled.
        assert sp.cooldown_ok("LongFast", was_mentioned=False) is False

    @pytest.mark.parametrize("value", ["0", "-1", "-30.5"])
    def test_zero_or_negative_disables(self, clock, monkeypatch, value):
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", value)
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast") is True

    def test_env_override_duration(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "5")
        sp.note_channel_reply("LongFast")
        assert sp.cooldown_ok("LongFast") is False
        clock.advance(6)
        assert sp.cooldown_ok("LongFast") is True

    def test_invalid_override_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "banana")
        assert sp.conversation_cooldown_seconds() == 60.0

    def test_expired_entry_is_evicted(self, clock):
        sp.note_channel_reply("LongFast")
        clock.advance(61)
        assert sp.cooldown_ok("LongFast") is True
        # The map must not keep a row per channel ever spoken on.
        assert "longfast" not in sp._last_reply_at

    def test_empty_channel_is_never_throttled(self, clock):
        # DMs carry no channel; they must not all share one cooldown slot.
        sp.note_channel_reply("")
        assert sp.cooldown_ok("") is True
        assert sp.cooldown_ok(None) is True


# ---------------------------------------------------------------------------
# Control 2: loop-signature detection
# ---------------------------------------------------------------------------


class TestLoopSignatureDetection:
    def test_off_by_default(self, clock):
        assert sp.loop_detection_enabled() is False
        assert sp.loop_signature_seen("LongFast", "hello") is False
        assert sp.loop_signature_seen("LongFast", "hello") is False
        # Nothing is even recorded while disabled.
        assert sp.loop_signature_count() == 0

    def test_repeat_suppressed_when_enabled(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "hello") is False
        assert sp.loop_signature_seen("LongFast", "hello") is True

    def test_different_text_not_suppressed(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "hello") is False
        assert sp.loop_signature_seen("LongFast", "goodbye") is False

    def test_scoped_per_channel(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "hello") is False
        # Same text, different channel: a separate conversation.
        assert sp.loop_signature_seen("Emergency", "hello") is False

    def test_sender_is_deliberately_not_part_of_the_signature(self, clock, monkeypatch):
        # This is the whole point: in the loop being broken, bot A and bot B
        # are different senders saying the same thing.  The signature has no
        # sender field, so there is nothing to vary.
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature("LongFast", "hi") == sp.loop_signature("LongFast", "hi")

    @pytest.mark.parametrize(
        "variant",
        ["HELLO there", "hello   there", "  hello there  ", "hello\nthere", "Hello There"],
    )
    def test_trivial_variation_does_not_defeat_it(self, clock, monkeypatch, variant):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "hello there") is False
        assert sp.loop_signature_seen("LongFast", variant) is True

    def test_punctuation_still_distinguishes(self, clock, monkeypatch):
        # Normalization stops at case and whitespace; going further would
        # start conflating genuinely different messages.
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "ready") is False
        assert sp.loop_signature_seen("LongFast", "ready?") is False

    def test_empty_text_is_ignored(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        assert sp.loop_signature_seen("LongFast", "   ") is False
        assert sp.loop_signature_count() == 0

    def test_repeat_does_not_arm_the_cooldown(self, clock, monkeypatch):
        # The two controls stay independent: a false positive here must not
        # silence the channel for a full cooldown window.
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        sp.loop_signature_seen("LongFast", "hello")
        assert sp.loop_signature_seen("LongFast", "hello") is True
        assert sp.cooldown_ok("LongFast") is True


class TestLoopSignatureMemoryBound:
    """A long-running gateway must not leak a dict entry per message."""

    def test_entries_expire_after_the_ttl(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS", "30")
        assert sp.loop_signature_seen("LongFast", "hello") is False
        clock.advance(31)
        # Aged out, so it is treated as new again — and the entry is gone.
        assert sp.loop_signature_seen("LongFast", "hello") is False

    def test_ttl_eviction_bounds_the_cache(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS", "10")
        for i in range(50):
            sp.loop_signature_seen("LongFast", f"message {i}")
            clock.advance(1)
        # Only entries inside the 10s window survive.
        assert sp.loop_signature_count() <= 11

    def test_hard_cap_bounds_the_cache(self, clock, monkeypatch):
        # Even inside one TTL window a busy channel must not fill memory.
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES", "10")
        for i in range(500):
            sp.loop_signature_seen("LongFast", f"message {i}")
        assert sp.loop_signature_count() <= 10

    def test_default_cap_bounds_the_cache(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        for i in range(2000):
            sp.loop_signature_seen("LongFast", f"message {i}")
        assert sp.loop_signature_count() <= sp.DEFAULT_LOOP_SIGNATURE_MAX_ENTRIES

    def test_eviction_is_oldest_first(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES", "3")
        for text in ("one", "two", "three"):
            sp.loop_signature_seen("LongFast", text)
        sp.loop_signature_seen("LongFast", "four")  # evicts "one"
        assert sp.loop_signature_seen("LongFast", "one") is False
        assert sp.loop_signature_seen("LongFast", "four") is True

    def test_a_repeating_message_stays_suppressed(self, clock, monkeypatch):
        # Refreshed on each hit, so a steady loop cannot age its own
        # signature out mid-flight and re-open itself.
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS", "10")
        assert sp.loop_signature_seen("LongFast", "loop") is False
        for _ in range(20):
            clock.advance(5)
            assert sp.loop_signature_seen("LongFast", "loop") is True

    @pytest.mark.parametrize("bad", ["nonsense", "0", "-5"])
    def test_invalid_bounds_fall_back_to_defaults(self, monkeypatch, bad):
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES", bad)
        monkeypatch.setenv("MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS", bad)
        assert sp.loop_signature_max_entries() == sp.DEFAULT_LOOP_SIGNATURE_MAX_ENTRIES
        assert sp.loop_signature_ttl_seconds() == sp.DEFAULT_LOOP_SIGNATURE_TTL_SECONDS

    def test_invalid_enable_flag_falls_back_to_off(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "maybe")
        assert sp.loop_detection_enabled() is False


# ---------------------------------------------------------------------------
# Control 3: the configurable hard rate limit
# ---------------------------------------------------------------------------


class TestConfigurableRateLimit:
    def test_defaults_unchanged(self):
        assert sp.rate_limit_max_sends() == 5
        assert sp.rate_limit_window_seconds() == 60.0

    def test_module_constants_track_the_override(self, monkeypatch):
        # mesh_tools aliases these and tests read them; a value snapshotted
        # at import would silently quote a limit the gate is not applying.
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "2")
        assert sp.RATE_LIMIT_MAX_SENDS == 2
        assert int(sp.RATE_LIMIT_MAX_SENDS) == 2
        assert f"{sp.RATE_LIMIT_MAX_SENDS}" == "2"

    def test_mesh_tools_alias_is_not_stale(self, monkeypatch):
        import mesh_tools as tools_mod

        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "3")
        assert tools_mod._RATE_LIMIT_MAX_SENDS == 3
        assert tools_mod._rate_limit_max_sends() == 3

    def test_override_is_enforced(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "2")
        assert sp.rate_limit_ok() is True
        assert sp.rate_limit_ok() is True
        assert sp.rate_limit_ok() is False

    def test_window_override_is_enforced(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "1")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS", "10")
        assert sp.rate_limit_ok() is True
        assert sp.rate_limit_ok() is False
        clock.advance(11)
        assert sp.rate_limit_ok() is True

    @pytest.mark.parametrize("bad", ["nonsense", "0", "-1", "", "   "])
    def test_invalid_overrides_fall_back_to_defaults(self, monkeypatch, bad):
        # Never crash, and never read a typo as "no limit".
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", bad)
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS", bad)
        assert sp.rate_limit_max_sends() == 5
        assert sp.rate_limit_window_seconds() == 60.0

    def test_invalid_override_still_enforces_the_default(self, clock, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "unlimited")
        for _ in range(5):
            assert sp.rate_limit_ok() is True
        assert sp.rate_limit_ok() is False


# ---------------------------------------------------------------------------
# The success criterion: two bots, one channel, require_mention false
# ---------------------------------------------------------------------------


class Bot:
    """One Hermes adapter on the shared air, wired to an echoing agent.

    The "agent" replies to every message it is handed, which is exactly
    what a real agent does when ``require_mention`` is false.  Nothing here
    tries to be clever about not looping — the whole point is that the
    policy under test is the only thing preventing one.
    """

    def __init__(self, adapter: MeshtasticAdapter, iface: FakeMeshInterface,
                 name: str) -> None:
        self.adapter = adapter
        self.iface = iface
        self.name = name
        self.replies: List[str] = []

    @property
    def transmissions(self) -> int:
        return len(self.iface.sent)


async def build_bot(monkeypatch, air: SharedAir, *, node_num: int, name: str,
                    reply_text=None) -> Bot:
    iface = FakeMeshInterface(my_node_num=node_num, air=air)

    async def _open(**kwargs):
        return iface

    monkeypatch.setattr(tp, "open_interface", _open)

    adapter = MeshtasticAdapter(FakeConfig(extra={
        "transport": "serial",
        "serial_port": "/dev/ttyFAKE",
        "node_name": name,
        "chunk_delay_seconds": 0,
        # The configuration this whole feature exists for.
        "channels": {"LongFast": {"require_mention": False}},
        "group_policy": "open",
    }))

    bot = Bot(adapter, iface, name)

    async def agent(event):
        # A plain echoing agent: it answers whatever it is given.  Two of
        # these on one channel is the runaway.
        text = reply_text(event.text) if reply_text else f"{name} heard: {event.text}"
        bot.replies.append(text)
        await adapter.send(event.source.chat_id, text)

    adapter.set_message_handler(agent)
    assert await adapter.connect() is True
    return bot


@pytest.fixture
async def two_bots(monkeypatch):
    """Two connected adapters sharing one fake RF channel."""
    built: List[Bot] = []

    async def _build(*, max_transmissions: int = 50, reply_text=None):
        air = SharedAir(max_transmissions=max_transmissions)
        a = await build_bot(monkeypatch, air, node_num=MY_NODE_NUM,
                            name="BotA", reply_text=reply_text)
        b = await build_bot(monkeypatch, air, node_num=0x22334455,
                            name="BotB", reply_text=reply_text)
        built.extend([a, b])
        return air, a, b

    yield _build

    for bot in built:
        await bot.adapter.disconnect()


async def run_out(air: SharedAir, *, quiet_for: float = 0.3,
                  timeout: float = 3.0) -> None:
    """Let the exchange run until the air goes quiet, or time out.

    "Terminates" means: no new transmission for *quiet_for* seconds.  A
    runaway never goes quiet, so it either trips the air's transmission
    budget or hits *timeout* — both of which fail the calling test.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_count = -1
    quiet_since = loop.time()
    while loop.time() < deadline:
        count = air.transmission_count
        if count != last_count:
            last_count = count
            quiet_since = loop.time()
        elif loop.time() - quiet_since >= quiet_for:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"the exchange never went quiet: {air.transmission_count} transmissions "
        f"in {timeout}s — this is the runaway the feature exists to prevent"
    )


class TestTwoBotsOnOneChannel:
    """The success criterion, stated by the user:

    "the system can survive 2 instances of itself inside of a channel where
    require_mention is false, and it won't create an infinite loop."
    """

    async def test_defaults_bound_the_exchange(self, two_bots):
        air, a, b = await two_bots()

        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        await run_out(air)

        total = air.transmission_count
        # Each bot answers the human's seed once, then the conversation
        # cooldown stops each of them replying to the other.
        assert total <= 4, f"expected a bounded exchange, got {total} transmissions"
        assert total >= 1, "the bots should still have answered the human at least once"
        assert a.transmissions <= 2
        assert b.transmissions <= 2

    async def test_a_second_seed_inside_the_window_is_also_bounded(self, two_bots):
        air, a, b = await two_bots()

        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        await run_out(air)
        first = air.transmission_count

        air.seed("anyone there?", from_num=PEER_NODE_NUM)
        await run_out(air)

        # Still inside the 60s cooldown, so the second seed adds nothing.
        assert air.transmission_count == first

    async def test_runaway_is_reproducible_without_the_controls(self, two_bots, monkeypatch):
        """The harness must be able to reproduce the bug it guards against.

        Without this, every assertion above could be passing because the
        fake mesh never carried traffic between the two bots at all.
        """
        air, a, b = await two_bots(max_transmissions=25)

        # Disable all three controls, as a fully misconfigured operator would.
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "100000")

        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        # The exchange runs away until the air's budget stops it.  The
        # adapter catches the resulting send failure (it treats it as a
        # radio error, which is right), so assert on the traffic rather
        # than on the exception escaping.
        try:
            await run_out(air, timeout=2.0)
        except AssertionError:
            pass  # never went quiet — also a runaway

        assert air.transmission_count > 10, (
            "the two-bot harness must be able to reproduce the runaway it "
            f"guards against, but only saw {air.transmission_count} "
            "transmissions — the bots are not hearing each other, so the "
            "passing tests above would be vacuous"
        )


class TestEachLayerIndependentlyBounds:
    """Each control must stop the runaway on its own.

    They are deliberately not tested together here: if only the combination
    worked, a single misconfigured variable would reopen the hole, and an
    operator who turns one control off has no way to know they have
    disarmed the whole defence.
    """

    async def test_cooldown_alone(self, two_bots, monkeypatch):
        # Cooldown on (default); loop detection off (default); rate limit
        # effectively out of the way.
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "100000")
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "false")

        air, a, b = await two_bots()
        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        await run_out(air)

        assert air.transmission_count <= 4, (
            f"the cooldown alone must bound the exchange, saw "
            f"{air.transmission_count} transmissions"
        )

    async def test_loop_detection_alone(self, two_bots, monkeypatch):
        # Cooldown off, rate limit out of the way: only the signature cache
        # is left.  The bots echo a *fixed* string so the repeat is exact —
        # this control keys on content, and an echo that appends the input
        # produces new content every hop by construction.
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "100000")
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "true")

        air, a, b = await two_bots(reply_text=lambda _text: "acknowledged")
        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        await run_out(air)

        assert air.transmission_count <= 4, (
            f"loop detection alone must bound the exchange, saw "
            f"{air.transmission_count} transmissions"
        )

    async def test_rate_limit_alone(self, two_bots, monkeypatch):
        # Both reply-decision controls disabled.  The hard backstop is the
        # only thing left, and it must still cap the airtime.
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "false")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "5")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS", "60")

        air, a, b = await two_bots(max_transmissions=40)
        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        try:
            await run_out(air)
        except AssertionError:
            pass

        # The bucket is global across both adapters in this process, so the
        # whole exchange is capped at the limit itself.
        assert air.transmission_count <= 5, (
            f"the rate limit alone must cap airtime, saw "
            f"{air.transmission_count} transmissions"
        )

    async def test_all_three_disabled_is_the_runaway(self, two_bots, monkeypatch):
        # The negative control for the three tests above: with everything
        # off the exchange does not terminate, which is what proves each of
        # them was doing the work attributed to it.
        monkeypatch.setenv("MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS", "0")
        monkeypatch.setenv("MESHTASTIC_LOOP_DETECTION", "false")
        monkeypatch.setenv("MESHTASTIC_RATE_LIMIT_MAX_SENDS", "100000")

        air, a, b = await two_bots(max_transmissions=30)
        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        try:
            await run_out(air, timeout=2.0)
        except AssertionError:
            pass

        assert air.transmission_count > 10


class TestDirectMessagesAreNotThrottled:
    """The cooldown is per *channel*; a DM is not on one.

    The scope chosen with the user was "per channel, applying to all replies
    including mentions and DMs to that channel".  A direct message has no
    channel to cool down — it is addressed to the bot alone, cannot be
    overheard by a second bot, and so cannot start the loop this feature
    exists to break.  Throttling it would only make the bot unresponsive to
    its operator.  The hard rate limit still caps DM airtime.
    """

    async def test_dm_still_answered_while_a_channel_is_cooling(self, two_bots):
        air, a, b = await two_bots()

        air.seed("hello everyone", from_num=PEER_NODE_NUM)
        await run_out(air)
        assert not sp.cooldown_ok("LongFast"), "the channel should be cooling"

        before = a.transmissions
        # A DM arrives on the same radio while LongFast is in cooldown.
        a.iface.inject_text("are you there?", from_id=node_num_to_hex(PEER_NODE_NUM),
                            to=a.iface.my_node_id)
        await asyncio.sleep(0.3)

        assert a.transmissions > before, (
            "a direct message must still be answered while a channel is in "
            "cooldown — the cooldown is per channel, and a DM is not on one"
        )
