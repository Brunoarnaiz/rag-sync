import pytest

from rag_sync.documents import Document, DuplicateIdError, fingerprint, make_plan

MODEL = "openai/text-embedding-3-small"


def doc(id, content="text", **kw):
    return Document(id=id, title=kw.pop("title", f"Doc {id}"), content=content, **kw)


def indexed(*docs, model=MODEL, chunk_chars=1500):
    return {d.id: fingerprint(d, model, chunk_chars) for d in docs}


def test_plan_sorts_documents_into_new_modified_deleted_unchanged():
    old = [doc("a"), doc("b"), doc("c")]
    new = [doc("a"), doc("b", "edited"), doc("d")]
    plan = make_plan(new, indexed(*old), MODEL, 1500)
    assert [d.id for d in plan.added] == ["d"]
    assert [d.id for d in plan.modified] == ["b"]
    assert plan.deleted == ["c"]
    assert plan.unchanged == 1
    assert plan.has_changes


def test_nothing_changed():
    docs = [doc("a"), doc("b")]
    plan = make_plan(docs, indexed(*docs), MODEL, 1500)
    assert not plan.has_changes and plan.unchanged == 2


@pytest.mark.parametrize("change", [
    {"title": "Renamed"},
    {"owner_id": "user-2"},
    {"metadata": {"tema": "3"}},
])
def test_title_owner_and_metadata_count_as_changes(change):
    before = doc("a", owner_id="user-1")
    after = doc("a", owner_id="user-1")
    for key, value in change.items():
        setattr(after, key, value)
    assert make_plan([after], indexed(before), MODEL, 1500).modified == [after]


def test_a_new_timestamp_alone_is_not_a_change():
    from datetime import datetime, timezone
    before = doc("a", updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    after = doc("a", updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert make_plan([after], indexed(before), MODEL, 1500).unchanged == 1


def test_changing_the_model_or_chunk_size_re_embeds_everything():
    docs = [doc("a"), doc("b")]
    assert len(make_plan(docs, indexed(*docs), "other/model", 1500).modified) == 2
    assert len(make_plan(docs, indexed(*docs), MODEL, 800).modified) == 2


def test_duplicate_ids_are_an_error():
    with pytest.raises(DuplicateIdError):
        make_plan([doc("a"), doc("a")], {}, MODEL, 1500)


def test_a_document_with_only_a_title_still_gets_one_chunk():
    assert doc("a", content="", title="Only a title").chunks(1500) == ["Only a title"]
    assert doc("a", content="", title="").chunks(1500) == []
