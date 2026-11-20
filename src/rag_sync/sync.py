"""One sync of one source: compare, embed what changed, delete what's gone, log the run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .chunking import embedding_input
from .config import Config, Source
from .documents import Document, Plan, make_plan
from .embedder import Embedder
from .store import RunStats, Store

CHARS_PER_TOKEN = 4  # rough estimate, only for --dry-run


class SyncRefused(Exception):
    """The sync looked dangerous (it would wipe most of the index) and --force was not given."""


@dataclass
class SyncResult:
    source: str
    plan: Plan
    dry_run: bool
    chunks: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    estimated: bool = False


def sync_source(
    store: Store,
    embedder: Embedder | None,
    config: Config,
    source: Source,
    *,
    dry_run: bool = False,
    force: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> SyncResult:
    started_at = datetime.now(timezone.utc)
    documents = store.fetch_documents(source.query)
    indexed = store.indexed_hashes(source.name)
    plan = make_plan(documents, indexed, config.model, config.chunk_chars)

    if indexed and not force:
        _check_deletions(source.name, plan, len(indexed), config.max_delete_ratio)

    work = [(doc, doc.chunks(config.chunk_chars)) for doc in plan.to_embed]
    total_chunks = sum(len(chunks) for _, chunks in work)

    if dry_run:
        chars = sum(len(embedding_input(doc.title, c)) for doc, chunks in work for c in chunks)
        tokens = chars // CHARS_PER_TOKEN
        return SyncResult(
            source=source.name, plan=plan, dry_run=True, chunks=total_chunks, tokens=tokens,
            cost_usd=tokens * config.price_per_million / 1_000_000, estimated=True,
        )

    if work and embedder is None:
        raise ValueError("an embedder is needed to sync changes")

    tokens = 0
    cost = 0.0
    done = 0
    for group in _groups(work, config.batch_size):
        texts = [embedding_input(doc.title, chunk) for doc, chunks in group for chunk in chunks]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), config.batch_size):
            result = embedder.embed(texts[start:start + config.batch_size])
            vectors.extend(result.vectors)
            tokens += result.tokens
            cost += result.cost_usd
        offset = 0
        for doc, chunks in group:
            store.write_document(source.name, doc, plan.hashes[doc.id], chunks, vectors[offset:offset + len(chunks)])
            offset += len(chunks)
        done += len(texts)
        log(f"  {source.name}: embedded {done}/{total_chunks} chunks")

    store.delete_documents(source.name, plan.deleted)
    store.record_run(
        RunStats(
            source=source.name, started_at=started_at, added=len(plan.added), modified=len(plan.modified),
            deleted=len(plan.deleted), unchanged=plan.unchanged, chunks_embedded=total_chunks,
            tokens=tokens, cost_usd=cost,
        )
    )
    return SyncResult(source=source.name, plan=plan, dry_run=False, chunks=total_chunks, tokens=tokens, cost_usd=cost)


def _check_deletions(name: str, plan: Plan, indexed: int, max_ratio: float) -> None:
    if not plan.hashes:
        raise SyncRefused(
            f"{name}: the query returned no rows, so the sync would delete all {indexed} indexed documents. "
            "If that is really what you want, run again with --force."
        )
    ratio = len(plan.deleted) / indexed
    if ratio > max_ratio:
        raise SyncRefused(
            f"{name}: the sync would delete {len(plan.deleted)} of {indexed} indexed documents "
            f"({ratio:.0%}, limit {max_ratio:.0%}). Check the query, or run again with --force."
        )


def _groups(work: list[tuple[Document, list[str]]], batch_size: int):
    """Group documents so each group fills roughly one embedding request."""
    group: list[tuple[Document, list[str]]] = []
    size = 0
    for doc, chunks in work:
        if not chunks:
            # Nothing to embed (empty title and content), but the index should still know it.
            yield [(doc, chunks)]
            continue
        if group and size + len(chunks) > batch_size:
            yield group
            group, size = [], 0
        group.append((doc, chunks))
        size += len(chunks)
    if group:
        yield group
