from rag_sync.chunking import chunk_text, embedding_input


def test_short_text_is_one_chunk():
    assert chunk_text("Hello.\n\nWorld.", 100) == ["Hello.\n\nWorld."]


def test_empty_text_has_no_chunks():
    assert chunk_text("", 100) == []
    assert chunk_text("  \n\n  ", 100) == []


def test_paragraphs_are_packed_up_to_the_limit():
    text = "\n\n".join(["a" * 40, "b" * 40, "c" * 40])
    assert chunk_text(text, 90) == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]


def test_no_chunk_is_longer_than_the_limit():
    text = "\n\n".join(["word " * 100, "x" * 1000, "Sentence one. Sentence two. " * 30])
    chunks = chunk_text(text, 200)
    assert chunks
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c.replace("\n", "").replace(" ", "") for c in chunks) == text.replace("\n", "").replace(" ", "")


def test_long_paragraph_is_cut_at_a_sentence_end():
    text = "First sentence here. " + "y" * 30
    assert chunk_text(text, 40) == ["First sentence here.", "y" * 30]


def test_embedding_input_prefixes_the_title():
    assert embedding_input("Pricing", "Pro is 29 EUR") == "Pricing\n\nPro is 29 EUR"
    assert embedding_input("", "text") == "text"
