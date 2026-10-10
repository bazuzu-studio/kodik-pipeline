"""
Kodik API client.

Источник данных теперь только Kodik API:
  GET https://kodik-api.com/list

Клиент умеет:
- читать token/translation_id из .env;
- ходить страницами;
- повторять запросы при 429/5xx/сетевых ошибках;
- превращать ответ API в формат, который уже понимает загрузчик Payload;
- выбирать только онгоинги (fetch_ongoing) и добирать отдельные записи
  по Kodik ID (fetch_by_kodik_id) для команды update-ongoing.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .config import (
    kodik_delay, kodik_limit, kodik_retries, kodik_timeout, require_env,
)
from . import franchise
from .status import is_ongoing, normalize_status
from .text_utils import slugify

API_URL = "https://kodik-api.com/list"
SEARCH_URL = "https://kodik-api.com/search"

# Kodik фильтрует статус разными полями: anime_status — аниме,
# all_status — остальные сериалы. Запрашиваем оба и дополнительно
# фильтруем на клиенте (если сервер проигнорирует параметр).
ONGOING_FILTER_FIELDS = ("anime_status", "all_status")
USER_AGENT = "kodik-pipeline/2.2"


def _request_url(url: str, timeout: int | None = None, retries: int | None = None) -> dict[str, Any]:
    """GET JSON по готовому URL. Kodik /list пагинируется через поле `next`."""
    timeout = kodik_timeout() if timeout is None else timeout
    retries = kodik_retries() if retries is None else retries
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Kodik API вернул не JSON object")
            return payload
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                detail = f": {body}" if body else ""
                raise RuntimeError(f"Kodik API HTTP {exc.code}{detail}") from exc
            wait = min(30, 2 ** (attempt - 1))
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_wait = max(0, float(retry_after)) if retry_after else wait
            except ValueError:
                retry_wait = wait
            retry_wait = min(30, retry_wait)
            print(f"  API HTTP {exc.code}; повтор через {retry_wait:g}с...")
            time.sleep(retry_wait)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt == retries:
                break
            wait = min(30, 2 ** (attempt - 1))
            print(f"  API ошибка: {exc}; повтор через {wait}с...")
            time.sleep(wait)

    raise RuntimeError(f"Не удалось получить Kodik API после {retries} попыток: {last_error}")


def _request(params: dict[str, Any], timeout: int | None = None, retries: int | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    return _request_url(f"{API_URL}?{query}", timeout=timeout, retries=retries)


def iter_items(
    *,
    token: str | None = None,
    translation_id: int | str | None = None,
    limit: int | None = None,
    delay: float | None = None,
    max_pages: int | None = None,
    extra_params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Потоково отдаёт все results из /list.

    extra_params — дополнительные фильтры Kodik (например anime_status).

    ВАЖНО: Kodik /list не использует параметр `page` для пагинации.
    После первой страницы API возвращает URL следующей страницы в поле `next`;
    именно этот URL нужно запрашивать дальше.
    """
    token = token or require_env("KODIK_TOKEN")
    translation_id = translation_id or os.getenv("KODIK_TRANSLATION_ID", "609")
    limit = kodik_limit() if limit is None else limit
    delay = kodik_delay() if delay is None else delay
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть в диапазоне 1..1000")
    if delay < 0:
        raise ValueError("delay не может быть отрицательным")

    params = {
        "token": token,
        "with_episodes": "true",
        "with_material_data": "true",
        "translation_id": translation_id,
        "limit": limit,
    }
    for key, value in (extra_params or {}).items():
        if value is not None:
            params[key] = value
    url: str | None = API_URL + "?" + urllib.parse.urlencode(params)
    page_number = 0
    seen_ids: set[str] = set()
    seen_pages: set[str] = set()

    while url:
        if max_pages is not None and page_number >= max_pages:
            return

        if url in seen_pages:
            raise RuntimeError("Kodik API вернул зацикленную пагинацию (next указывает на уже посещённый URL)")
        seen_pages.add(url)
        page_number += 1
        payload = _request_url(url)
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError("Kodik API: поле results имеет неожиданный тип")

        if not results:
            return

        added = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            added += 1
            yield item

        print(f"API page={page_number}: получено {len(results)}, новых {added}")

        next_url = payload.get("next_page") or payload.get("next")
        if not next_url:
            return
        if not isinstance(next_url, str):
            return
        url = urllib.parse.urljoin(API_URL, next_url)

        if delay:
            time.sleep(delay)


def _pick_genres(md: dict[str, Any]) -> list[str]:
    raw = md.get("anime_genres") or md.get("genres") or []
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            title = item.strip()
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or "").strip()
        else:
            continue
        if title and title.casefold() not in seen:
            seen.add(title.casefold())
            result.append(title)
    return result


def _normalize_link(link: Any) -> str | None:
    if not link:
        return None
    value = str(link).strip()
    return "https:" + value if value.startswith("//") else value


def _episodes_for_season(season_info: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    eps = season_info.get("episodes") or {}
    if not isinstance(eps, dict):
        return result
    for number, link in eps.items():
        try:
            number_int = int(number)
        except (TypeError, ValueError):
            continue
        result.append({"number": number_int, "playerLink": _normalize_link(link)})
    return sorted(result, key=lambda x: x["number"])


def _seasons(item: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seasons = item.get("seasons") or {}
    if not isinstance(seasons, dict):
        return result
    for season_number_raw, season_info in seasons.items():
        if not isinstance(season_info, dict):
            continue
        try:
            season_number = int(season_number_raw)
        except (TypeError, ValueError):
            continue
        result.append({
            "seasonNumber": season_number,
            "kodikId": item.get("id"),
            "link": _normalize_link(season_info.get("link")),
            "shikimoriId": str(item.get("shikimori_id") or ""),
            "title": item.get("title") or "",
            "releaseYear": item.get("year"),
            "episodes": _episodes_for_season(season_info),
        })
    return sorted(result, key=lambda x: x["seasonNumber"])


def _content_type(item: dict[str, Any], md: dict[str, Any]) -> str:
    raw = str(item.get("type") or md.get("type") or "").lower()
    if "movie" in raw:
        return "movie"
    if "serial" in raw or "series" in raw:
        return "series"
    # Kodik anime-serial обычно имеет seasons.
    return "series" if item.get("seasons") else "movie"


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Преобразует одну запись Kodik API в формат load.py."""
    md = item.get("material_data") or {}
    if not isinstance(md, dict):
        md = {}

    title_ru = md.get("title") or item.get("title") or item.get("title_orig") or "Без названия"
    title_en = (
        md.get("title_en")
        or md.get("titleEn")
        or item.get("title_en")
        or item.get("title_orig")
        or item.get("title")
        or f"kodik-{item.get('id')}"
    )
    slug = (
        md.get("slug")
        or item.get("slug")
        or slugify(str(title_en))
    )
    if not slug or slug == "unknown":
        slug = f"kodik-{item.get('id') or 'unknown'}"

    content_type = _content_type(item, md)
    record: dict[str, Any] = {
        "id": item.get("id"),
        "titleEn": str(title_en),
        "titleRu": str(title_ru),
        "slug": str(slug),
        "kodikId": item.get("id"),
        "type": content_type,
        "originalTitle": item.get("title_orig"),
        "description": md.get("anime_description") or md.get("description"),
        "releaseYear": md.get("year") or item.get("year"),
        "rating": _rating(md),
        "posterUrl": md.get("anime_poster_url") or md.get("poster_url"),
        "shikimoriId": item.get("shikimori_id") or md.get("shikimori_id"),
        "kinopoiskId": item.get("kinopoisk_id") or md.get("kinopoisk_id"),
        "imdbId": item.get("imdb_id") or md.get("imdb_id"),
        "genres": _pick_genres(md),
        "screenshots": md.get("screenshots") or item.get("screenshots") or [],
        # Ниже — поля из material_data. В БД пишутся minimalAge (age_rating)
        # и status (release_status, если такая колонка есть — см. status.py);
        # остальные пока используются только внутри пайплайна.
        "status": normalize_status(md.get("anime_status") or md.get("all_status")),
        "episodesTotal": md.get("episodes_total"),
        "episodesAired": md.get("episodes_aired"),
        "minimalAge": md.get("minimal_age"),
        "ratingMpaa": md.get("rating_mpaa"),
        "airedAt": md.get("aired_at"),
        "releasedAt": md.get("released_at"),
        "nextEpisodeAt": md.get("next_episode_at"),
        "quality": item.get("quality"),
        "translation": item.get("translation") or {},
        "worldArtLink": item.get("worldart_link"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
    }

    if content_type == "movie":
        duration = md.get("duration")
        try:
            record["duration"] = int(duration) if duration not in (None, "") else None
        except (TypeError, ValueError):
            record["duration"] = None
        # Прямая ссылка на плеер для фильма — у Kodik лежит в корневом
        # поле `link` (для сериалов ссылка на плеер берётся по-другому:
        # отдельно на каждый эпизод внутри seasons, см. _episodes_for_season).
        record["playerLink"] = _normalize_link(item.get("link"))
    else:
        record["seasons"] = _seasons(item)
        record["lastSeason"] = item.get("last_season")
        record["lastEpisode"] = item.get("last_episode")
        record["episodesCount"] = item.get("episodes_count")

    return record


def _rating(md: dict[str, Any]) -> float | None:
    value = md.get("shikimori_rating")
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return round(number, 1) if number > 0 else None


def fetch_normalized(
    *,
    token: str | None = None,
    translation_id: int | str | None = None,
    limit: int | None = None,
    delay: float | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    records = [normalize_item(item) for item in iter_items(
        token=token,
        translation_id=translation_id,
        limit=limit,
        delay=delay,
        max_pages=max_pages,
    )]

    # Убираем дубли по Kodik ID.
    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for rec in records:
        key = str(rec.get("kodikId") or rec.get("titleEn") or "")
        if key in seen_ids:
            continue
        seen_ids.add(key)
        result.append(rec)

    # Одинаковый shikimori_id — это один и тот же сезон: оставляем лучшую запись
    # (больше серий, свежее обновление), иначе он попал бы в БД дважды.
    result, skipped_dupes = franchise.drop_duplicate_records(result)
    for message in skipped_dupes:
        print(f"  [WARN] {message}")

    # Один и тот же тайтл у Kodik может иметь отдельную запись на каждый сезон
    # (и на каждую «часть»). Группируем только по точному kinopoisk_id/imdb_id.
    groups: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []
    for rec in result:
        if rec.get("type") != "series":
            standalone.append(rec)
            continue
        key = None
        if rec.get("kinopoiskId"):
            key = f"kp:{rec['kinopoiskId']}"
        elif rec.get("imdbId"):
            key = f"imdb:{rec['imdbId']}"
        if key:
            groups.setdefault(key, []).append(rec)
        else:
            standalone.append(rec)

    grouped: list[dict[str, Any]] = list(standalone)
    for group_key, items in groups.items():
        # Значение для content.franchise_id. Должно совпадать с тем, что миграция
        # CMS проставила существующим записям (= голый kinopoisk_id), иначе сезоны
        # одной франшизы разъедутся по разным идентификаторам. Ключ группировки
        # с префиксом (kp:/imdb:) нужен только внутри этой функции.
        franchise_id = group_key.split(":", 1)[1]

        kept, skipped = franchise.drop_duplicate_records(items)
        for message in skipped:
            print(f"  [WARN] франшиза {franchise_id}: {message}")
        items[:] = kept

        if len(items) == 1:
            # Единственная запись франшизы: настоящие номера сезонов Kodik не трогаем.
            franchise.number_standalone(items[0])
            items[0]["franchiseId"] = franchise_id
            grouped.append(items[0])
            continue

        for rec in franchise.assign_franchise_numbers(items):
            rec["franchiseId"] = franchise_id
            grouped.append(rec)

    for rec in standalone:
        if rec.get("type") == "series":
            franchise.number_standalone(rec)

    # Названия сезонов франшизы различаем суффиксом (S2, S3 Part 2): первый
    # сезон сохраняет базовое имя. Slug — тот же суффикс в нижнем регистре.
    for franchise_items in groups.values():
        if len(franchise_items) <= 1:
            continue
        for rec in franchise_items:
            suffix = franchise.title_suffix(rec)
            if suffix:
                rec["titleEn"] = f"{rec['titleEn']}{suffix}"
                rec["slug"] = f"{rec['slug']}{franchise.slug_suffix(rec)}"

    # На случай двух разных Kodik-записей с одинаковым titleEn (например,
    # фильмы с одинаковым названием) гарантируем уникальный slug/title.
    used_titles: dict[str, int] = {}
    for rec in grouped:
        base_title = rec["titleEn"]
        count = used_titles.get(base_title, 0) + 1
        used_titles[base_title] = count
        if count > 1:
            rec["titleEn"] = f"{base_title} #{count}"
            rec["slug"] = f"{rec['slug']}-{count}"

    return grouped


def fetch_ongoing(
    *,
    token: str | None = None,
    translation_id: int | str | None = None,
    limit: int | None = None,
    delay: float | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Все сериалы со статусом ongoing (с сезонами и ссылками на серии).

    Франшизы здесь НЕ группируются: у записи остаётся kodikId, по которому
    update-ongoing находит уже загруженный content. Фильтр по статусу
    дублируется на клиенте, поэтому лишние записи не попадут в результат,
    даже если Kodik проигнорирует серверный фильтр.
    """
    found: dict[str, dict[str, Any]] = {}
    for field in ONGOING_FILTER_FIELDS:
        print(f"Онгоинги: запрос с {field}=ongoing")
        for item in iter_items(
            token=token,
            translation_id=translation_id,
            limit=limit,
            delay=delay,
            max_pages=max_pages,
            extra_params={field: "ongoing"},
        ):
            rec = normalize_item(item)
            if rec.get("type") != "series" or not is_ongoing(rec.get("status")):
                continue
            key = str(rec.get("kodikId") or "")
            if key:
                found.setdefault(key, rec)
    return list(found.values())


def fetch_by_kodik_id(
    kodik_id: str,
    *,
    token: str | None = None,
) -> dict[str, Any] | None:
    """Одна запись по Kodik ID через /search (или None, если не найдена).

    Нужна, чтобы узнать актуальный статус тайтла, который пропал из списка
    онгоингов (обычно — сериал завершился).
    """
    token = token or require_env("KODIK_TOKEN")
    params = {
        "token": token,
        "id": kodik_id,
        "with_episodes": "true",
        "with_material_data": "true",
        "limit": 5,
    }
    payload = _request_url(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
    for item in payload.get("results") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(kodik_id):
            return normalize_item(item)
    return None


def fetch_by_shikimori_id(
    shikimori_id: str,
    *,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Все записи Kodik одного тайтла (по записи на озвучку) через /search?shikimori_id=…

    В отличие от /list с translation_id, один запрос возвращает сразу все озвучки тайтла —
    на этом построен `sync-dubs --by-title`. Озвучка записи — rec["translation"]["id"].
    Возвращаются только сериалы (type == "series").
    """
    token = token or require_env("KODIK_TOKEN")
    params = {
        "token": token,
        "shikimori_id": shikimori_id,
        "with_episodes": "true",
        "with_material_data": "true",
        "limit": 100,
    }
    payload = _request_url(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise RuntimeError("Kodik API: поле results имеет неожиданный тип")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        seen.add(item_id)
        rec = normalize_item(item)
        if rec.get("type") == "series":
            records.append(rec)
    return records
