"""
Шаг 3: скачивание постеров (posterUrl из data/kodik.json) и загрузка в
S3/MinIO, с привязкой media-записи к content / _content_v в Postgres.

Установка: pip install -r requirements.txt
Запуск: python pipeline.py posters data/kodik.json
"""

from __future__ import annotations

import argparse
import io
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from botocore.exceptions import ClientError

from . import s3_client
from .config import S3Config, database_url
from .db import table_columns, transaction
from .json_io import load_json
from .text_utils import slugify

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

POSTER_URL_FIELD = "posterUrl"
POSTER_BASE_URL = ""  # если posterUrl приходит относительным путём
S3_PREFIX = ""  # опциональный префикс ключа в бакете
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def download_image(url: str, retries: int = 2, timeout: int = 15, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[bytes, str, int, int]:
    if POSTER_BASE_URL and url.startswith("/"):
        url = POSTER_BASE_URL.rstrip("/") + url

    scheme = urlparse(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Неподдерживаемая схема URL постера: {scheme or 'отсутствует'}")
    if max_bytes <= 0:
        raise ValueError("max_bytes должен быть > 0")
    if retries < 0:
        raise ValueError("retries не может быть отрицательным")
    if timeout <= 0:
        raise ValueError("timeout должен быть > 0")

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if not content_type.startswith("image/"):
                    raise ValueError(f"Не картинка: Content-Type={content_type or "не указан"}")
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError(f"Изображение слишком большое: {content_length} байт > {max_bytes}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(min(1024 * 256, max_bytes - total + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Изображение слишком большое: > {max_bytes} байт")
                data = b"".join(chunks)

            if HAS_PIL:
                img = Image.open(io.BytesIO(data))
                width, height = img.size
                fmt = (img.format or "JPEG").lower()
            else:
                width, height = 0, 0
                if data[:2] == b"\xff\xd8":
                    fmt = "jpeg"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    fmt = "png"
                elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    fmt = "webp"
                else:
                    fmt = "jpeg"

            return data, f"image/{fmt}", width, height
        except Exception as e:  # noqa: BLE001 - собираем любую ошибку сети/декодирования
            last_err = e
            if attempt < retries:
                time.sleep(2)

    assert last_err is not None
    raise last_err


def build_media_insert(media_cols: set[str], values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Собирает INSERT только из полей, которые реально есть в таблице
    media (схема может отличаться между проектами Payload)."""
    cols = [c for c in values if c in media_cols]
    vals = {c: values[c] for c in cols}
    col_names = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    return f"INSERT INTO media ({col_names}) VALUES ({placeholders}) RETURNING id;", vals


def process_record(cur, rec: dict[str, Any], s3, cfg: S3Config, media_cols: set[str], poster_cols: list[str], *, timeout: int = 15, retries: int = 2, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Обрабатывает одну запись data/kodik.json. Возвращает статус:
    'ok' | 'exists' | 'no_url' | 'no_content' | 'error'."""
    poster_url = rec.get(POSTER_URL_FIELD)
    title_en = rec.get("titleEn")
    title_ru = rec.get("titleRu")
    slug = rec.get("slug")

    if not poster_url:
        return "no_url"
    if not title_en or not slug:
        return "no_content"

    try:
        data, mime_type, width, height = download_image(poster_url, retries=retries, timeout=timeout, max_bytes=max_bytes)
        ext = "jpg" if mime_type == "image/jpeg" else mime_type.split("/")[1]
        filename = f"{slugify(slug)}.{ext}"
        object_key = f"{S3_PREFIX}/{filename}" if S3_PREFIX else filename
    except Exception as e:  # noqa: BLE001
        print(f"  [ERR] {title_en}: скачивание — {e}")
        return "error"

    try:
        s3.put_object(Bucket=cfg.bucket, Key=object_key, Body=data, ContentType=mime_type)
    except ClientError as e:
        print(f"  [ERR] {title_en}: S3 — {e}")
        return "error"

    cur.execute("SAVEPOINT sp1")
    try:
        cur.execute("SELECT id FROM content WHERE title_en = %(t)s", {"t": title_en})
        row = cur.fetchone()
        if not row:
            cur.execute("ROLLBACK TO SAVEPOINT sp1")
            print(f"  [SKIP] {title_en}: нет в БД")
            return "no_content"
        content_id = row[0]

        cur.execute("SELECT id FROM media WHERE filename = %(f)s", {"f": filename})
        row = cur.fetchone()

        media_exists = row is not None
        if media_exists:
            media_id = row[0]
            print(f"  [EXISTS] {title_en}: media.id={media_id}")
        else:
            alt_text = title_ru or title_en or f"Постер {slugify(slug)}"
            if len(alt_text) > 255:
                alt_text = alt_text[:252] + "..."

            insert_values = {
                "filename": filename,
                "mime_type": mime_type,
                "filesize": len(data),
                "width": width or None,
                "height": height or None,
                "url": f"{cfg.public_url}/{object_key}",
                "alt": alt_text,
            }
            sql, params = build_media_insert(media_cols, insert_values)
            cur.execute(sql, params)
            media_id = cur.fetchone()[0]
            print(f"  [OK] {title_en}: media.id={media_id}, {filename} ({width}x{height})")

        if poster_cols:
            poster_col = "poster_id" if "poster_id" in poster_cols else poster_cols[0]
            cur.execute(
                f"UPDATE content SET {poster_col} = %(m)s, updated_at = now() WHERE id = %(c)s",
                {"m": media_id, "c": content_id},
            )

        cur.execute(
            """
            UPDATE _content_v
            SET version_poster_id = %(m)s, version_updated_at = now(), updated_at = now()
            WHERE parent_id = %(c)s AND latest = true
            """,
            {"m": media_id, "c": content_id},
        )

        cur.execute("RELEASE SAVEPOINT sp1")
        return "exists" if media_exists else "ok"
    except Exception as e:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        print(f"  [ERR] {title_en}: БД — {e}")
        return "error"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Загружает постеры в S3/MinIO и привязывает их к content")
    parser.add_argument("merged", help="JSON из Kodik API")
    parser.add_argument("--timeout", type=int, default=15, help="таймаут скачивания постера, секунд")
    parser.add_argument("--retries", type=int, default=2, help="количество повторов скачивания")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="максимальный размер одного изображения")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = S3Config.from_env()
    s3 = s3_client.build_client(cfg)

    print("\nПроверка подключения к S3...")
    try:
        s3.head_bucket(Bucket=cfg.bucket)
        print("OK: бакет доступен\n")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise SystemExit(f"ОШИБКА подключения к S3: {code} — {e}")

    records = load_json(args.merged)

    counts = {"ok": 0, "exists": 0, "no_url": 0, "no_content": 0, "error": 0}

    with transaction(database_url()) as conn:
        with conn.cursor() as cur:
            media_cols = table_columns(cur, "media")
            print(f"Колонки media: {sorted(media_cols)}")

            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'content' AND table_schema = 'public'
                  AND column_name LIKE '%poster%'
                """
            )
            poster_cols = [row[0] for row in cur.fetchall()]
            if not poster_cols:
                print("ВНИМАНИЕ: в таблице content нет колонки poster. poster_id не будет обновлён.")
            else:
                print(f"Колонки постера в content: {poster_cols}")

            for rec in records:
                status = process_record(cur, rec, s3, cfg, media_cols, poster_cols, timeout=args.timeout, retries=args.retries, max_bytes=args.max_bytes)
                counts[status] = counts.get(status, 0) + 1

    uploaded = counts["ok"] + counts["exists"]
    print(
        f"\nГотово: загружено={uploaded}, "
        f"без URL={counts['no_url']}, "
        f"без content={counts['no_content']}, "
        f"ошибок={counts['error']}"
    )


if __name__ == "__main__":
    main()
