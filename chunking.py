"""LoRa-sized text chunking and markdown stripping.

Port of MeshClaw's ``src/chunk.ts`` with one deliberate correction: MeshClaw
measures chunk length in JavaScript ``.length`` (UTF-16 code units) while a
LoRa frame carries **bytes**.  A message of emoji or CJK text therefore
overflows the frame in the TypeScript original.  Everything here measures
``len(s.encode("utf-8"))``.

Pure logic — no Hermes or ``meshtastic`` imports.
"""

from __future__ import annotations

import re
from typing import List

__all__ = [
    "MESHTASTIC_CHUNK_LIMIT",
    "MESHTASTIC_HARD_LIMIT",
    "byte_len",
    "chunk_text",
    "strip_markdown",
]

# Soft default: the limit we aim for, leaving headroom in the frame.
MESHTASTIC_CHUNK_LIMIT = 200
# Physical ceiling: a Meshtastic text payload cannot exceed this many bytes.
MESHTASTIC_HARD_LIMIT = 230


def byte_len(text: str) -> int:
    """Length of *text* in UTF-8 bytes — the unit LoRa frames are sized in."""
    return len(text.encode("utf-8"))


def _split_by_bytes(token: str, limit: int) -> List[str]:
    """Hard-split *token* into pieces of at most *limit* UTF-8 bytes.

    Splits on character boundaries, never mid-codepoint, so every piece is
    valid UTF-8.  Used only as the last resort for a single token that
    cannot fit in one frame (e.g. a 400-character URL).
    """
    pieces: List[str] = []
    current = ""
    for ch in token:
        candidate = current + ch
        if byte_len(candidate) > limit:
            if current:
                pieces.append(current)
                current = ch
            else:
                # A single character wider than the limit — emit it alone
                # rather than looping forever.  Only possible with an
                # absurdly small limit.
                pieces.append(ch)
                current = ""
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_text(
    text: str,
    limit: int = MESHTASTIC_CHUNK_LIMIT,
    hard_limit: int = MESHTASTIC_HARD_LIMIT,
) -> List[str]:
    """Split *text* into LoRa-sized chunks, measured in UTF-8 bytes.

    Algorithm (semantics preserved from ``chunk.ts``):

    1. Clamp the soft limit to the hard limit — a caller may ask for
       stricter chunks, never looser ones than the frame allows.
    2. Greedily pack whitespace-separated tokens; never split a token that
       fits in a frame.
    3. A token longer than the soft limit (typically a URL) is emitted whole
       up to the hard limit, so links stay clickable on the receiving radio.
    4. Only hard-split a token that cannot fit in a single frame at all.

    Returns ``[]`` for empty/whitespace-only input.
    """
    if not text:
        return []

    hard = max(1, int(hard_limit))
    soft = max(1, int(limit))
    # Rule 1: soft may be stricter than hard, never looser.
    soft = min(soft, hard)

    tokens = text.split()
    if not tokens:
        return []

    chunks: List[str] = []
    current = ""

    for token in tokens:
        token_bytes = byte_len(token)

        # Rule 4: token cannot fit even one frame — flush and hard-split it.
        if token_bytes > hard:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_by_bytes(token, hard))
            continue

        # Rule 3: token exceeds the soft limit but fits a frame (a URL).
        # Give it a frame of its own so it survives intact.
        if token_bytes > soft:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(token)
            continue

        # Rule 2: greedy packing against the soft limit.
        candidate = f"{current} {token}" if current else token
        if byte_len(candidate) <= soft:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = token

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# Markdown stripping
# ---------------------------------------------------------------------------
#
# A radio renders raw asterisks and backticks literally, wasting scarce
# airtime on characters that carry no meaning.  These regexes match IRC's
# ``_strip_markdown`` (plugins/platforms/irc/adapter.py) so behaviour is
# consistent across text-only Hermes platforms.

_RE_CODE_FENCE = re.compile(r"```\w*\n?")
_RE_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
_RE_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_RE_ITALIC_UNDER = re.compile(r"(?<!\w)_(.+?)_(?!\w)", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`(.+?)`", re.DOTALL)
_RE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def strip_markdown(text: str) -> str:
    """Convert markdown to plain text for transmission over the radio.

    Images become their URL and links become ``text (url)``; images are
    handled first so ``![alt](url)`` is not mis-parsed as a link.
    """
    if not text:
        return ""
    text = _RE_CODE_FENCE.sub("", text)
    text = _RE_BOLD_STAR.sub(r"\1", text)
    text = _RE_BOLD_UNDER.sub(r"\1", text)
    text = _RE_ITALIC_STAR.sub(r"\1", text)
    text = _RE_ITALIC_UNDER.sub(r"\1", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_IMAGE.sub(r"\2", text)
    text = _RE_LINK.sub(r"\1 (\2)", text)
    return text
