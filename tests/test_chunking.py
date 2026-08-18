"""Tests for LoRa chunking and markdown stripping.

The byte-accuracy tests are the important ones: MeshClaw measures UTF-16
code units against a byte-sized frame, so its chunks overflow on emoji and
CJK.  These tests pin the corrected behaviour.
"""

import pytest

from chunking import (
    MESHTASTIC_CHUNK_LIMIT,
    MESHTASTIC_HARD_LIMIT,
    byte_len,
    chunk_text,
    strip_markdown,
)


class TestChunkBasics:
    def test_empty_input(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_message_single_chunk(self):
        assert chunk_text("hello mesh") == ["hello mesh"]

    def test_whitespace_normalized(self):
        assert chunk_text("hello    mesh\n\nworld") == ["hello mesh world"]

    def test_splits_when_over_limit(self):
        text = " ".join(["word"] * 100)  # ~499 bytes
        chunks = chunk_text(text, limit=50)
        assert len(chunks) > 1
        for chunk in chunks:
            assert byte_len(chunk) <= 50

    def test_no_token_is_split_when_it_fits(self):
        text = " ".join(["alpha", "bravo", "charlie", "delta"])
        for chunk in chunk_text(text, limit=20):
            for token in chunk.split():
                assert token in ("alpha", "bravo", "charlie", "delta")

    def test_reassembles_to_original_tokens(self):
        text = " ".join(f"token{i}" for i in range(50))
        chunks = chunk_text(text, limit=60)
        assert " ".join(chunks).split() == text.split()

    def test_default_limits(self):
        text = "x " * 500
        for chunk in chunk_text(text):
            assert byte_len(chunk) <= MESHTASTIC_CHUNK_LIMIT


class TestByteAccuracy:
    """A LoRa frame is bytes, not characters — the MeshClaw fix."""

    def test_emoji_respects_byte_limit(self):
        # Each 😀 is 4 UTF-8 bytes.  Measured as characters, 60 of them look
        # like 60 "chars" and would fit a 200-char limit; as bytes they are
        # 240 and overflow the frame.
        text = " ".join("😀" * 3 for _ in range(40))
        chunks = chunk_text(text, limit=50)
        for chunk in chunks:
            assert byte_len(chunk) <= 50

    def test_cjk_respects_byte_limit(self):
        # CJK codepoints are 3 UTF-8 bytes each.
        text = " ".join("日本語テキスト" for _ in range(30))
        chunks = chunk_text(text, limit=60)
        for chunk in chunks:
            assert byte_len(chunk) <= 60

    def test_multibyte_never_split_mid_codepoint(self):
        text = "😀" * 200
        chunks = chunk_text(text, limit=50, hard_limit=50)
        for chunk in chunks:
            # Round-trips cleanly => no broken surrogate/partial sequence.
            assert chunk.encode("utf-8").decode("utf-8") == chunk

    def test_mixed_ascii_and_multibyte(self):
        text = "hello 世界 goodbye 🌍 " * 20
        for chunk in chunk_text(text, limit=64):
            assert byte_len(chunk) <= 64


class TestUrlHandling:
    def test_long_url_kept_whole(self):
        url = "https://example.com/" + "a" * 180  # 200 bytes, > soft limit
        chunks = chunk_text(f"see {url} ok", limit=100)
        assert url in chunks, "URL longer than the soft limit must stay intact"

    def test_url_over_hard_limit_is_split(self):
        url = "https://example.com/" + "a" * 400
        chunks = chunk_text(url, limit=100, hard_limit=230)
        assert len(chunks) > 1
        for chunk in chunks:
            assert byte_len(chunk) <= 230
        assert "".join(chunks) == url


class TestLimits:
    def test_soft_limit_clamped_to_hard(self):
        # Callers may request stricter chunks, never looser than the frame.
        text = " ".join(["word"] * 200)
        for chunk in chunk_text(text, limit=500, hard_limit=100):
            assert byte_len(chunk) <= 100

    def test_hard_limit_default_ceiling(self):
        text = "z" * 1000
        for chunk in chunk_text(text, limit=MESHTASTIC_CHUNK_LIMIT):
            assert byte_len(chunk) <= MESHTASTIC_HARD_LIMIT

    def test_hard_split_only_as_last_resort(self):
        token = "y" * 300
        chunks = chunk_text(token, limit=200, hard_limit=230)
        assert len(chunks) == 2
        assert "".join(chunks) == token

    def test_tiny_limit_does_not_hang(self):
        assert chunk_text("abcdef", limit=1, hard_limit=1) == list("abcdef")


class TestStripMarkdown:
    def test_bold_and_italic(self):
        assert strip_markdown("**bold**") == "bold"
        assert strip_markdown("__bold__") == "bold"
        assert strip_markdown("*italic*") == "italic"
        assert strip_markdown("_italic_") == "italic"

    def test_inline_code_and_fences(self):
        assert strip_markdown("`code`") == "code"
        assert "python" not in strip_markdown("```python\nx = 1\n```")

    def test_link_becomes_text_and_url(self):
        assert strip_markdown("[docs](https://example.com)") == "docs (https://example.com)"

    def test_image_becomes_url_and_is_not_read_as_link(self):
        # Images must be handled before links, else ![a](u) leaves a stray "!".
        assert strip_markdown("![alt](https://example.com/i.png)") == "https://example.com/i.png"

    def test_empty(self):
        assert strip_markdown("") == ""

    def test_plain_text_untouched(self):
        assert strip_markdown("just plain text") == "just plain text"

    def test_underscores_inside_words_preserved(self):
        # snake_case identifiers must survive.
        assert strip_markdown("call my_func now") == "call my_func now"

    def test_chunking_applies_after_stripping(self):
        text = "**" + " ".join(["word"] * 50) + "**"
        chunks = chunk_text(strip_markdown(text), limit=50)
        for chunk in chunks:
            assert "*" not in chunk
