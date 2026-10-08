"""Клиент AniList — без сети. Фикстура — реальный ответ для BLEACH: Thousand-Year Blood War (idMal 41467)."""
import io
import json
import urllib.error

import pytest

from kodik_pipeline import anilist

BLEACH_AIRING = [
    1665414000, 1666018800, 1666623600, 1667228400, 1667833200, 1668438000, 1669042800,
    1669647600, 1670252400, 1670857200, 1671462000, 1672066800, 1672066800,  # 12 и 13 — двойной финал
]


def media(edges, **extra):
    return {"data": {"Media": {
        "id": 116674, "idMal": 41467, "status": "FINISHED", "format": "TV", "episodes": 13,
        "seasonYear": 2022, "title": {"romaji": "BLEACH: Sennen Kessen-hen", "english": "BLEACH: Thousand-Year Blood War"},
        "nextAiringEpisode": None,
        "airingSchedule": {"edges": [{"node": {"episode": n, "airingAt": at}} for n, at in edges]},
        **extra,
    }}}


def test_parses_real_bleach_response():
    post = lambda payload: media(list(enumerate(BLEACH_AIRING, start=1)))
    result = anilist.fetch_schedule(41467, post=post)
    assert (result.anilist_id, result.mal_id, result.status, result.episodes_total) == (116674, 41467, "FINISHED", 13)
    assert result.title == "BLEACH: Thousand-Year Blood War"
    assert result.next_episode is None
    assert len(result.airing) == 13 and result.airing[12] == result.airing[13] == 1672066800


def test_next_airing_episode_is_parsed():
    post = lambda payload: media([(1, 100)], nextAiringEpisode={"episode": 2, "airingAt": 200})
    assert anilist.fetch_schedule(1, post=post).next_episode == (2, 200)


def test_paginates_while_page_is_full_and_ignores_has_next_page():
    pages = {1: [(n, 1000 + n) for n in range(1, 4)], 2: [(4, 1004)]}
    calls = []

    def post(payload):
        page = payload["variables"]["page"]
        calls.append(page)
        data = media(pages[page])
        data["data"]["Media"]["airingSchedule"]["pageInfo"] = {"hasNextPage": True}  # врёт, как в реальных ответах
        return data

    result = anilist.fetch_schedule(1, post=post, per_page=3)
    assert calls == [1, 2] and sorted(result.airing) == [1, 2, 3, 4]


def test_not_found_returns_none():
    post = lambda payload: {"errors": [{"message": "Not Found.", "status": 404}], "data": {"Media": None}}
    assert anilist.fetch_schedule(999999999, post=post) is None
    assert anilist.fetch_schedule(1, post=lambda p: {"data": {"Media": None}}) is None


def test_graphql_error_is_raised():
    with pytest.raises(anilist.AniListError, match="Too many"):
        anilist.fetch_schedule(1, post=lambda p: {"errors": [{"message": "Too many complexity", "status": 400}]})


def test_junk_edges_are_skipped():
    post = lambda payload: media([(1, 100), (0, 50), (2, 0), (None, 5)])
    assert anilist.fetch_schedule(1, post=post).airing == {1: 100}


def test_variables_use_id_mal_and_validated_fields():
    seen = {}
    anilist.fetch_schedule(41467, post=lambda p: seen.update(p) or media([]))
    assert seen["variables"]["idMal"] == 41467
    assert "Media(idMal: $idMal, type: ANIME)" in seen["query"]
    assert "timeUntilAiring" not in seen["query"]  # зависит от момента запроса — не храним


# ─── HTTP-слой ───────────────────────────────────────────────────

class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def http_error(code, body="{}", headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(body.encode()))


def test_post_json_waits_on_429_then_succeeds(monkeypatch):
    sleeps, outcomes = [], [http_error(429, headers={"Retry-After": "7"}), FakeResponse(b'{"ok": 1}')]

    def fake_urlopen(request, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(anilist.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(anilist, "_sleep", sleeps.append)
    assert anilist.post_json({"query": "q"}) == {"ok": 1}
    assert sleeps == [7.0]


def test_post_json_returns_graphql_error_body_on_404(monkeypatch):
    body = json.dumps({"errors": [{"message": "Not Found.", "status": 404}], "data": {"Media": None}})
    monkeypatch.setattr(anilist.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(http_error(404, body)))
    assert anilist.fetch_schedule(1) is None  # 404 — «такого тайтла нет», а не сбой


def test_post_json_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(anilist.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")))
    monkeypatch.setattr(anilist, "_sleep", lambda s: None)
    with pytest.raises(anilist.AniListError, match="недоступен"):
        anilist.post_json({"query": "q"}, retries=2)
