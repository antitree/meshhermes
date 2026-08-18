"""Tests for node ID and target normalization."""

import pytest

from normalize import (
    BROADCAST_ID,
    channel_name_from_target,
    hex_to_node_num,
    is_channel_target,
    looks_like_node_id,
    node_num_to_hex,
    normalize_allow_entry,
    normalize_node_id,
    normalize_target,
)


class TestHexConversion:
    def test_num_to_hex_example_from_spec(self):
        assert node_num_to_hex(2882400001) == "!abcdef01"

    def test_num_to_hex_zero_pads_to_eight(self):
        assert node_num_to_hex(1) == "!00000001"
        assert node_num_to_hex(0) == "!00000000"

    def test_num_to_hex_max(self):
        assert node_num_to_hex(0xFFFFFFFF) == "!ffffffff"

    @pytest.mark.parametrize("bad", [-1, 0x1_0000_0000, "12", None, True])
    def test_num_to_hex_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            node_num_to_hex(bad)

    def test_hex_to_num_roundtrip(self):
        for num in (0, 1, 12345, 2882400001, 0xFFFFFFFF):
            assert hex_to_node_num(node_num_to_hex(num)) == num

    def test_hex_to_num_accepts_bare_and_uppercase(self):
        assert hex_to_node_num("abcd0001") == 2882338817
        assert hex_to_node_num("!ABCD0001") == 2882338817

    @pytest.mark.parametrize("bad", ["", "!", "zzzz", "!xyz", "123456789", None, 42])
    def test_hex_to_num_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            hex_to_node_num(bad)


class TestNormalizeNodeId:
    def test_canonical_passthrough(self):
        assert normalize_node_id("!abcd0001") == "!abcd0001"

    def test_uppercase_normalized(self):
        assert normalize_node_id("!ABCD0001") == "!abcd0001"

    def test_bare_hex_with_letters_is_hex(self):
        assert normalize_node_id("abcd0001") == "!abcd0001"

    def test_bang_prefixed_all_decimal_is_hex(self):
        # The "!" marker forces hex interpretation.
        assert normalize_node_id("!12345678") == "!12345678"
        assert hex_to_node_num("!12345678") == 0x12345678

    def test_bare_all_decimal_is_decimal(self):
        # THE disambiguation rule: bare all-decimal parses as a decimal node
        # number, not as hex.  Getting this backwards misroutes DMs.
        assert normalize_node_id("12345678") == node_num_to_hex(12345678)
        assert normalize_node_id("12345678") == "!00bc614e"

    def test_decimal_and_hex_forms_of_same_node_agree(self):
        assert normalize_node_id("2882338817") == normalize_node_id("!abcd0001")

    def test_short_hex_zero_padded(self):
        assert normalize_node_id("!ff") == "!000000ff"

    def test_accepts_int(self):
        assert normalize_node_id(2882400001) == "!abcdef01"

    def test_whitespace_tolerated(self):
        assert normalize_node_id("  !abcd0001  ") == "!abcd0001"

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "hello world", "!zzzzzzzz", "123456789012", None, True, 3.5, "!"],
    )
    def test_rejects_garbage(self, bad):
        assert normalize_node_id(bad) is None

    def test_out_of_range_decimal_rejected(self):
        assert normalize_node_id(str(0x1_0000_0000)) is None


class TestLooksLikeNodeId:
    @pytest.mark.parametrize("good", ["!abcd0001", "abcd0001", "12345678", "!ff"])
    def test_true_for_node_ids(self, good):
        assert looks_like_node_id(good) is True

    @pytest.mark.parametrize("bad", ["LongFast", "channel:LongFast", "", None])
    def test_false_otherwise(self, bad):
        assert looks_like_node_id(bad) is False


class TestNormalizeTarget:
    def test_strips_platform_prefix(self):
        assert normalize_target("meshtastic:!abcd0001") == "!abcd0001"

    def test_strips_user_prefix(self):
        assert normalize_target("user:!abcd0001") == "!abcd0001"

    def test_strips_node_prefix(self):
        assert normalize_target("node:abcd0001") == "!abcd0001"

    def test_strips_stacked_prefixes(self):
        assert normalize_target("meshtastic:user:!abcd0001") == "!abcd0001"

    def test_channel_target_preserved(self):
        assert normalize_target("channel:LongFast") == "channel:LongFast"

    def test_channel_case_preserved(self):
        # Device channel names are case-sensitive; do not fold them.
        assert normalize_target("channel:MyChan") == "channel:MyChan"

    def test_bare_non_node_string_is_a_channel(self):
        assert normalize_target("LongFast") == "channel:LongFast"

    def test_platform_prefixed_channel(self):
        assert normalize_target("meshtastic:channel:LongFast") == "channel:LongFast"

    def test_broadcast_spellings(self):
        for spelling in ("^all", "all", "broadcast", "BROADCAST"):
            assert normalize_target(spelling) == BROADCAST_ID

    def test_accepts_int(self):
        assert normalize_target(2882400001) == "!abcdef01"

    @pytest.mark.parametrize("bad", ["", "   ", None, "meshtastic:", "channel:"])
    def test_rejects_empty(self, bad):
        assert normalize_target(bad) is None


class TestChannelHelpers:
    def test_is_channel_target(self):
        assert is_channel_target("channel:LongFast") is True
        assert is_channel_target("CHANNEL:LongFast") is True
        assert is_channel_target("!abcd0001") is False
        assert is_channel_target(None) is False

    def test_channel_name_extraction(self):
        assert channel_name_from_target("channel:LongFast") == "LongFast"
        assert channel_name_from_target("channel:") is None
        assert channel_name_from_target("!abcd0001") is None


class TestNormalizeAllowEntry:
    def test_wildcard(self):
        assert normalize_allow_entry("*") == "*"

    def test_node_forms_converge(self):
        # Every spelling of the same node must compare equal in an allowlist.
        expected = "!abcd0001"
        for spelling in ("!abcd0001", "!ABCD0001", "ABCD0001", "2882338817",
                         "meshtastic:!abcd0001", "user:ABCD0001"):
            assert normalize_allow_entry(spelling) == expected

    def test_channel_entry(self):
        assert normalize_allow_entry("channel:longfast") == "channel:longfast"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_rejects_empty(self, bad):
        assert normalize_allow_entry(bad) is None
