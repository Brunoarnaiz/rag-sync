"""Documents, their fingerprints, and the plan that compares the source with the index."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .chunking import chunk_text


@dataclass
class Document:
    id: str
    title: str
    content: str
    owner_id: str | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def chunks(self, max_chars: int) -> list[str]:
        chunks = chunk_text(self.content, max_chars)
        if not chunks and self.title.strip():
            # Nothing but a title: still worth finding by name.
            chunks = [self.title.strip()]
        return chunks


def fingerprint(doc: Document, model: str, chunk_chars: int) -> str:
    """sha256 of everything that ends up in the index for this document.

    The model and chunk size are included on purpose: change either and every
    document is re-embedded, because the old vectors are no longer comparable.
    The owner and metadata are included too, so the index never keeps stale
    access information. ``updated_at`` is not: a new timestamp on the same
    text is not a reason to pay for new embeddings.
    """
    payload = {
        "model": model,
        "chunk_chars": chunk_chars,
        "title": doc.title,
        "content": doc.content,
        "owner_id": doc.owner_id,
        "metadata": doc.metadata,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class Plan:
    added: list[Document]
    modified: list[Document]
    deleted: list[str]
    unchanged: int
    hashes: dict[str, str]

    @property
    def to_embed(self) -> list[Document]:
        return self.added + self.modified

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


class DuplicateIdError(ValueError):
    """The source query returned the same id twice."""


def make_plan(documents: list[Document], indexed: dict[str, str], model: str, chunk_chars: int) -> Plan:
    """Compare what the source has now with what the index holds.

    ``indexed`` maps doc id -> fingerprint, as stored at the last sync.
    """
    hashes: dict[str, str] = {}
    added: list[Document] = []
    modified: list[Document] = []
    unchanged = 0
    for doc in documents:
        if doc.id in hashes:
            raise DuplicateIdError(f"the source returned id {doc.id!r} more than once")
        digest = fingerprint(doc, model, chunk_chars)
        hashes[doc.id] = digest
        if doc.id not in indexed:
            added.append(doc)
        elif indexed[doc.id] != digest:
            modified.append(doc)
        else:
            unchanged += 1
    deleted = sorted(doc_id for doc_id in indexed if doc_id not in hashes)
    return Plan(added=added, modified=modified, deleted=deleted, unchanged=unchanged, hashes=hashes)
