"""
Weerintegratie voor energiebeslissingen.

Primair: Buienradar (NL-specifiek)
Fallback: OpenWeather One Call 3.0
"""
import os
import time
import logging
from typing import Optional, Dict, Any, List, Tuple
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)


class WeatherService:
    def __init__(self, api_key: Optional[str] = None, lat: float = 51.99, lon: float = 5.84):
        """Initialize weather service.

        Location is overridable via WEATHER_LAT/WEATHER_LON env vars.
        """
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY")
        self.lat = float(os.getenv("WEATHER_LAT", lat))
        self.lon = float(os.getenv("WEATHER_LON", lon))
        self.base_url = "https://api.openweathermap.org/data/3.0/onecall"
        self.buienradar_url = "https://data.buienradar.nl/2.0/feed/json"
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl_s = 600.0  # 10 min

    def _get_cache(self, key: str) -> Any:
        now = time.time()
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, data = hit
        if (now - ts) > self._cache_ttl_s:
            return None
        return data

    def _set_cache(self, key: str, data: Any):
        self._cache[key] = (time.time(), data)

    @staticmethod
    def _safe_num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _parse_hhmm_to_hour(hhmm: str) -> Optional[int]:
        try:
            return int(str(hhmm).split(":")[0])
        except Exception:
            return None

    def _nearest_station(self, stations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        best = None
        best_d = 10e9
        for s in stations or []:
            lat = self._safe_num(s.get("Latitude"), 0.0)
            lon = self._safe_num(s.get("Longitude"), 0.0)
            if lat == 0 and lon == 0:
                continue
            d = abs(lat - self.lat) + abs(lon - self.lon)
            if d < best_d:
                best_d = d
                best = s
        return best

    async def _fetch_buienradar(self) -> Optional[Dict[str, Any]]:
        cache_key = "buienradar_raw"
        cached = self._get_cache(cache_key)
        if cached:
            return cached
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(self.buienradar_url)
                response.raise_for_status()
                data = response.json()
            if isinstance(data, dict):
                self._set_cache(cache_key, data)
                return data
        except Exception as e:
            logger.warning(f"Buienradar fetch failed: {e}")
        return None

    async def _fetch_openweather_current(self) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self.base_url,
                    params={
                        "lat": self.lat,
                        "lon": self.lon,
                        "appid": self.api_key,
                        "units": "metric",
                        "lang": "nl",
                        "exclude": "minutely,alerts",
                    },
                )
                response.raise_for_status()
                data = response.json()
            current = data.get("current", {})
            return {
                "temperature": round(self._safe_num(current.get("temp")), 1),
                "feels_like": round(self._safe_num(current.get("feels_like")), 1),
                "humidity": int(self._safe_num(current.get("humidity"), 0)),
                "clouds": int(self._safe_num(current.get("clouds"), 100)),
                "description": ((current.get("weather") or [{}])[0]).get("description", "onbekend"),
                "icon": ((current.get("weather") or [{}])[0]).get("icon", ""),
                "sunrise": datetime.fromtimestamp(self._safe_num(current.get("sunrise"), 0)).strftime("%H:%M"),
                "sunset": datetime.fromtimestamp(self._safe_num(current.get("sunset"), 0)).strftime("%H:%M"),
                "wind_speed": round(self._safe_num(current.get("wind_speed"), 0) * 3.6, 1),
                "uvi": self._safe_num(current.get("uvi"), 0),
                "source": "openweather",
            }
        except Exception as e:
            logger.error(f"Failed to fetch current weather (OpenWeather): {e}")
            return None

    async def _fetch_openweather_forecast(self, hours: int = 24) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self.base_url,
                    params={
                        "lat": self.lat,
                        "lon": self.lon,
                        "appid": self.api_key,
                        "units": "metric",
                        "lang": "nl",
                        "exclude": "current,minutely,daily,alerts",
                    },
                )
                response.raise_for_status()
                data = response.json()
            forecasts = []
            for item in (data.get("hourly") or [])[:hours]:
                forecasts.append({
                    "time": datetime.fromtimestamp(self._safe_num(item.get("dt"), 0)).strftime("%H:%M"),
                    "date": datetime.fromtimestamp(self._safe_num(item.get("dt"), 0)).strftime("%d-%m"),
                    "temperature": round(self._safe_num(item.get("temp"), 0), 1),
                    "clouds": int(self._safe_num(item.get("clouds"), 100)),
                    "description": ((item.get("weather") or [{}])[0]).get("description", "onbekend"),
                    "icon": ((item.get("weather") or [{}])[0]).get("icon", ""),
                    "rain_prob": self._safe_num(item.get("pop"), 0) * 100,
                    "uvi": self._safe_num(item.get("uvi"), 0),
                })
            return {
                "forecasts": forecasts,
                "city": "Lokaal",
                "source": "openweather",
            }
        except Exception as e:
            logger.error(f"Failed to fetch forecast (OpenWeather): {e}")
            return None

    async def _fetch_buienradar_current(self) -> Optional[Dict[str, Any]]:
        raw = await self._fetch_buienradar()
        if not raw:
            return None
        stations = (((raw.get("actual") or {}).get("stationmeasurements")) or
                    ((raw.get("actual") or {}).get("WeatherStationMeasurements")) or [])
        st = self._nearest_station(stations)
        if not st:
            return None
        clouds = int(self._safe_num(st.get("cloudcover"), 100))
        return {
            "temperature": round(self._safe_num(st.get("temperature"), 0), 1),
            "feels_like": round(self._safe_num(st.get("feeltemperature"), 0), 1),
            "humidity": int(self._safe_num(st.get("humidity"), 0)),
            "clouds": clouds,
            "description": str(st.get("weatherdescription") or st.get("weatherdescriptionnl") or "onbekend"),
            "icon": str(st.get("iconurl") or ""),
            "sunrise": str(((raw.get("forecast") or {}).get("sunrise")) or ""),
            "sunset": str(((raw.get("forecast") or {}).get("sunset")) or ""),
            "wind_speed": round(self._safe_num(st.get("windspeed"), 0), 1),
            "uvi": self._safe_num(st.get("sunpower"), 0),
            "source": "buienradar",
            "station": st.get("stationname") or st.get("stationName"),
        }

    async def _fetch_buienradar_forecast(self, hours: int = 24) -> Optional[Dict[str, Any]]:
        raw = await self._fetch_buienradar()
        if not raw:
            return None
        fc_root = raw.get("forecast") or {}
        forecast = (
            fc_root.get("hourlyforecast")
            or fc_root.get("hourlyForecast")
            or fc_root.get("hourly")
            or []
        )
        # Sommige Buienradar feeds bevatten alleen dagdata.
        # Dan gebruiken we OpenWeather fallback voor echte uur-balkjes.
        if not forecast:
            return None
        out = []
        for item in forecast[:hours]:
            time_str = str(item.get("hour") or item.get("datetime") or "--:--")
            hour = self._parse_hhmm_to_hour(time_str)
            clouds = int(self._safe_num(item.get("cloudcover"), 100))
            out.append({
                "time": time_str if ":" in time_str else (f"{hour:02d}:00" if hour is not None else "--:--"),
                "date": str(item.get("date") or ""),
                "temperature": round(self._safe_num(item.get("temperature"), 0), 1),
                "clouds": clouds,
                "description": str(item.get("weatherdescription") or "onbekend"),
                "icon": str(item.get("iconurl") or ""),
                "rain_prob": self._safe_num(item.get("rainchance"), 0),
                "uvi": self._safe_num(item.get("sunpower"), 0),
            })
        return {
            "forecasts": out,
            "city": "Lokaal",
            "source": "buienradar",
        }

    async def get_current_weather(self) -> Optional[Dict[str, Any]]:
        """Current weather. Buienradar first, OpenWeather fallback."""
        cached = self._get_cache("current_weather")
        if cached:
            return cached
        data = await self._fetch_buienradar_current()
        if not data:
            data = await self._fetch_openweather_current()
        if data:
            self._set_cache("current_weather", data)
        return data

    async def get_forecast(self, hours: int = 24) -> Optional[Dict[str, Any]]:
        """Hourly forecast. Buienradar first, OpenWeather fallback."""
        cache_key = f"forecast_{hours}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached
        data = await self._fetch_buienradar_forecast(hours=hours)
        if not data:
            data = await self._fetch_openweather_forecast(hours=hours)
        if data:
            self._set_cache(cache_key, data)
        return data

    async def get_solar_forecast(self) -> Optional[Dict[str, Any]]:
        """Get solar-relevant forecast for PV predictions."""
        current = await self.get_current_weather()
        forecast = await self.get_forecast(hours=24)
        if not current or not forecast:
            return None
        forecasts = forecast.get("forecasts") or []
        if not forecasts:
            return None
        avg_clouds = sum(int(f.get("clouds", 100)) for f in forecasts) / len(forecasts)
        solar_potential = 100 - avg_clouds
        return {
            "current": current,
            "forecast_24h": forecast,
            "solar_potential": round(solar_potential, 1),
            "recommendation": (
                "Veel zon verwacht" if solar_potential > 70 else
                "Gedeeltelijk bewolkt" if solar_potential > 40 else
                "Weinig zon verwacht"
            ),
            "source": forecast.get("source") or current.get("source") or "unknown",
        }

    async def get_solar_hours(self, hours: int = 12) -> Dict[str, Any]:
        """Compacte uurdata voor sidebar staafjes."""
        fc = await self.get_forecast(hours=max(1, hours))
        if not fc:
            return {"hours": [], "source": "none"}
        cur = await self.get_current_weather()
        sunrise_h = None
        sunset_h = None
        try:
            sunrise_h = self._parse_hhmm_to_hour((cur or {}).get("sunrise", ""))
            sunset_h = self._parse_hhmm_to_hour((cur or {}).get("sunset", ""))
        except Exception:
            sunrise_h = None
            sunset_h = None
        rows = []
        for f in (fc.get("forecasts") or [])[:hours]:
            clouds = int(f.get("clouds", 100))
            solar_score = max(0, min(100, 100 - clouds))
            hour = self._parse_hhmm_to_hour(str(f.get("time", "")))
            # Buiten daglicht: zon-score geforceerd naar 0.
            if (
                hour is not None
                and sunrise_h is not None
                and sunset_h is not None
                and (hour < sunrise_h or hour >= sunset_h)
            ):
                solar_score = 0
            rows.append({
                "time": f.get("time", "--:--"),
                "clouds": clouds,
                "solar_score": solar_score,
                "rain_prob": round(float(f.get("rain_prob", 0)), 1),
            })
        return {"hours": rows, "source": fc.get("source", "unknown")}

    async def get_no_sun_likely(self, horizon_hours: int = 4) -> Dict[str, Any]:
        """Heuristiek: weinig zon komende uren?"""
        sh = await self.get_solar_hours(hours=max(1, horizon_hours))
        rows = sh.get("hours") or []
        if not rows:
            return {"no_sun_likely": False, "confidence": 0, "source": sh.get("source", "none")}
        avg_solar = sum(r.get("solar_score", 0) for r in rows) / len(rows)
        avg_clouds = sum(r.get("clouds", 100) for r in rows) / len(rows)
        no_sun = avg_solar <= 25 or avg_clouds >= 75
        confidence = int(min(100, max(0, (avg_clouds - 50) * 2)))
        return {
            "no_sun_likely": bool(no_sun),
            "confidence": confidence,
            "avg_clouds": round(avg_clouds, 1),
            "avg_solar_score": round(avg_solar, 1),
            "source": sh.get("source", "unknown"),
        }

    async def get_tomorrow_sun_likely(
        self,
        *,
        solar_min: float = 55.0,
        look_hours: int = 36,
    ) -> Dict[str, Any]:
        """Heuristiek: morgen overdag veel zon? (daglicht-uren met solar_score > 0)."""
        sh = await self.get_solar_hours(hours=max(12, look_hours))
        rows = sh.get("hours") or []
        if not rows:
            return {
                "tomorrow_sun_likely": False,
                "confidence": 0,
                "avg_solar_score": 0.0,
                "daylight_hours": 0,
                "source": sh.get("source", "none"),
            }
        # Neem uren na vandaag (vanaf middernacht): ruwweg 24h-horizon vanaf nu,
        # maar alleen daglicht (solar_score > 0).
        daylight = [r for r in rows if float(r.get("solar_score") or 0) > 0]
        # Prefer uren verder weg (morgen): skip eerste ~6 daglicht-uren van vandaag als er genoeg is
        if len(daylight) > 10:
            tomorrowish = daylight[6:]
        else:
            tomorrowish = daylight
        if not tomorrowish:
            return {
                "tomorrow_sun_likely": False,
                "confidence": 0,
                "avg_solar_score": 0.0,
                "daylight_hours": 0,
                "source": sh.get("source", "unknown"),
            }
        avg_solar = sum(float(r.get("solar_score") or 0) for r in tomorrowish) / len(tomorrowish)
        avg_clouds = sum(float(r.get("clouds") or 100) for r in tomorrowish) / len(tomorrowish)
        likely = avg_solar >= float(solar_min)
        confidence = int(min(100, max(0, avg_solar)))
        return {
            "tomorrow_sun_likely": bool(likely),
            "confidence": confidence,
            "avg_solar_score": round(avg_solar, 1),
            "avg_clouds": round(avg_clouds, 1),
            "daylight_hours": len(tomorrowish),
            "source": sh.get("source", "unknown"),
        }


# Global instance
weather_service = WeatherService()
