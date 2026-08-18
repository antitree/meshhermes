"""Tests for channel matching and mention gating."""

import pytest

from policy import (
    resolve_channel_match,
    resolve_mention_gate,
    resolve_require_mention,
    strip_mention,
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

    def test_mid_sentence_mention_counts_but_is_not_stripped(self):
        text, mentioned = strip_mention("ping @hermes please", "hermes")
        assert mentioned is True
        assert "@hermes" in text

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
