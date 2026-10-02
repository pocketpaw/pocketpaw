# tests/test_money.py — ISO 4217 minor-unit helpers and the exponent-table parity.
#
# The table must equal tests/fixtures/currency_exponents.json exactly: paw-bar and
# paw-enterprise compare their copies against the same fixture.

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pocketpaw.money import (
    CURRENCY_EXPONENTS,
    client_sends_minor_units,
    convert_legacy_minor,
    exponent,
    format_minor,
    from_minor,
    normalize_currency,
    to_minor,
)

FIXTURE = Path(__file__).parent / "fixtures" / "currency_exponents.json"


def test_table_matches_fixture() -> None:
    assert CURRENCY_EXPONENTS == json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [("USD", 2), ("usd", 2), ("JPY", 0), ("jpy ", 0), ("KWD", 3), ("CLF", 4), ("ZZZ", 2), ("", 2)],
)
def test_exponent(code: str, expected: int) -> None:
    assert exponent(code) == expected


def test_normalize_currency() -> None:
    assert normalize_currency(" eur ") == "EUR"
    for bad in ("", "US", "USDX", "U$D", None, 840):
        with pytest.raises(ValueError):
            normalize_currency(bad)


@pytest.mark.parametrize(
    ("amount", "code", "expected"),
    [
        ("3.50", "USD", 350),
        ("1500", "JPY", 1500),
        ("1499.5", "JPY", 1500),
        ("1.25", "KWD", 1250),
        ("0.005", "USD", 1),
        (Decimal("19.99"), "EUR", 1999),
        (0.1, "USD", 10),
        (7, "KRW", 7),
    ],
)
def test_to_minor(amount: object, code: str, expected: int) -> None:
    assert to_minor(amount, code) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "Infinity"])
def test_to_minor_rejects_non_numbers(bad: str) -> None:
    with pytest.raises(ValueError):
        to_minor(bad, "USD")


@pytest.mark.parametrize("huge", ["1e30", "1" * 30, Decimal("9e40")])
def test_to_minor_out_of_range_is_a_value_error(huge: object) -> None:
    with pytest.raises(ValueError):  # not decimal.InvalidOperation
        to_minor(huge, "USD")  # type: ignore[arg-type]


def test_from_minor_out_of_range_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        from_minor(10**40, "USD")


def test_from_minor_round_trips() -> None:
    assert from_minor(350, "USD") == Decimal("3.50")
    assert from_minor(1500, "JPY") == Decimal("1500")
    assert from_minor(1250, "KWD") == Decimal("1.250")
    for code in ("USD", "JPY", "KWD", "CLF"):
        assert to_minor(from_minor(123456, code), code) == 123456


@pytest.mark.parametrize(
    ("amount", "code", "expected"),
    [
        (350, "USD", "$3.50"),
        (1500, "JPY", "¥1,500"),
        (1250, "KWD", "1.250 KWD"),
        (123456, "EUR", "€1,234.56"),
        (999, "GBP", "£9.99"),
        (50000, "INR", "₹500.00"),
        (1000, "CHF", "10.00 CHF"),
        (350, "", "3.50"),
        (-350, "USD", "-$3.50"),
        ("350", "usd", "$3.50"),
    ],
)
def test_format_minor(amount: object, code: str, expected: str) -> None:
    assert format_minor(amount, code) == expected


def test_format_minor_non_integer_is_blank() -> None:
    assert format_minor(None, "USD") == ""
    assert format_minor("abc", "USD") == ""


@pytest.mark.parametrize("absurd", [10**40, float("inf"), float("nan")])
def test_format_minor_out_of_range_is_blank_not_an_error(absurd: object) -> None:
    assert format_minor(absurd, "USD") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("iso4217", True), (" ISO4217 ", True), (None, False), ("", False), ("legacy", False)],
)
def test_client_sends_minor_units(value: object, expected: bool) -> None:
    assert client_sends_minor_units(value) is expected


@pytest.mark.parametrize(
    ("old", "code", "new"),
    [(150000, "JPY", 1500), (150050, "JPY", 1501), (125, "KWD", 1250), (350, "USD", 350)],
)
def test_convert_legacy_minor(old: int, code: str, new: int) -> None:
    assert convert_legacy_minor(old, code) == new
