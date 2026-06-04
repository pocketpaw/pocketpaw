# Weather tool — current conditions + multi-day forecast via Open-Meteo.
# Created: 2026-05-31 — zero-config builtin tool (no API key required).
#
# Open-Meteo is a free, keyless weather API. Two hops:
#   - Geocoding: https://geocoding-api.open-meteo.com/v1/search (city -> lat/lon)
#   - Forecast:  https://api.open-meteo.com/v1/forecast
# Returns clean structured text the agent renders; no inline ui-spec plumbing
# (the inline-primitive layer is owned by a separate RFC).

import logging
from typing import Any

import httpx

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes -> short description.
_WEATHER_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _describe(code: int) -> str:
    return _WEATHER_CODES.get(code, "Unknown")


class WeatherTool(BaseTool):
    """Get current conditions and a multi-day forecast for any location.

    Uses Open-Meteo (free, no API key). Returns structured text with current
    temperature, conditions, and a daily high/low/precip forecast.
    """

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return (
            "Get the weather forecast for any location. No API key required. "
            "Returns current conditions plus a multi-day forecast with daily highs, "
            "lows, precipitation, and wind. Use for weather questions, trip planning, "
            "or deciding what to wear."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": 'City or place name, e.g. "Tokyo", "New York", "London".',
                },
                "days": {
                    "type": "integer",
                    "description": "Number of forecast days (1-14). Default: 5.",
                    "default": 5,
                },
                "units": {
                    "type": "string",
                    "description": "Temperature units: 'celsius' (default) or 'fahrenheit'.",
                    "default": "celsius",
                },
            },
            "required": ["location"],
        }

    async def execute(
        self,
        location: str,
        days: int = 5,
        units: str = "celsius",
    ) -> str:
        if not location or not location.strip():
            return self._error("No location provided.")

        days = min(max(int(days), 1), 14)
        units = units.lower()
        if units not in ("celsius", "fahrenheit"):
            units = "celsius"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                geo = await self._geocode(client, location)
                if geo is None:
                    return self._error(
                        f"Could not find a location named '{location}'. "
                        "Try a different or more specific name."
                    )

                forecast = await self._forecast(
                    client, geo["latitude"], geo["longitude"], days, units
                )
        except httpx.HTTPStatusError as e:
            return self._error(f"Weather API error: {e.response.status_code}")
        except httpx.TimeoutException:
            return self._error("Weather request timed out. Please try again.")
        except Exception as e:
            return self._error(f"Weather lookup failed: {e}")

        if not forecast or not forecast.get("daily"):
            return self._error("Weather data unavailable for that location right now.")

        return self._format(geo, forecast, days, units)

    async def _geocode(self, client: httpx.AsyncClient, location: str) -> dict | None:
        resp = await client.get(
            _GEOCODE_URL,
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return results[0] if results else None

    async def _forecast(
        self,
        client: httpx.AsyncClient,
        lat: float,
        lon: float,
        days: int,
        units: str,
    ) -> dict:
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_sum,wind_speed_10m_max"
            ),
            "forecast_days": days,
            "timezone": "auto",
        }
        if units == "fahrenheit":
            params["temperature_unit"] = "fahrenheit"
            params["wind_speed_unit"] = "mph"
        resp = await client.get(_FORECAST_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _format(geo: dict, forecast: dict, days: int, units: str) -> str:
        temp_unit = "°F" if units == "fahrenheit" else "°C"
        wind_unit = "mph" if units == "fahrenheit" else "km/h"

        admin1 = geo.get("admin1")
        country = geo.get("country", "")
        name = geo.get("name", "")
        place = f"{name}, {admin1}, {country}" if admin1 else f"{name}, {country}"
        place = place.strip().strip(",")

        lines = [f"Weather for {place}:"]

        current = forecast.get("current") or {}
        if current:
            lines.append(
                f"\nNow: {_describe(current.get('weather_code', -1))}, "
                f"{round(current.get('temperature_2m', 0))}{temp_unit}, "
                f"humidity {current.get('relative_humidity_2m', 0)}%, "
                f"wind {round(current.get('wind_speed_10m', 0))} {wind_unit}"
            )

        daily = forecast.get("daily", {})
        times = daily.get("time", [])
        if times:
            lines.append(f"\n{days}-day forecast:")
            for i, date in enumerate(times):
                code = daily["weather_code"][i]
                high = round(daily["temperature_2m_max"][i])
                low = round(daily["temperature_2m_min"][i])
                precip = daily["precipitation_sum"][i]
                lines.append(
                    f"  {date}: {_describe(code)}, high {high}{temp_unit} / "
                    f"low {low}{temp_unit}, precip {precip}mm"
                )

        return "\n".join(lines)
