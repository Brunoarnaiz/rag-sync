"""In-memory stand-ins for the database and the embeddings API."""

from __future__ import annotations

import hashlib

from rag_sync.documents import Document
from rag_sync.embedder import Embeddings
from rag_sync.store import RunStats


class FakeStore:
    def __init__(self, documents: list[Document] | None = None):
        self.source_rows: list[Document] = list(documents or [])
        self.docs: dict[tuple[str, str], dict] = {}
        self.runs: list[RunStats] = []
        self.writes = 0

    def fetch_documents(self, query: str) -> list[Document]:
        return list(self.source_rows)

    def indexed_hashes(self, source: str) -> dict[str, str]:
        return {doc_id: row["hash"] for (src, doc_id), row in self.docs.items() if src == source}

    def write_document(self, source, doc, content_hash, chunks, vectors):
        assert len(chunks) == len(vectors)
        self.docs[(source, doc.id)] = {"hash": content_hash, "chunks": chunks, "vectors": vectors, "owner": doc.owner_id}
        self.writes += 1

    def delete_documents(self, source, doc_ids):
        for doc_id in doc_ids:
            self.docs.pop((source, doc_id), None)

    def record_run(self, stats: RunStats) -> None:
        self.runs.append(stats)


class FakeEmbedder:
    """Deterministic vectors; counts calls and texts so tests can check what was paid for."""

    def __init__(self, dims: int = 8, fail_after: int | None = None):
        self.dims = dims
        self.calls: list[list[str]] = []
        self.fail_after = fail_after

    def embed(self, texts: list[str]) -> Embeddings:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("embedding API down")
        self.calls.append(list(texts))
        return Embeddings(vectors=[vector(t, self.dims) for t in texts], tokens=sum(len(t) for t in texts) // 4, cost_usd=0.001)

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]


def vector(text: str, dims: int) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255 + 0.01 for i in range(dims)]
