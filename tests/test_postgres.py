"""End-to-end against a real Postgres with pgvector.

Skipped unless RAG_SYNC_TEST_DATABASE_URL points to a throwaway database,
for example:

    docker run -d --name rag-sync-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 pgvector/pgvector:pg17
    RAG_SYNC_TEST_DATABASE_URL=postgresql://postgres:pg@localhost:55432/postgres pytest
"""

import os
from pathlib import Path

import pytest

from rag_sync.config import Config, Source
from rag_sync.store import PostgresStore
from rag_sync.sync import sync_source

from fakes import FakeEmbedder, vector

URL = os.environ.get("RAG_SYNC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="RAG_SYNC_TEST_DATABASE_URL not set")

SCHEMA = (Path(__file__).parent.parent / "sql" / "schema.sql").read_text()
SOURCE = Source(
    name="notes",
    query="select id, title, body as content, owner as owner_id, edited_at as updated_at, "
          "jsonb_build_object('topic', topic) as metadata from app_notes",
)
CONFIG = Config(sources=(SOURCE,), chunk_chars=300, batch_size=3)


@pytest.fixture
def store():
    store = PostgresStore.connect(URL)
    store.conn.execute("drop schema if exists rag_sync cascade")
    store.conn.execute("drop table if exists app_notes")
    store.conn.execute(SCHEMA)
    store.conn.execute(
        "create table app_notes (id int primary key, title text, body text, owner text, topic text, "
        "edited_at timestamptz default now())"
    )
    yield store
    store.conn.execute("drop schema rag_sync cascade")
    store.conn.execute("drop table app_notes")
    store.close()


def notes(store, *rows):
    store.conn.execute("delete from app_notes")
    for row in rows:
        store.conn.execute("insert into app_notes (id, title, body, owner, topic) values (%s, %s, %s, %s, %s)", row)


def count(store, table):
    return store.conn.execute(f"select count(*) from rag_sync.{table}").fetchone()[0]


def test_sync_search_and_resync(store):
    embedder = FakeEmbedder(dims=1536)
    long_body = "\n\n".join(f"Section {i}. " + "w" * 200 for i in range(5))
    notes(store,
          (1, "Integrals", "How to integrate by parts.", "u1", "math"),
          (2, "Webhooks", long_body, None, "api"),
          (3, "Old export", "The CSV export tool.", "u2", "api"))

    first = sync_source(store, embedder, CONFIG, SOURCE)
    assert len(first.plan.added) == 3
    assert count(store, "documents") == 3
    assert count(store, "chunks") == 1 + 5 + 1
    assert count(store, "runs") == 1

    # Nothing changed: nothing embedded, one more run logged.
    calls = len(embedder.calls)
    again = sync_source(store, embedder, CONFIG, SOURCE)
    assert not again.plan.has_changes and len(embedder.calls) == calls

    # Edit one, delete one, add one.
    notes(store,
          (1, "Integrals", "How to integrate by parts. Now with examples.", "u1", "math"),
          (2, "Webhooks", long_body, None, "api"),
          (4, "Limits", "100 requests per minute.", "u1", "api"))
    third = sync_source(store, embedder, CONFIG, SOURCE)
    assert (len(third.plan.added), len(third.plan.modified), third.plan.deleted) == (1, 1, ["3"])
    assert sorted(r[0] for r in store.conn.execute("select doc_id from rag_sync.documents")) == ["1", "2", "4"]
    assert count(store, "chunks") == 1 + 5 + 1
    meta = store.conn.execute("select metadata from rag_sync.documents where doc_id = '4'").fetchone()[0]
    assert meta == {"topic": "api"}

    # Search: the exact text of a chunk comes first; owners only see their own plus shared.
    target = vector("Integrals\n\nHow to integrate by parts. Now with examples.", 1536)
    hits = store.search(target, "notes", None, 3)
    assert hits[0].doc_id == "1" and hits[0].similarity == pytest.approx(1.0)
    # One hit per document: "Webhooks" has 5 chunks but appears once, with its best chunk.
    u2_hits = store.search(target, None, "u2", 20)
    assert [h.doc_id for h in u2_hits] == ["2"]
    seen_by_u1 = {h.doc_id for h in store.search(target, None, "u1", 20)}
    assert seen_by_u1 == {"1", "2", "4"}
