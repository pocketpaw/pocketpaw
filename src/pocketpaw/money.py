# src/pocketpaw/money.py — ISO 4217 minor units, the one meaning of every amount.
#
# Every stored or wire amount (``price_cents``, ``value_cents``, ``total_cents``,
# ``amount_cents``) is an integer count of the ISO 4217 MINOR unit of its
# currency; the ``_cents`` names are historical. JPY has exponent 0 (1500 is
# ¥1,500), USD 2 (350 is $3.50), KWD 3 (1250 is 1.250 KWD).
#
# ``CURRENCY_EXPONENTS`` lists only the ISO exceptions; every other code is 2.
# The same table ships as ``tests/fixtures/currency_exponents.json`` and is
# copied into paw-bar ``lib/money.ts`` and paw-enterprise
# ``core/shared/money.ts``; a parity test in each repo compares against the
# fixture, so edit all copies together. We deliberately do NOT use CLDR (Intl /
# Babel) digits: they differ from ISO for some codes (e.g. IQD) and server and
# clients must agree exactly. An unknown but well-formed 3-letter code is
# accepted with exponent 2.

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# fmt: off
CURRENCY_EXPONENTS: dict[str, int] = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}
# fmt: on

DEFAULT_EXPONENT = 2

_CODE_RE = re.compile(r"[A-Z]{3}")

# Prefix symbols for the prompt. Anything else renders as "<amount> <CODE>",
# which is unambiguous and never wrong.
_SYMBOLS: dict[str, str] = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}


def normalize_currency(code: object) -> str:
    """Upper-cased, stripped 3-letter code, or ``ValueError``."""
    if not isinstance(code, str):
        raise ValueError(f"currency must be a 3-letter code, got {code!r}")
    norm = code.strip().upper()
    if not _CODE_RE.fullmatch(norm):
        raise ValueError(f"currency must be a 3-letter code, got {code!r}")
    return norm


def exponent(code: object) -> int:
    """ISO 4217 minor-unit exponent; 2 for unknown or malformed codes."""
    if not isinstance(code, str):
        return DEFAULT_EXPONENT
    return CURRENCY_EXPONENTS.get(code.strip().upper(), DEFAULT_EXPONENT)


def to_minor(amount: str | Decimal | int | float, code: object) -> int:
    """Major amount (``"3.50"``) → minor units for ``code``, rounded half-up.

    Floats go through ``str`` first so ``0.1`` stays 0.1. Raises ``ValueError``
    on a non-numeric or non-finite amount.
    """
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal amount: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a finite amount: {amount!r}")
    scaled = value.scaleb(exponent(code))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(amount: int, code: object) -> Decimal:
    """Minor units → exact major ``Decimal`` with the currency's exponent places."""
    e = exponent(code)
    return Decimal(int(amount)).scaleb(-e).quantize(Decimal(1).scaleb(-e))


def format_minor(amount: object, code: object) -> str:
    """Human amount for prompts: ``$3.50``, ``¥1,500``, ``1.250 KWD``.

    Returns ``""`` when ``amount`` is not an integer-like value. A blank or
    malformed code renders the bare number with two decimals.
    """
    try:
        minor = int(amount)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return ""
    try:
        norm = normalize_currency(code)
    except ValueError:
        norm = ""
    e = exponent(norm)
    major = from_minor(minor, norm)
    number = f"{abs(major):,.{e}f}"
    sign = "-" if minor < 0 else ""
    symbol = _SYMBOLS.get(norm)
    if symbol:
        return f"{sign}{symbol}{number}"
    return f"{sign}{number} {norm}".rstrip()


def convert_legacy_minor(amount: int, code: object) -> int:
    """Re-express an amount stored under the old "major × 100" rule.

    ``round(old × 10^(e−2))``: ÷100 for exponent 0, ×10 for exponent 3, unchanged
    for 2. Used only by the one-shot data migrations.
    """
    e = exponent(code)
    if e == DEFAULT_EXPONENT:
        return int(amount)
    scaled = Decimal(int(amount)).scaleb(e - DEFAULT_EXPONENT)
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
