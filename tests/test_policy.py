"""Tests for channel matching and mention gating."""

import pytest

from policy import (
    resolve_channel_match,
    resolve_mention_gate,
    resolve_require_mention,
    strip_mention,
    mention_trigger_names,
)


class TestChannelMatch:
    def test_exact_match(self):
        channels = {"LongFast": {"require_mention": False}}
        match = resolve_channel_match(channels, "LongFast")
        assert match.config == {"require_mention": False}
        assert match.matched_name == "LongFast"

    def test_case_insensitive_match(self):
        channels = {"LongFast": {"require_mention": False}}
        match = resolve_channel_match(channels, "longfast")
        assert match.config == {"require_mention": False}
        assert match.matched_name == "LongFast"

    def test_exact_wins_over_case_insensitive(self):
        channels = {"chan": {"id": "exact"}, "CHAN": {"id": "other"}}
        assert resolve_channel_match(channels, "chan").config == {"id": "exact"}

    def test_wildcard_exposed_separately(self):
        channels = {"*": {"require_mention": True}, "Emergency": {"require_mention": False}}
        match = resolve_channel_match(channels, "Emergency")
        assert match.config == {"require_mention": False}
        assert match.wildcard == {"require_mention": True}

    def test_unmatched_channel_gets_wildcard_only(self):
        channels = {"*": {"require_mention": True}}
        match = resolve_channel_match(channels, "Unknown")
        assert match.config is None
        assert match.wildcard == {"require_mention": True}

    def test_empty_channels(self):
        match = resolve_channel_match({}, "LongFast")
        assert match.config is None and match.wildcard is None
        match = resolve_channel_match(None, "LongFast")
        assert match.config is None and match.wildcard is None

    def test_no_target(self):
        match = resolve_channel_match({"*": {"a": 1}}, None)
        assert match.config is None
        assert match.wildcard == {"a": 1}


class TestRequireMention:
    def test_defaults_to_true(self):
        # Safe default on a shared radio channel.
        assert resolve_require_mention(None, None) is True

    def test_channel_value_wins(self):
        assert resolve_require_mention({"require_mention": False}, {"require_mention": True}) is False

    def test_wildcard_used_when_channel_silent(self):
        assert resolve_require_mention({}, {"require_mention": False}) is False

    def test_channel_absent_key_falls_through(self):
        assert resolve_require_mention({"other": 1}, {"require_mention": False}) is False

    @pytest.mark.parametrize("truthy", [True, "true", "yes", "on", "1", 1])
    def test_truthy_spellings(self, truthy):
        assert resolve_require_mention({"require_mention": truthy}, None) is True

    @pytest.mark.parametrize("falsy", [False, "false", "no", "off", "0", 0])
    def test_falsy_spellings(self, falsy):
        assert resolve_require_mention({"require_mention": falsy}, None) is False

    def test_unparseable_falls_through_to_default(self):
        assert resolve_require_mention({"require_mention": "maybe"}, None) is True


class TestStripMention:
    def test_at_prefix(self):
        assert strip_mention("@hermes hello there", "hermes") == ("hello there", True)

    def test_colon_prefix(self):
        assert strip_mention("hermes: hello", "hermes") == ("hello", True)

    def test_comma_prefix(self):
        assert strip_mention("hermes, hello", "hermes") == ("hello", True)

    def test_case_insensitive(self):
        assert strip_mention("@HERMES hello", "hermes") == ("hello", True)

    def test_mid_sentence_mention_does_not_count(self):
        # BREAKING vs. the old IRC-style matcher: Meshtastic addressing is
        # leading-position only.  "tell @hermes I said hi" is conversation
        # about the bot, not an instruction to it.
        text, mentioned = strip_mention("ping @hermes please", "hermes")
        assert mentioned is False
        assert text == "ping @hermes please"

    def test_no_mention(self):
        assert strip_mention("hello everyone", "hermes") == ("hello everyone", False)

    def test_substring_is_not_a_mention(self):
        # "hermesbot" must not match a bot named "hermes".
        _, mentioned = strip_mention("@hermesbot hi", "hermes")
        assert mentioned is False

    def test_no_node_name(self):
        assert strip_mention("hello", None) == ("hello", False)

    def test_name_with_regex_characters(self):
        assert strip_mention("@node.1 hi", "node.1") == ("hi", True)

    def test_regex_metacharacters_are_literal(self):
        # "." must not act as a wildcard: "nodeX1" is a different node.
        assert strip_mention("nodeX1 hi", "node.1") == ("nodeX1 hi", False)
        assert strip_mention("a*b(c) hi", "a*b(c)") == ("hi", True)


class TestBareLongNameMatching:
    """The behaviour table agreed with the user."""

    NAME = "Long Name of Node"

    def test_at_prefix(self):
        assert strip_mention("@Long Name of Node tell me X", self.NAME) == ("tell me X", True)

    def test_bare_name_followed_by_space(self):
        assert strip_mention("Long Name of Node tell me X", self.NAME) == ("tell me X", True)

    def test_colon(self):
        assert strip_mention("Long Name of Node: tell me X", self.NAME) == ("tell me X", True)

    def test_comma(self):
        assert strip_mention("Long Name of Node, tell me X", self.NAME) == ("tell me X", True)

    def test_case_insensitive(self):
        assert strip_mention("long name of node tell me X", self.NAME) == ("tell me X", True)

    def test_mid_sentence_does_not_match(self):
        assert strip_mention("hey Long Name of Node hi", self.NAME) == (
            "hey Long Name of Node hi",
            False,
        )

    def test_bare_name_alone_matches_with_empty_text(self):
        # Documented: the gate ALLOWS it (was_mentioned is True) and hands
        # back an empty body; the adapter drops empty bodies before
        # dispatch, so this does not crash and does not wake the agent.
        assert strip_mention("Long Name of Node", self.NAME) == ("", True)

    def test_bare_name_alone_gate_allows_with_empty_text(self):
        gate = resolve_mention_gate("Long Name of Node", self.NAME, require_mention=True)
        assert gate.allowed is True
        assert gate.was_mentioned is True
        assert gate.text == ""

    def test_leading_whitespace_tolerated(self):
        assert strip_mention("   Long Name of Node  hi  ", self.NAME) == ("hi", True)

    def test_longer_name_is_not_a_match(self):
        assert strip_mention("Long Name of Nodes hi", self.NAME) == (
            "Long Name of Nodes hi",
            False,
        )

    def test_unicode_and_emoji_names(self):
        assert strip_mention("🐝 Hive tell me X", "🐝 Hive") == ("tell me X", True)
        assert strip_mention("Ünïcødé hi", "Ünïcødé") == ("hi", True)

    def test_empty_and_whitespace_names(self):
        assert strip_mention("anything", "") == ("anything", False)
        assert strip_mention("anything", "   ") == ("anything", False)
        assert strip_mention("anything", None) == ("anything", False)

    def test_empty_text(self):
        assert strip_mention("", self.NAME) == ("", False)


class TestShortNameMatching:
    LONG = "Long Name of Node"

    def test_short_name_triggers(self):
        assert strip_mention("LNN tell me X", self.LONG, "LNN") == ("tell me X", True)

    def test_short_name_with_at_and_punctuation(self):
        assert strip_mention("@LNN: hi", self.LONG, "LNN") == ("hi", True)
        assert strip_mention("LNN, hi", self.LONG, "LNN") == ("hi", True)

    def test_short_name_mid_sentence_does_not_trigger(self):
        assert strip_mention("ask LNN about it", self.LONG, "LNN") == (
            "ask LNN about it",
            False,
        )

    def test_short_name_requires_word_boundary(self):
        assert strip_mention("LNNX hi", self.LONG, "LNN") == ("LNNX hi", False)

    def test_too_short_short_name_ignored(self):
        assert strip_mention("AB hi", self.LONG, "AB") == ("AB hi", False)
        assert strip_mention("A hi", self.LONG, "A") == ("A hi", False)

    def test_stopword_short_name_ignored(self):
        assert strip_mention("test the radio", self.LONG, "test") == (
            "test the radio",
            False,
        )
        assert strip_mention("hey there", self.LONG, "hey") == ("hey there", False)

    def test_empty_short_name_ignored(self):
        assert strip_mention("random chatter", self.LONG, "   ") == (
            "random chatter",
            False,
        )
        assert strip_mention("random chatter", self.LONG, None) == (
            "random chatter",
            False,
        )

    def test_long_name_preferred_when_it_starts_with_short_name(self):
        # Longest-first ordering strips the whole long name, not its prefix.
        assert strip_mention("LNN Base hello", "LNN Base", "LNN") == ("hello", True)

    def test_extra_names_also_trigger(self):
        # Configured node_name is primary; the radio's real long name stays
        # a trigger via extra_names.
        assert strip_mention("Radio Actual hi", "Hermes", None, ("Radio Actual",)) == (
            "hi",
            True,
        )

    def test_trigger_name_list_dedupes_and_sorts(self):
        names = mention_trigger_names("Hermes", "Hermes", ("hermes",))
        assert names == ("Hermes",)
        assert mention_trigger_names("Hermes", "HRM") == ("Hermes", "HRM")


class TestMentionGate:
    def test_dm_always_allowed(self):
        gate = resolve_mention_gate("hello", "hermes", require_mention=True, is_direct=True)
        assert gate.allowed is True

    def test_dm_still_strips_mention(self):
        gate = resolve_mention_gate("@hermes hello", "hermes", require_mention=True, is_direct=True)
        assert gate.text == "hello"

    def test_channel_unaddressed_blocked(self):
        gate = resolve_mention_gate("just chatting", "hermes", require_mention=True)
        assert gate.allowed is False
        assert gate.reason == "not_mentioned"

    def test_channel_addressed_allowed_and_stripped(self):
        gate = resolve_mention_gate("@hermes status?", "hermes", require_mention=True)
        assert gate.allowed is True
        assert gate.text == "status?"
        assert gate.was_mentioned is True

    def test_require_mention_off_allows_everything(self):
        gate = resolve_mention_gate("anything", "hermes", require_mention=False)
        assert gate.allowed is True

    def test_authorized_command_bypass(self):
        gate = resolve_mention_gate(
            "/help", "hermes", require_mention=True, is_authorized_command=True
        )
        assert gate.allowed is True
        assert gate.reason == "authorized_command"

    def test_unauthorized_command_still_gated(self):
        gate = resolve_mention_gate(
            "/help", "hermes", require_mention=True, is_authorized_command=False
        )
        assert gate.allowed is False

    def test_leading_slash_does_not_bypass_gate(self):
        # Regression: CHANGELOG "A leading `/` bypassed mention gating."
        # Attacker-controlled text must never look like authorization.
        for text in ("/help", "/ status", "//nodes", "/hermes hi"):
            gate = resolve_mention_gate(text, "hermes", require_mention=True)
            assert gate.allowed is False, text
            assert gate.reason == "not_mentioned"

    def test_slash_after_mention_is_fine(self):
        gate = resolve_mention_gate("hermes /nodes", "hermes", require_mention=True)
        assert gate.allowed is True
        assert gate.text == "/nodes"

    def test_require_mention_off_keeps_full_text_when_unaddressed(self):
        gate = resolve_mention_gate("just chatting", "hermes", require_mention=False)
        assert gate.allowed is True
        assert gate.text == "just chatting"
        assert gate.was_mentioned is False

    def test_dm_bare_name_is_stripped(self):
        gate = resolve_mention_gate(
            "hermes what time is it", "hermes", require_mention=True, is_direct=True
        )
        assert gate.allowed is True
        assert gate.reason == "dm"
        assert gate.text == "what time is it"

    def test_gate_accepts_short_name(self):
        gate = resolve_mention_gate(
            "LNN status?", "Long Name of Node", require_mention=True, short_name="LNN"
        )
        assert gate.allowed is True
        assert gate.text == "status?"

    def test_gate_short_name_collision_still_blocked(self):
        gate = resolve_mention_gate(
            "ok everyone", "Long Name of Node", require_mention=True, short_name="ok"
        )
        assert gate.allowed is False
