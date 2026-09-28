import pytest

from kodik_pipeline.status import is_ongoing, normalize_status, status_column


@pytest.mark.parametrize("raw,expected", [
    ("ongoing", "ongoing"), ("Ongoing", "ongoing"), ("выходит", "ongoing"), ("онгоинг", "ongoing"),
    ("released", "released"), ("вышел", "released"),
    ("anons", "anons"), (None, None), ("", None), ("  ", None),
    ("какой-то новый статус", None),  # вне enum CMS -> не пишем
])
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


def test_is_ongoing():
    assert is_ongoing("ongoing")
    assert not is_ongoing("released")
    assert not is_ongoing(None)


def test_status_column_default_and_validation(monkeypatch):
    monkeypatch.delenv("RELEASE_STATUS_COLUMN", raising=False)
    assert status_column() == "release_status"
    monkeypatch.setenv("RELEASE_STATUS_COLUMN", "anime_status")
    assert status_column() == "anime_status"
    monkeypatch.setenv("RELEASE_STATUS_COLUMN", "x; DROP TABLE content")
    with pytest.raises(SystemExit):
        status_column()
