# Tests for security/pii.py — PII detection and masking.
# Created: 2026-02-16

import pytest

from pocketpaw.security.pii import (
    PIIAction,
    PIIScanner,
    PIIType,
    get_pii_scanner,
    reset_pii_scanner,
)


@pytest.fixture
def scanner():
    return PIIScanner(default_action=PIIAction.MASK)


# ---------------------------------------------------------------------------
# Detection per PII type
# ---------------------------------------------------------------------------
class TestSSNDetection:
    def test_ssn_dashed(self, scanner):
        result = scanner.scan("My SSN is 123-45-6789")
        assert result.has_pii
        assert PIIType.SSN in result.pii_types_found
        assert "123-45-6789" not in result.sanitized_text
        assert "[REDACTED-SSN]" in result.sanitized_text

    def test_ssn_preserves_surrounding_text(self, scanner):
        result = scanner.scan("Before 123-45-6789 after")
        assert result.sanitized_text == "Before [REDACTED-SSN] after"


class TestEmailDetection:
    def test_simple_email(self, scanner):
        result = scanner.scan("Contact me at john@example.com please")
        assert result.has_pii
        assert PIIType.EMAIL in result.pii_types_found
        assert "[REDACTED-EMAIL]" in result.sanitized_text
        assert "john@example.com" not in result.sanitized_text

    def test_email_with_plus(self, scanner):
        result = scanner.scan("Send to user+tag@gmail.com")
        assert result.has_pii
        assert PIIType.EMAIL in result.pii_types_found


class TestPhoneDetection:
    def test_us_phone_parentheses(self, scanner):
        result = scanner.scan("Call me at (555) 123-4567")
        assert result.has_pii
        assert PIIType.PHONE in result.pii_types_found

    def test_us_phone_dashed(self, scanner):
        result = scanner.scan("Phone: 555-123-4567")
        assert result.has_pii
        assert PIIType.PHONE in result.pii_types_found


class TestCreditCardDetection:
    def test_visa(self, scanner):
        result = scanner.scan("Card: 4111-1111-1111-1111")
        assert result.has_pii
        assert PIIType.CREDIT_CARD in result.pii_types_found
        assert "[REDACTED-CREDIT_CARD]" in result.sanitized_text

    def test_mastercard(self, scanner):
        result = scanner.scan("MC: 5500 0000 0000 0004")
        assert result.has_pii
        assert PIIType.CREDIT_CARD in result.pii_types_found


class TestIPAddressDetection:
    def test_ipv4(self, scanner):
        result = scanner.scan("Server at 192.168.1.100")
        assert result.has_pii
        assert PIIType.IP_ADDRESS in result.pii_types_found
        assert "[REDACTED-IP_ADDRESS]" in result.sanitized_text


class TestDateOfBirthDetection:
    def test_dob_with_keyword(self, scanner):
        result = scanner.scan("Born on 03/15/1990")
        assert result.has_pii
        assert PIIType.DATE_OF_BIRTH in result.pii_types_found

    def test_date_without_keyword_no_match(self, scanner):
        """Bare dates without context keywords should not match."""
        result = scanner.scan("The report was filed on 03/15/1990")
        assert PIIType.DATE_OF_BIRTH not in result.pii_types_found


# ---------------------------------------------------------------------------
# Safe content
# ---------------------------------------------------------------------------
class TestSafeContent:
    def test_normal_message(self, scanner):
        result = scanner.scan("How do I sort a list in Python?")
        assert not result.has_pii
        assert result.sanitized_text == "How do I sort a list in Python?"

    def test_empty_string(self, scanner):
        result = scanner.scan("")
        assert not result.has_pii

    def test_none_like(self, scanner):
        result = scanner.scan("")
        assert result.sanitized_text == ""


# ---------------------------------------------------------------------------
# Action modes
# ---------------------------------------------------------------------------
class TestActions:
    def test_log_preserves_text(self):
        s = PIIScanner(default_action=PIIAction.LOG)
        result = s.scan("Email: test@example.com")
        assert result.has_pii
        assert result.sanitized_text == "Email: test@example.com"

    def test_mask_replaces(self):
        s = PIIScanner(default_action=PIIAction.MASK)
        result = s.scan("Email: test@example.com")
        assert "[REDACTED-EMAIL]" in result.sanitized_text
        assert "test@example.com" not in result.sanitized_text

    def test_hash_produces_partial(self):
        s = PIIScanner(default_action=PIIAction.HASH)
        result = s.scan("Email: test@example.com")
        assert "[PII-EMAIL:" in result.sanitized_text
        assert "test@example.com" not in result.sanitized_text

    def test_per_type_override(self):
        s = PIIScanner(
            default_action=PIIAction.MASK,
            type_actions={PIIType.EMAIL: PIIAction.HASH},
        )
        result = s.scan("SSN: 123-45-6789, Email: test@example.com")
        assert "[REDACTED-SSN]" in result.sanitized_text
        assert "[PII-EMAIL:" in result.sanitized_text


# ---------------------------------------------------------------------------
# Multiple PII in one message
# ---------------------------------------------------------------------------
class TestMultiplePII:
    def test_multiple_types(self, scanner):
        text = "SSN: 123-45-6789, Email: john@test.com, Phone: 555-123-4567"
        result = scanner.scan(text)
        assert len(result.pii_types_found) >= 3
        assert PIIType.SSN in result.pii_types_found
        assert PIIType.EMAIL in result.pii_types_found
        assert PIIType.PHONE in result.pii_types_found

    def test_all_replaced(self, scanner):
        text = "SSN: 123-45-6789, Email: john@test.com"
        result = scanner.scan(text)
        assert "123-45-6789" not in result.sanitized_text
        assert "john@test.com" not in result.sanitized_text


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
class TestSingleton:
    def test_singleton_returns_same_instance(self):
        reset_pii_scanner()
        s1 = get_pii_scanner()
        s2 = get_pii_scanner()
        assert s1 is s2
        reset_pii_scanner()

    def test_reset_creates_new_instance(self):
        reset_pii_scanner()
        s1 = get_pii_scanner()
        reset_pii_scanner()
        s2 = get_pii_scanner()
        assert s1 is not s2
        reset_pii_scanner()
