"""
Клиент AniList GraphQL: расписание выхода серий по MyAnimeList-id.

Shikimori использует id MyAnimeList, поэтому content.shikimori_id однозначно
определяет тайтл в AniList (Media(idMal: ...)) — без поиска по названию, который
может выбрать не тот сезон.

Запрос проверен на реальных данных (Bleach: Thousand-Year Blood War, idMal 41467).

Особенности AniList, учтённые здесь:
  - airingAt — Unix-секунды, время ЭФИРА (Япония). Тот же формат, что у
    episodes.airing_at в CMS;
  - timeUntilAiring зависит от момента запроса — не запрашиваем и не храним;
  - pageInfo.hasNextPage у airingSchedule ненадёжен (приходило true при 25 записях
    из 30), поэтому листаем, пока страница заполнена целиком;
  - несколько серий могут иметь одинаковый airingAt (двойные финалы) — это норма;
  - несуществующий idMal даёт HTTP 404 с JSON-ошибкой «Not Found» — это не сбой,
    а «в AniList такого нет»;
  - лимит запросов ~90 в минуту (в плохие дни ниже): при 429 ждём Retry-After.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

ANILIST_URL = os.environ.get("ANILIST_URL", "").strip() or "https://graphql.anilist.co"
PER_PAGE = 50  # максимум AniList
MAX_PAGES = 10  # защита от бесконечного цикла: до 500 серий на тайтл

SCHEDULE_QUERY = """
query ($idMal: Int!, $page: Int = 1, $perPage: Int = 50) {
  Media(idMal: $idMal, type: ANIME) {
    id
    idMal
    status
    format
    episodes
    seasonYear
    title { romaji english }
    nextAiringEpisode { episode airingAt }
    airingSchedule(page: $page, perPage: $perPage) {
      edges { node { episode airingAt } }
    }
  }
}
"""

_sleep: Callable[[float], None] = time.sleep


class AniListError(RuntimeError):
    """Сбой AniList (сеть, лимиты, ошибка GraphQL) — не «тайтл не найден»."""


@dataclass
class AnimeSchedule:
    anilist_id: int
    mal_id: int
    status: str | None
    episodes_total: int | None
    title: str
    # (номер серии, airingAt) ближайшей невышедшей серии или None
    next_episode: tuple[int, int] | None
    # номер серии -> airingAt
    airing: dict[int, int] = field(default_factory=dict)


def post_json(payload: dict[str, Any], *, timeout: float = 30.0, retries: int = 4) -> dict[str, Any]:
    """POST в AniList с повторами на 429/5xx/сетевые сбои. Возвращает разобранный JSON
    (в том числе тело ошибки с полем errors — например, 404 «Not Found»)."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ANILIST_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "kodik-pipeline/2 (+schedule sync)",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                wait = _retry_after(exc)
                print(f"  [AniList] лимит запросов, жду {wait:.0f} с")
                _sleep(wait)
                last_error = exc
                continue
            if exc.code >= 500:
                _sleep(2 ** attempt)
                last_error = exc
                continue
            try:
                return json.loads(raw)  # 4xx с телом GraphQL-ошибки (404 Not Found и т. п.)
            except ValueError:
                raise AniListError(f"HTTP {exc.code}: {raw[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            _sleep(2 ** attempt)
            last_error = exc
    raise AniListError(f"AniList недоступен после {retries + 1} попыток: {last_error}")


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        return max(1.0, min(float(exc.headers.get("Retry-After", "60")), 120.0))
    except (TypeError, ValueError):
        return 60.0


def _is_not_found(errors: list[dict[str, Any]]) -> bool:
    return any(e.get("status") == 404 or str(e.get("message", "")).casefold() == "not found." for e in errors)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def fetch_schedule(
    mal_id: int,
    *,
    post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    per_page: int = PER_PAGE,
) -> AnimeSchedule | None:
    """Расписание тайтла по idMal. None — в AniList такого тайтла нет.
    Бросает AniListError при сбоях."""
    post = post or post_json
    airing: dict[int, int] = {}
    media: dict[str, Any] | None = None

    for page in range(1, MAX_PAGES + 1):
        data = post({"query": SCHEDULE_QUERY, "variables": {"idMal": mal_id, "page": page, "perPage": per_page}})
        errors = data.get("errors") or []
        if errors:
            if _is_not_found(errors):
                return None
            raise AniListError("; ".join(str(e.get("message", e)) for e in errors))

        media = (data.get("data") or {}).get("Media")
        if not media:
            return None

        edges = ((media.get("airingSchedule") or {}).get("edges")) or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            number, at = _int_or_none(node.get("episode")), _int_or_none(node.get("airingAt"))
            if number is not None and at is not None and number > 0 and at > 0:
                airing.setdefault(number, at)
        if len(edges) < per_page:  # hasNextPage ненадёжен — ориентируемся на заполненность страницы
            break

    assert media is not None
    nxt = media.get("nextAiringEpisode") or {}
    next_episode = None
    if _int_or_none(nxt.get("episode")) and _int_or_none(nxt.get("airingAt")):
        next_episode = (int(nxt["episode"]), int(nxt["airingAt"]))

    title = media.get("title") or {}
    return AnimeSchedule(
        anilist_id=int(media["id"]),
        mal_id=int(media.get("idMal") or mal_id),
        status=media.get("status"),
        episodes_total=_int_or_none(media.get("episodes")),
        title=str(title.get("english") or title.get("romaji") or ""),
        next_episode=next_episode,
        airing=airing,
    )
