"""Общая фабрика S3/MinIO-клиента (использовалась только в upload_posters.py,
а test_s3.py дублировал её вручную с чуть другими параметрами)."""

from __future__ import annotations

from urllib.parse import urlparse

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import S3Config


def build_client(cfg: S3Config) -> BaseClient:
    parsed = urlparse(cfg.endpoint)
    endpoint_url = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else cfg.endpoint
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def check_connection(cfg: S3Config) -> None:
    """Проверяет доступность бакета (test_s3.py) и печатает список бакетов,
    видимых с этими ключами — полезно для диагностики опечаток в имени бакета."""
    client = build_client(cfg)

    print("Проверка подключения к S3...")
    try:
        client.head_bucket(Bucket=cfg.bucket)
        print(f"OK: бакет '{cfg.bucket}' доступен")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        print(f"ОШИБКА подключения к бакету '{cfg.bucket}': {code} — {e}")

    try:
        resp = client.list_buckets()
        print("\nБакеты, видимые с текущими ключами:")
        for b in resp["Buckets"]:
            print(f"  {b['Name']}")
    except ClientError as e:
        print(f"Не удалось получить список бакетов: {e}")
