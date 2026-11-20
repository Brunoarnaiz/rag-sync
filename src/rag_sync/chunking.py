"""Split a document into chunks small enough to embed."""

from __future__ import annotations

import re

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs into chunks of at most ``max_chars`` characters.

    Paragraph boundaries are kept whenever possible. A paragraph longer than
    ``max_chars`` is cut at the last line break or sentence end that fits, or
    hard-cut if there is none.
    """
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(text or "") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for piece in _split_long(paragraph, max_chars):
            if current and len(current) + 2 + len(piece) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    pieces = []
    rest = paragraph
    while len(rest) > max_chars:
        cut = max(rest.rfind("\n", 0, max_chars), rest.rfind(". ", 0, max_chars - 1) + 1)
        if cut < max_chars // 2:
            cut = max_chars
        piece, rest = rest[:cut].strip(), rest[cut:].strip()
        if piece:
            pieces.append(piece)
    if rest:
        pieces.append(rest)
    return pieces


def embedding_input(title: str, chunk: str) -> str:
    """The text that actually gets embedded: the chunk, prefixed by its document title."""
    return f"{title}\n\n{chunk}" if title else chunk
