from kodik_pipeline.genres import title_index
from kodik_pipeline.richtext import build_richtext


def test_build_richtext_none_for_empty():
    assert build_richtext(None) is None
    assert build_richtext("") is None


def test_build_richtext_wraps_text_in_lexical_structure():
    doc = build_richtext("Описание")
    leaf = doc["root"]["children"][0]["children"][0]
    assert leaf["text"] == "Описание"
    assert leaf["type"] == "text"


def test_title_index_lowercases_keys_keeps_original_values():
    genres = [{"title": "Экшен"}, {"title": "Драма"}]
    idx = title_index(genres)
    assert idx == {"экшен": "Экшен", "драма": "Драма"}


def test_title_index_is_case_insensitive_for_unicode():
    genres = [{"title": "Экшен"}]
    idx = title_index(genres)
    assert idx["ЭКШЕН".casefold()] == "Экшен"
