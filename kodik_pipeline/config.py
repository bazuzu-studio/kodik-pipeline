"""Конфигурация пайплайна из окружения и .env."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Ошибка: не задана переменная окружения {name}", file=sys.stderr)
        raise SystemExit(1)
    return value


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise SystemExit(f"Ошибка: {name} должно быть целым числом, получено {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise SystemExit(f"Ошибка: {name} должно быть >= {minimum}")
    if maximum is not None and value > maximum:
        raise SystemExit(f"Ошибка: {name} должно быть <= {maximum}")
    return value


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise SystemExit(f"Ошибка: {name} должно быть числом, получено {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise SystemExit(f"Ошибка: {name} должно быть >= {minimum}")
    return value


def database_url() -> str:
    return require_env("DATABASE_URL")


def kodik_token() -> str:
    return require_env("KODIK_TOKEN")


def kodik_translation_id() -> str:
    return os.environ.get("KODIK_TRANSLATION_ID", "609").strip() or "609"


def kodik_limit() -> int:
    return env_int("KODIK_LIMIT", 100, minimum=1, maximum=1000)


def kodik_delay() -> float:
    return env_float("KODIK_DELAY", 0.25, minimum=0)


def kodik_timeout() -> int:
    return env_int("KODIK_TIMEOUT", 60, minimum=1, maximum=300)


def kodik_retries() -> int:
    return env_int("KODIK_RETRIES", 5, minimum=1, maximum=10)


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    public_url: str
    region: str = "us-east-1"

    @classmethod
    def from_env(cls) -> "S3Config":
        return cls(
            endpoint=require_env("S3_ENDPOINT").rstrip("/"),
            access_key=require_env("S3_ACCESS_KEY_ID"),
            secret_key=require_env("S3_SECRET_ACCESS_KEY"),
            bucket=require_env("S3_BUCKET"),
            public_url=require_env("S3_PUBLIC_URL").rstrip("/"),
            region=os.environ.get("S3_REGION", "").strip() or "us-east-1",
        )
