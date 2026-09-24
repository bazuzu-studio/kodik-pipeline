from kodik_pipeline.text_utils import slugify, to_float


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_supports_cyrillic():
    assert slugify("Атака титанов 2") == "атака-титанов-2"


def test_slugify_empty_or_all_stripped_returns_unknown():
    assert slugify("") == "unknown"
    assert slugify("!!!") == "unknown"


def test_to_float_none_and_zero():
    assert to_float(None) is None
    assert to_float(0) is None
    assert to_float(-1.5) is None


def test_to_float_rounds_to_one_decimal():
    assert to_float(7.849) == 7.8


def test_to_float_parses_comma_decimal_string():
    assert to_float("7,8") == 7.8


def test_to_float_invalid_string_returns_none():
    assert to_float("not-a-number") is None
