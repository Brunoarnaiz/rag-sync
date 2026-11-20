import pytest

from rag_sync.config import Config, Source
from rag_sync.documents import Document
from rag_sync.sync import SyncRefused, sync_source

from fakes import FakeEmbedder, FakeStore

SOURCE = Source(name="docs", query="select ...")


def config(**kw):
    return Config(sources=(SOURCE,), **kw)


def doc(id, content="Some text.", owner=None):
    return Document(id=id, title=f"Doc {id}", content=content, owner_id=owner)


def test_first_sync_embeds_everything_and_logs_the_run():
    store = FakeStore([doc("a"), doc("b", owner="u1")])
    embedder = FakeEmbedder()
    result = sync_source(store, embedder, config(), SOURCE)
    assert len(result.plan.added) == 2 and result.chunks == 2
    assert set(store.docs) == {("docs", "a"), ("docs", "b")}
    assert store.docs[("docs", "b")]["owner"] == "u1"
    assert embedder.texts == ["Doc a\n\nSome text.", "Doc b\n\nSome text."]
    [run] = store.runs
    assert (run.added, run.modified, run.deleted, run.unchanged, run.chunks_embedded) == (2, 0, 0, 0, 2)


def test_second_sync_without_changes_embeds_nothing():
    store = FakeStore([doc("a"), doc("b")])
    sync_source(store, FakeEmbedder(), config(), SOURCE)
    embedder = FakeEmbedder()
    result = sync_source(store, embedder, config(), SOURCE)
    assert embedder.calls == []
    assert result.plan.unchanged == 2 and not result.plan.has_changes
    assert store.runs[-1].chunks_embedded == 0


def test_only_changed_documents_are_re_embedded_and_deleted_ones_are_dropped():
    store = FakeStore([doc("a"), doc("b"), doc("c")])
    sync_source(store, FakeEmbedder(), config(), SOURCE)

    store.source_rows = [doc("a"), doc("b", "New price: 39 EUR."), doc("d")]
    embedder = FakeEmbedder()
    result = sync_source(store, embedder, config(), SOURCE)

    assert sorted(embedder.texts) == ["Doc b\n\nNew price: 39 EUR.", "Doc d\n\nSome text."]
    assert set(store.docs) == {("docs", "a"), ("docs", "b"), ("docs", "d")}
    assert store.docs[("docs", "b")]["chunks"] == ["New price: 39 EUR."]
    assert (len(result.plan.added), len(result.plan.modified), result.plan.deleted) == (1, 1, ["c"])


def test_dry_run_changes_nothing_and_estimates_the_cost():
    store = FakeStore([doc("a", "x" * 4000)])
    result = sync_source(store, None, config(), SOURCE, dry_run=True)
    assert store.docs == {} and store.runs == []
    assert result.dry_run and result.estimated
    assert result.chunks == 3 and result.tokens > 900 and result.cost_usd > 0


def test_chunks_are_batched_across_documents():
    store = FakeStore([doc(str(i)) for i in range(10)])
    embedder = FakeEmbedder()
    sync_source(store, embedder, config(batch_size=4), SOURCE)
    assert [len(call) for call in embedder.calls] == [4, 4, 2]
    assert len(store.docs) == 10


def test_a_big_document_is_split_over_several_requests():
    store = FakeStore([doc("big", "\n\n".join(f"Paragraph {i}. " + "z" * 300 for i in range(9)))])
    embedder = FakeEmbedder()
    sync_source(store, embedder, config(batch_size=4, chunk_chars=400), SOURCE)
    assert [len(call) for call in embedder.calls] == [4, 4, 1]
    assert len(store.docs[("docs", "big")]["vectors"]) == 9


def test_an_empty_source_does_not_wipe_the_index():
    store = FakeStore([doc("a"), doc("b")])
    sync_source(store, FakeEmbedder(), config(), SOURCE)
    store.source_rows = []
    with pytest.raises(SyncRefused, match="no rows"):
        sync_source(store, FakeEmbedder(), config(), SOURCE)
    assert len(store.docs) == 2


def test_mass_deletion_is_refused_unless_forced():
    store = FakeStore([doc(str(i)) for i in range(10)])
    sync_source(store, FakeEmbedder(), config(), SOURCE)
    store.source_rows = [doc("0"), doc("1")]
    with pytest.raises(SyncRefused, match="8 of 10"):
        sync_source(store, FakeEmbedder(), config(), SOURCE)
    sync_source(store, FakeEmbedder(), config(), SOURCE, force=True)
    assert len(store.docs) == 2


def test_if_embedding_fails_midway_the_rest_is_picked_up_next_time():
    store = FakeStore([doc(str(i)) for i in range(6)])
    with pytest.raises(RuntimeError):
        sync_source(store, FakeEmbedder(fail_after=1), config(batch_size=2), SOURCE)
    assert len(store.docs) == 2 and store.runs == []

    embedder = FakeEmbedder()
    result = sync_source(store, embedder, config(batch_size=2), SOURCE)
    assert len(result.plan.added) == 4 and result.plan.unchanged == 2
    assert len(store.docs) == 6
