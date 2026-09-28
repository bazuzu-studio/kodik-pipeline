import json
import os

import pytest

from kodik_pipeline.json_io import load_json, save_json


def test_save_json_is_atomic_and_creates_dirs(tmp_path):
    path = tmp_path / "nested" / "k.json"
    save_json(str(path), [{"a": "я"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"a": "я"}]
    assert os.listdir(path.parent) == ["k.json"]  # tmp-файлов не осталось


def test_save_json_failure_keeps_old_file(tmp_path):
    path = tmp_path / "k.json"
    save_json(str(path), [1])
    with pytest.raises(TypeError):
        save_json(str(path), [object()])
    assert load_json(str(path)) == [1]
    assert os.listdir(tmp_path) == ["k.json"]
