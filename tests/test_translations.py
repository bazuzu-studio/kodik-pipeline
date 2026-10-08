"""Сопоставление озвучек справочника с translations/v2 Kodik (без сети)."""
import argparse
import json

import pytest

from kodik_pipeline import translations as tr


def vo(title, **extra):
    from kodik_pipeline.text_utils import slugify
    return {"title": title, "slug": slugify(title), "kodikTranslationId": None, **extra}


def kodik(*rows):
    return [{"id": i, "title": t, "count": c} for i, t, c in rows]


def test_normalize_ignores_case_and_punctuation():
    assert tr.normalize("Re: Voice") == tr.normalize("RE:VOICE") == "revoice"
    assert tr.normalize("SHIZA Project") == tr.normalize("Shiza Project")
    assert tr.loose("AniLibria.TV") == tr.loose("AniLibria") == "anilibria"
    assert tr.loose("TV") == "tv"  # короткие названия не обрезаются


def test_strict_and_loose_matches():
    report = tr.match_voiceovers(
        [vo("AniLibria"), vo("SHIZA Project"), vo("Re: Voice")],
        kodik((609, "AniLibria.TV", 5000), (767, "Shiza Project", 900), (1, "Re:Voice", 40), (2, "#студияБУБНЯЖА", 8)),
    )
    assert {s: m[0] for s, m in report.matched.items()} == {"anilibria": 609, "shiza-project": 767, "re-voice": 1}
    assert not report.not_found and not report.ambiguous


def test_name_in_parentheses_is_tried_separately():
    report = tr.match_voiceovers([vo("DeliciousDub (ChillDUB)")], kodik((55, "ChillDUB", 12)))
    assert report.matched["deliciousdub-chilldub"][0] == 55


def test_aliases_help_when_kodik_names_it_differently():
    entry = vo("AnimeVostorg", aliases=["AnimeVost"])
    assert tr.match_voiceovers([entry], kodik((9, "AnimeVost", 300))).matched["animevostorg"][0] == 9
    assert tr.match_voiceovers([vo("AnimeVostorg")], kodik((9, "AnimeVost", 300))).not_found == ["animevostorg"]


def test_several_kodik_voiceovers_with_same_name_are_not_guessed():
    rows = kodik((10, "AniDub", 100), (11, "Anidub", 700))
    report = tr.match_voiceovers([vo("AniDub")], rows)
    assert not report.matched and [o[0] for o in report.ambiguous["anidub"]] == [11, 10]  # крупная первой
    picked = tr.match_voiceovers([vo("AniDub")], rows, pick_largest=True)
    assert picked.matched["anidub"][0] == 11


def test_similar_names_are_only_suggestions():
    report = tr.match_voiceovers([vo("Amber")], kodik((7, "Amber Studio Team", 20)))
    assert not report.matched and report.not_found == ["amber"]
    assert report.suggestions["amber"][0][0] == 7


def test_same_kodik_id_for_two_voiceovers_is_a_conflict():
    report = tr.match_voiceovers([vo("AniLiberty"), vo("AniLiberty2", aliases=["AniLiberty"])], kodik((5, "AniLiberty", 1)))
    assert not report.matched and set(report.conflicts[5]) == {"aniliberty", "aniliberty2"}


def test_existing_id_is_never_overwritten_silently():
    report = tr.match_voiceovers([vo("AniDub", kodikTranslationId=1)], kodik((10, "AniDub", 100)))
    assert not report.matched and report.mismatches["anidub"] == (1, 10)


def test_fetch_follows_next_page():
    pages = {
        "first": {"results": [{"id": 1, "title": "A", "count": 1}], "next_page": "second"},
        "second": {"results": [{"id": 2, "title": "B", "count": 2}], "next_page": None},
    }
    calls = []

    def fake(url):
        calls.append(url)
        return pages["second" if "second" in url else "first"]

    # next_page подменён на ключ, чтобы не городить URL
    pages["first"]["next_page"] = "https://x/second"
    out = tr.fetch_translations("TOKEN", fetch=fake)
    assert [t["id"] for t in out] == [1, 2]
    assert "token=TOKEN" in calls[0] and "types=anime-serial%2Canime" in calls[0]


def test_run_writes_ids_and_keeps_other_fields(tmp_path, monkeypatch, capsys):
    f = tmp_path / "voiceovers.json"
    f.write_text(json.dumps([
        {"title": "AniLibria", "slug": "anilibria"},
        {"title": "AnimeVostorg", "slug": "animevostorg", "aliases": ["AnimeVost"]},
        {"title": "Kansai", "slug": "kansai"},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tr, "fetch_translations", lambda token, types: kodik((609, "AniLibria.TV", 5000), (9, "AnimeVost", 300)))
    args = argparse.Namespace(file=str(f), token="t", types="anime", write=False, pick_largest=False)

    tr.run(args)  # без --write файл не меняется
    assert "kodikTranslationId" not in f.read_text(encoding="utf-8")

    args.write = True
    tr.run(args)
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert [e.get("kodikTranslationId") for e in saved] == [609, 9, None]
    assert saved[1]["aliases"] == ["AnimeVost"]
    assert "Не найдено (1)" in capsys.readouterr().out

    tr.run(args)  # повторный запуск идемпотентен
    assert json.loads(f.read_text(encoding="utf-8")) == saved


def test_token_is_never_written_to_the_file(tmp_path, monkeypatch):
    f = tmp_path / "voiceovers.json"
    f.write_text(json.dumps([{"title": "AniLibria"}]), encoding="utf-8")
    monkeypatch.setattr(tr, "fetch_translations", lambda token, types: kodik((609, "AniLibria", 1)))
    tr.run(argparse.Namespace(file=str(f), token="SECRET-TOKEN", types="anime", write=True, pick_largest=False))
    assert "SECRET-TOKEN" not in f.read_text(encoding="utf-8")


def test_untracked_translations_lists_unknown_by_size_and_skips_known():
    from kodik_pipeline.translations import MatchReport, untracked_translations

    voiceovers = [{"title": "A", "slug": "a", "kodikTranslationId": 1}]
    report = MatchReport()
    report.matched["b"] = (2, "B", 50, "строгое")
    report.ambiguous["c"] = [(3, "C", 10), (4, "C", 5)]
    translations = [
        {"id": 1, "title": "A", "count": 900}, {"id": 2, "title": "B", "count": 800},
        {"id": 3, "title": "C", "count": 700}, {"id": 4, "title": "C", "count": 600},
        {"id": 5, "title": "Новая", "count": 300}, {"id": 6, "title": "Мелкая", "count": 7},
        {"id": 7, "title": "Пустая", "count": 0},
    ]
    assert untracked_translations(voiceovers, translations, report) == [(5, "Новая", 300), (6, "Мелкая", 7)]
    assert untracked_translations(voiceovers, translations, report, limit=1) == [(5, "Новая", 300)]
    assert untracked_translations(voiceovers, translations, report, limit=0) == []
