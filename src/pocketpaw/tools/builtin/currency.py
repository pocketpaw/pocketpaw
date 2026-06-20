# Currency conversion tool — keyless FX via Frankfurter (ECB reference rates).
# Created: 2026-05-31 — zero-config builtin tool (no API key required).
#
# Frankfurter (https://frankfurter.dev) is a free, keyless FX API backed by
# European Central Bank reference rates. We hit the canonical host
# https://api.frankfurter.dev/v1/latest to avoid the legacy host's redirect.
# Returns clean structured text the agent renders; no inline ui-spec plumbing
# (the inline-primitive layer is owned by a separate RFC).

import logging
from typing import Any

import httpx

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_LATEST_URL = "https://api.frankfurter.dev/v1/latest"

# Display symbols for common currencies (cosmetic only — Frankfurter covers ~30).
_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "C$",
    "CHF": "CHF",
    "KRW": "₩",
    "SGD": "S$",
    "HKD": "HK$",
    "NZD": "NZ$",
    "SEK": "kr",
    "BRL": "R$",
    "ZAR": "R",
}

# Currencies conventionally shown without decimal places.
_NO_DECIMAL = frozenset({"JPY", "KRW", "HUF", "ISK"})


def _fmt_amount(amount: float, code: str) -> str:
    symbol = _SYMBOLS.get(code, "")
    decimals = 0 if code in _NO_DECIMAL else 2
    return f"{symbol}{amount:,.{decimals}f} {code}"


class CurrencyTool(BaseTool):
    """Convert an amount between two currencies using live ECB rates.

    Uses Frankfurter (free, no API key). Returns the converted amount, the
    exchange rate, and the rate date.
    """

    @property
    def name(self) -> str:
        return "currency"

    @property
    def description(self) -> str:
        return (
            "Convert an amount between two currencies using live exchange rates. "
            "No API key required. Supports ~30 major currencies (USD, EUR, GBP, JPY, "
            "CNY, INR, AUD, CAD, CHF, and more). Use for currency conversion, travel "
            "budgets, or international pricing."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "description": "The amount to convert (must be positive).",
                },
                "from_currency": {
                    "type": "string",
                    "description": "Source 3-letter currency code, e.g. USD, EUR, GBP.",
                },
                "to_currency": {
                    "type": "string",
                    "description": "Target 3-letter currency code, e.g. JPY, INR, CNY.",
                },
            },
            "required": ["amount", "from_currency", "to_currency"],
        }

    async def execute(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
    ) -> str:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return self._error("Amount must be a number.")
        if amount <= 0:
            return self._error("Amount must be a positive number.")

        from_code = (from_currency or "").strip().upper()
        to_code = (to_currency or "").strip().upper()
        if len(from_code) != 3 or len(to_code) != 3:
            return self._error("Currency codes must be 3 letters (e.g. USD, EUR, JPY).")

        if from_code == to_code:
            return self._success(
                f"{_fmt_amount(amount, from_code)} = {_fmt_amount(amount, to_code)} "
                "(same currency, rate 1.0000)."
            )

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    _LATEST_URL,
                    params={"base": from_code, "symbols": to_code},
                )
        except httpx.TimeoutException:
            return self._error("Currency request timed out. Please try again.")
        except Exception as e:
            return self._error(f"Currency lookup failed: {e}")

        # Frankfurter returns 404 for an unknown base currency.
        if resp.status_code == 404:
            return self._error(f"Unknown or unsupported currency code: {from_code}.")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return self._error(f"Currency API error: {e.response.status_code}")

        try:
            data = resp.json()
        except Exception:
            return self._error("Currency API returned an unreadable response.")

        rates = data.get("rates") or {}
        rate = rates.get(to_code)
        if rate is None:
            return self._error(
                f"No exchange rate available for {from_code} -> {to_code}. "
                "Check the target currency code."
            )

        converted = amount * rate
        date = data.get("date", "")
        return (
            f"{_fmt_amount(amount, from_code)} = {_fmt_amount(converted, to_code)}\n"
            f"Rate: 1 {from_code} = {rate:.4f} {to_code}" + (f" (as of {date})" if date else "")
        )
