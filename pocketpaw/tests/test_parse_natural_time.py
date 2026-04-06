"""Tests for parse_natural_time function in scheduler module.

This test suite covers:
- Natural time parsing with and without 'in' prefix
- Different time units (minutes, hours, days, seconds)
- Abbreviations (min, hr, sec)
- Edge cases and boundary conditions
"""

from datetime import datetime, timedelta

from pocketpaw.scheduler import parse_natural_time


class TestParseNaturalTimeWithoutIn:
    """Test parsing time expressions without 'in' prefix (new feature)."""

    def test_parse_minutes_without_in(self):
        """Test parsing '5 minutes' without 'in' prefix."""
        result = parse_natural_time("5 minutes")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        # Allow 1 second tolerance for test execution time
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_hours_without_in(self):
        """Test parsing '2 hours' without 'in' prefix."""
        result = parse_natural_time("2 hours")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(hours=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_days_without_in(self):
        """Test parsing '3 days' without 'in' prefix."""
        result = parse_natural_time("3 days")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(days=3)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_seconds_without_in(self):
        """Test parsing '30 seconds' without 'in' prefix."""
        result = parse_natural_time("30 seconds")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(seconds=30)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_weeks_without_in(self):
        """Test parsing '2 weeks' without 'in' prefix."""
        result = parse_natural_time("2 weeks")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=2)
        assert abs((result - expected).total_seconds()) < 1


class TestParseNaturalTimeWithIn:
    """Test backward compatibility with 'in' prefix."""

    def test_parse_minutes_with_in(self):
        """Test parsing 'in 5 minutes' with 'in' prefix (backward compat)."""
        result = parse_natural_time("in 5 minutes")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_hours_with_in(self):
        """Test parsing 'in 2 hours' with 'in' prefix."""
        result = parse_natural_time("in 2 hours")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(hours=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_days_with_in(self):
        """Test parsing 'in 3 days' with 'in' prefix."""
        result = parse_natural_time("in 3 days")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(days=3)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_seconds_with_in(self):
        """Test parsing 'in 30 seconds' with 'in' prefix."""
        result = parse_natural_time("in 30 seconds")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(seconds=30)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_weeks_with_in(self):
        """Test parsing 'in 2 weeks' with 'in' prefix."""
        result = parse_natural_time("in 2 weeks")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=2)
        assert abs((result - expected).total_seconds()) < 1


class TestParseNaturalTimeAbbreviations:
    """Test parsing with abbreviated time units."""

    def test_parse_min_abbreviation(self):
        """Test parsing '10 min' abbreviation."""
        result = parse_natural_time("10 min")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=10)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_hr_abbreviation(self):
        """Test parsing '2 hr' abbreviation."""
        result = parse_natural_time("2 hr")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(hours=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_sec_abbreviation(self):
        """Test parsing '45 sec' abbreviation."""
        result = parse_natural_time("45 sec")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(seconds=45)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_min_with_in(self):
        """Test parsing 'in 10 min' with abbreviation."""
        result = parse_natural_time("in 10 min")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=10)
        assert abs((result - expected).total_seconds()) < 1


class TestParseNaturalTimeSingularPlural:
    """Test parsing both singular and plural forms."""

    def test_parse_singular_minute(self):
        """Test parsing '1 minute' (singular)."""
        result = parse_natural_time("1 minute")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=1)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_singular_hour(self):
        """Test parsing '1 hour' (singular)."""
        result = parse_natural_time("1 hour")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(hours=1)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_singular_day(self):
        """Test parsing '1 day' (singular)."""
        result = parse_natural_time("1 day")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(days=1)
        assert abs((result - expected).total_seconds()) < 1


class TestParseNaturalTimeEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_parse_with_extra_whitespace(self):
        """Test parsing with extra whitespace."""
        result = parse_natural_time("  5   minutes  ")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_mixed_case(self):
        """Test parsing with mixed case (should be case-insensitive)."""
        result = parse_natural_time("5 MINUTES")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_zero_value(self):
        """Test parsing '0 minutes' (edge case)."""
        result = parse_natural_time("0 minutes")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_large_value(self):
        """Test parsing large values like '1000 days'."""
        result = parse_natural_time("1000 days")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(days=1000)
        assert abs((result - expected).total_seconds()) < 1

    def test_word_boundary_prevents_false_match(self):
        """Test that word boundary prevents false matches like '3 dayplanners'."""
        result = parse_natural_time("3 dayplanners")
        # Should NOT match "3 day" from "3 dayplanners"
        # The function might return None or match something else
        # The key is that it shouldn't match "3 days"
        if result is not None:
            # If it parsed something, make sure it's not a 3-day offset
            now = datetime.now(result.tzinfo)
            three_days = now + timedelta(days=3)
            # The result should NOT be approximately 3 days from now
            assert abs((result - three_days).total_seconds()) > 3600  # More than 1 hour off

    def test_parse_embedded_in_sentence(self):
        """Test parsing time from within a sentence."""
        result = parse_natural_time("remind me in 5 minutes to call mom")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_without_in_embedded(self):
        """Test parsing time without 'in' from within a sentence."""
        result = parse_natural_time("remind me 5 minutes to call")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(minutes=5)
        assert abs((result - expected).total_seconds()) < 1


class TestParseNaturalTimeInvalidInputs:
    """Test handling of invalid or unparseable inputs."""

    def test_parse_invalid_text(self):
        """Test parsing text with no time expression."""
        result = parse_natural_time("hello world")
        assert result is None

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        result = parse_natural_time("")
        assert result is None

    def test_parse_only_number(self):
        """Test parsing just a number without unit."""
        result = parse_natural_time("5")
        assert result is None

    def test_parse_only_unit(self):
        """Test parsing just a unit without number."""
        result = parse_natural_time("minutes")
        assert result is None


class TestParseNaturalTimeWeeks:
    """Tests for week/weeks support."""

    def test_parse_1_week_with_in(self):
        """Test parsing 'in 1 week' with in prefix."""
        result = parse_natural_time("in 1 week")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=1)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_2_weeks_with_in(self):
        """Test parsing 'in 2 weeks' with in prefix."""
        result = parse_natural_time("in 2 weeks")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_3_weeks_without_in(self):
        """Test parsing '3 weeks' without in prefix."""
        result = parse_natural_time("3 weeks")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=3)
        assert abs((result - expected).total_seconds()) < 1

    def test_parse_1_week_singular_without_in(self):
        """Test parsing '1 week' singular without in prefix."""
        result = parse_natural_time("1 week")
        assert result is not None
        now = datetime.now(result.tzinfo)
        expected = now + timedelta(weeks=1)
        assert abs((result - expected).total_seconds()) < 1
