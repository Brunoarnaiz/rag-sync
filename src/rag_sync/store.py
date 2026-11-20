"""The index, stored in Postgres + pgvector (schema ``rag_sync``, see sql/schema.sql)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .documents import Document

REQUIRED_COLUMNS = ("id", "title", "content")


class SourceError(Exception):
    """A source query failed or returned the wrong columns."""


@dataclass
class RunStats:
    source: str
    started_at: datetime
    added: int
    modified: int
    deleted: int
    unchanged: int
    chunks_embedded: int
    tokens: int
    cost_usd: float


@dataclass
class Hit:
    source: str
    doc_id: str
    title: str
    content: str
    similarity: float


class Store(Protocol):
    def fetch_documents(self, query: str) -> list[Document]: ...
    def indexed_hashes(self, source: str) -> dict[str, str]: ...
    def write_document(self, source: str, doc: Document, content_hash: str, chunks: list[str], vectors: list[list[float]]) -> None: ...
    def delete_documents(self, source: str, doc_ids: list[str]) -> None: ...
    def record_run(self, stats: RunStats) -> None: ...


def _vector(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class PostgresStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    @classmethod
    def connect(cls, url: str) -> "PostgresStore":
        conn = psycopg.connect(url, autocommit=True, prepare_threshold=None)
        # On Supabase pgvector lives in the "extensions" schema. Appending it is
        # harmless elsewhere: a missing schema in search_path is ignored.
        conn.execute("select set_config('search_path', current_setting('search_path') || ', extensions', false)")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    # -- source --------------------------------------------------------------

    def fetch_documents(self, query: str) -> list[Document]:
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query)
                rows = cur.fetchall()
                columns = [c.name for c in cur.description or []]
        except psycopg.Error as exc:
            raise SourceError(str(exc).strip()) from exc
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise SourceError(f"the query must return columns {', '.join(REQUIRED_COLUMNS)}; missing: {', '.join(missing)}")
        documents = []
        for row in rows:
            if row["id"] is None:
                raise SourceError("the query returned a row with a null id")
            metadata = row.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise SourceError("'metadata' must be a JSON object")
            owner = row.get("owner_id")
            documents.append(
                Document(
                    id=str(row["id"]),
                    title=str(row["title"] or ""),
                    content=str(row["content"] or ""),
                    owner_id=None if owner is None else str(owner),
                    updated_at=row.get("updated_at"),
                    metadata=metadata,
                )
            )
        return documents

    # -- index ---------------------------------------------------------------

    def indexed_hashes(self, source: str) -> dict[str, str]:
        rows = self.conn.execute(
            "select doc_id, content_hash from rag_sync.documents where source = %s", (source,)
        ).fetchall()
        return {doc_id: digest for doc_id, digest in rows}

    def write_document(self, source: str, doc: Document, content_hash: str, chunks: list[str], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk")
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("delete from rag_sync.chunks where source = %s and doc_id = %s", (source, doc.id))
            cur.execute(
                """
                insert into rag_sync.documents
                    (source, doc_id, owner_id, title, content_hash, chunk_count, metadata, updated_at, synced_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, now())
                on conflict (source, doc_id) do update set
                    owner_id = excluded.owner_id,
                    title = excluded.title,
                    content_hash = excluded.content_hash,
                    chunk_count = excluded.chunk_count,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at,
                    synced_at = excluded.synced_at
                """,
                (source, doc.id, doc.owner_id, doc.title, content_hash, len(chunks), Jsonb(doc.metadata), doc.updated_at),
            )
            cur.executemany(
                """
                insert into rag_sync.chunks (source, doc_id, chunk_index, owner_id, content, embedding)
                values (%s, %s, %s, %s, %s, %s::vector)
                """,
                [(source, doc.id, i, doc.owner_id, chunk, _vector(vec)) for i, (chunk, vec) in enumerate(zip(chunks, vectors))],
            )

    def delete_documents(self, source: str, doc_ids: list[str]) -> None:
        if doc_ids:
            # Chunks go with them (on delete cascade).
            self.conn.execute(
                "delete from rag_sync.documents where source = %s and doc_id = any(%s)", (source, doc_ids)
            )

    def record_run(self, stats: RunStats) -> None:
        self.conn.execute(
            """
            insert into rag_sync.runs
                (source, started_at, added, modified, deleted, unchanged, chunks_embedded, tokens, cost_usd)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (stats.source, stats.started_at, stats.added, stats.modified, stats.deleted, stats.unchanged,
             stats.chunks_embedded, stats.tokens, stats.cost_usd),
        )

    # -- reading -------------------------------------------------------------

    def search(self, embedding: list[float], source: str | None, owner_id: str | None, k: int) -> list[Hit]:
        """Nearest chunks. With ``owner_id``, only that owner's chunks plus shared ones (owner_id is null)."""
        vec = _vector(embedding)
        rows = self.conn.execute(
            """
            select c.source, c.doc_id, d.title, c.content, 1 - (c.embedding <=> %(vec)s::vector) as similarity
            from rag_sync.chunks c
            join rag_sync.documents d using (source, doc_id)
            where (%(source)s::text is null or c.source = %(source)s)
              and (%(owner)s::text is null or c.owner_id is null or c.owner_id = %(owner)s)
            order by c.embedding <=> %(vec)s::vector
            limit %(k)s
            """,
            {"vec": vec, "source": source, "owner": owner_id, "k": k},
        ).fetchall()
        return [Hit(source=r[0], doc_id=r[1], title=r[2] or "", content=r[3], similarity=float(r[4])) for r in rows]

    def status(self) -> dict[str, Any]:
        sources = self.conn.execute(
            """
            select source, count(*), coalesce(sum(chunk_count), 0), max(synced_at)
            from rag_sync.documents group by source order by source
            """
        ).fetchall()
        runs = self.conn.execute(
            """
            select source, finished_at, added, modified, deleted, unchanged, chunks_embedded, tokens, cost_usd
            from rag_sync.runs order by id desc limit 10
            """
        ).fetchall()
        return {"sources": sources, "runs": runs}
