import pytest

import pipeline


def test_update_ongoing_parser_defaults():
    args = pipeline.build_parser().parse_args(["update-ongoing"])
    assert args.func is pipeline.update_ongoing_command
    assert args.dry_run is False and args.no_recheck is False
    assert args.recheck_limit == 200


def test_update_ongoing_parser_flags():
    args = pipeline.build_parser().parse_args(
        ["update-ongoing", "--dry-run", "--no-recheck", "--max-pages", "2"])
    assert args.dry_run and args.no_recheck and args.max_pages == 2


def test_fetch_refuses_to_overwrite_with_empty_result(monkeypatch, tmp_path):
    monkeypatch.setattr("kodik_pipeline.api.fetch_normalized", lambda **_: [])
    monkeypatch.setenv("KODIK_TOKEN", "x")
    out = tmp_path / "k.json"
    out.write_text("[1]")
    with pytest.raises(SystemExit):
        pipeline.main(["fetch", "--out", str(out)])
    assert out.read_text() == "[1]"


def test_revalidate_command_registered():
    args = pipeline.build_parser().parse_args(["revalidate"])
    assert args.func is pipeline.revalidate_command
