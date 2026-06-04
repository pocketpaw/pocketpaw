# Tests for the zero-config capability tools: weather, wiki, currency.
# Created: 2026-05-31
#
# These tools call free, keyless public APIs. All external HTTP is mocked —
# no live network. Each tool gets a happy-path test plus an error/timeout path.

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketpaw.tools.builtin.currency import CurrencyTool
from pocketpaw.tools.builtin.weather import WeatherTool
from pocketpaw.tools.builtin.wiki import WikiTool


def _mock_client(get_side_effect):
    """Build a mock httpx.AsyncClient whose .get() is driven by *get_side_effect*.

    *get_side_effect* may be a list (one response per sequential .get call) or a
    single response / exception. Returns the patch target's mock client.
    """
    client = AsyncMock()
    if isinstance(get_side_effect, list):
        client.get.side_effect = get_side_effect
    elif isinstance(get_side_effect, BaseException) or (
        isinstance(get_side_effect, type) and issubclass(get_side_effect, BaseException)
    ):
        client.get.side_effect = get_side_effect
    else:
        client.get.return_value = get_side_effect
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _resp(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# WeatherTool
# ---------------------------------------------------------------------------
class TestWeatherTool:
    @pytest.fixture
    def tool(self):
        return WeatherTool()

    def test_name_and_trust(self, tool):
        assert tool.name == "weather"
        assert tool.trust_level == "standard"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert "location" in params["properties"]
        assert params["required"] == ["location"]

    async def test_forecast_success(self, tool):
        geo_resp = _resp(
            {
                "results": [
                    {
                        "name": "Tokyo",
                        "latitude": 35.69,
                        "longitude": 139.69,
                        "country": "Japan",
                        "admin1": "Tokyo",
                        "timezone": "Asia/Tokyo",
                    }
                ]
            }
        )
        forecast_resp = _resp(
            {
                "current": {
                    "temperature_2m": 18.4,
                    "weather_code": 1,
                    "wind_speed_10m": 12.0,
                    "relative_humidity_2m": 55,
                },
                "daily": {
                    "time": ["2026-05-31", "2026-06-01"],
                    "weather_code": [1, 61],
                    "temperature_2m_max": [22.1, 19.8],
                    "temperature_2m_min": [14.0, 13.2],
                    "precipitation_sum": [0.0, 5.4],
                    "wind_speed_10m_max": [15.0, 20.0],
                },
            }
        )
        client = _mock_client([geo_resp, forecast_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(location="Tokyo", days=2)

        assert "Tokyo" in result
        assert "Japan" in result
        assert "Mainly clear" in result  # weather_code 1
        assert "Slight rain" in result  # weather_code 61
        assert "18" in result  # current temp rounded

    async def test_location_not_found(self, tool):
        geo_resp = _resp({"results": []})
        client = _mock_client([geo_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(location="Nowheresville XYZ")
        assert "Error" in result
        assert "Could not find" in result

    async def test_empty_location(self, tool):
        result = await tool.execute(location="   ")
        assert "Error" in result
        assert "No location" in result

    async def test_timeout(self, tool):
        client = _mock_client(httpx.TimeoutException("slow"))
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(location="Tokyo")
        assert "Error" in result
        assert "timed out" in result


# ---------------------------------------------------------------------------
# WikiTool
# ---------------------------------------------------------------------------
class TestWikiTool:
    @pytest.fixture
    def tool(self):
        return WikiTool()

    def test_name_and_trust(self, tool):
        assert tool.name == "wiki"
        assert tool.trust_level == "standard"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert "topic" in params["properties"]
        assert params["required"] == ["topic"]

    async def test_summary_success(self, tool):
        resp = _resp(
            {
                "title": "Eiffel Tower",
                "displaytitle": '<span class="mw-page-title-main">Eiffel Tower</span>',
                "description": "Tower in Paris, France",
                "extract": "The Eiffel Tower is a wrought-iron lattice tower in Paris.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Eiffel_Tower"}},
                "coordinates": {"lat": 48.8584, "lon": 2.2945},
            }
        )
        client = _mock_client(resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(topic="Eiffel Tower")

        assert "Eiffel Tower" in result
        assert "wrought-iron lattice tower" in result
        assert "Tower in Paris, France" in result
        assert "en.wikipedia.org/wiki/Eiffel_Tower" in result
        # HTML must be stripped from the title.
        assert "<span" not in result

    async def test_not_found_404(self, tool):
        resp = _resp({}, status_code=404)
        client = _mock_client(resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(topic="asdkjfhqwoeiruzzz")
        assert "Error" in result
        assert "No Wikipedia article" in result

    async def test_empty_topic(self, tool):
        result = await tool.execute(topic="")
        assert "Error" in result
        assert "No topic" in result

    async def test_timeout(self, tool):
        client = _mock_client(httpx.TimeoutException("slow"))
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(topic="Paris")
        assert "Error" in result
        assert "timed out" in result


# ---------------------------------------------------------------------------
# CurrencyTool
# ---------------------------------------------------------------------------
class TestCurrencyTool:
    @pytest.fixture
    def tool(self):
        return CurrencyTool()

    def test_name_and_trust(self, tool):
        assert tool.name == "currency"
        assert tool.trust_level == "standard"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert set(params["required"]) == {"amount", "from_currency", "to_currency"}

    async def test_conversion_success(self, tool):
        resp = _resp(
            {
                "amount": 1.0,
                "base": "USD",
                "date": "2026-05-29",
                "rates": {"JPY": 159.27},
            }
        )
        client = _mock_client(resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(amount=100, from_currency="usd", to_currency="jpy")

        assert "USD" in result
        assert "JPY" in result
        assert "159.27" in result
        assert "2026-05-29" in result
        # 100 * 159.27 = 15927, JPY shown without decimals.
        assert "15,927" in result

    async def test_same_currency_short_circuits(self, tool):
        # No HTTP call needed for same-currency conversion.
        client = _mock_client(httpx.TimeoutException("should not be called"))
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(amount=50, from_currency="EUR", to_currency="EUR")
        assert "same currency" in result
        client.get.assert_not_called()

    async def test_invalid_amount(self, tool):
        result = await tool.execute(amount=-5, from_currency="USD", to_currency="EUR")
        assert "Error" in result
        assert "positive" in result

    async def test_bad_currency_code(self, tool):
        result = await tool.execute(amount=10, from_currency="DOLLARS", to_currency="EUR")
        assert "Error" in result
        assert "3 letters" in result

    async def test_unknown_base_404(self, tool):
        resp = _resp({}, status_code=404)
        client = _mock_client(resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(amount=10, from_currency="XYZ", to_currency="EUR")
        assert "Error" in result
        assert "Unknown or unsupported" in result

    async def test_missing_target_rate(self, tool):
        resp = _resp({"amount": 1.0, "base": "USD", "date": "2026-05-29", "rates": {}})
        client = _mock_client(resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(amount=10, from_currency="USD", to_currency="ZZZ")
        assert "Error" in result
        assert "No exchange rate" in result

    async def test_timeout(self, tool):
        client = _mock_client(httpx.TimeoutException("slow"))
        with patch("httpx.AsyncClient", return_value=client):
            result = await tool.execute(amount=10, from_currency="USD", to_currency="EUR")
        assert "Error" in result
        assert "timed out" in result
