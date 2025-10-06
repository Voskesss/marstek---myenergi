"""
Weather API integration for OpenWeatherMap One Call API 3.0
Fetches current weather and forecast for smart energy decisions
"""
import os
import logging
from typing import Optional, Dict, Any
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

class WeatherService:
    def __init__(self, api_key: Optional[str] = None, lat: float = 51.99, lon: float = 5.84):
        """Initialize weather service
        Default location: Oosterbeek, NL (51.99, 5.84)
        """
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY")
        self.lat = lat
        self.lon = lon
        self.base_url = "https://api.openweathermap.org/data/3.0/onecall"
        
    async def get_current_weather(self) -> Optional[Dict[str, Any]]:
        """Get current weather conditions using One Call API 3.0"""
        if not self.api_key:
            logger.warning("No OpenWeather API key configured")
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
                        "exclude": "minutely,alerts"  # Only get current, hourly, daily
                    }
                )
                response.raise_for_status()
                data = response.json()
                current = data["current"]
                
                return {
                    "temperature": round(current["temp"], 1),
                    "feels_like": round(current["feels_like"], 1),
                    "humidity": current["humidity"],
                    "clouds": current["clouds"],  # Cloud coverage %
                    "description": current["weather"][0]["description"],
                    "icon": current["weather"][0]["icon"],
                    "sunrise": datetime.fromtimestamp(current["sunrise"]).strftime("%H:%M"),
                    "sunset": datetime.fromtimestamp(current["sunset"]).strftime("%H:%M"),
                    "wind_speed": round(current["wind_speed"] * 3.6, 1),  # m/s to km/h
                    "uvi": current.get("uvi", 0),  # UV index
                }
        except Exception as e:
            logger.error(f"Failed to fetch current weather: {e}")
            return None
    
    async def get_forecast(self, hours: int = 24) -> Optional[Dict[str, Any]]:
        """Get hourly weather forecast using One Call API 3.0"""
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
                        "exclude": "current,minutely,daily,alerts"
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                forecasts = []
                # Take only requested hours (API gives 48 hours)
                for item in data["hourly"][:hours]:
                    forecasts.append({
                        "time": datetime.fromtimestamp(item["dt"]).strftime("%H:%M"),
                        "date": datetime.fromtimestamp(item["dt"]).strftime("%d-%m"),
                        "temperature": round(item["temp"], 1),
                        "clouds": item["clouds"],
                        "description": item["weather"][0]["description"],
                        "icon": item["weather"][0]["icon"],
                        "rain_prob": item.get("pop", 0) * 100,  # Probability of precipitation
                        "uvi": item.get("uvi", 0),
                    })
                
                return {
                    "forecasts": forecasts,
                    "city": "Oosterbeek"
                }
        except Exception as e:
            logger.error(f"Failed to fetch forecast: {e}")
            return None
    
    async def get_solar_forecast(self) -> Optional[Dict[str, Any]]:
        """Get solar-relevant forecast for PV predictions"""
        current = await self.get_current_weather()
        forecast = await self.get_forecast(hours=24)
        
        if not current or not forecast:
            return None
        
        # Calculate average cloud coverage for next 24h
        avg_clouds = sum(f["clouds"] for f in forecast["forecasts"]) / len(forecast["forecasts"])
        
        # Estimate solar production potential (0-100%)
        solar_potential = 100 - avg_clouds
        
        return {
            "current": current,
            "forecast_24h": forecast,
            "solar_potential": round(solar_potential, 1),
            "recommendation": (
                "Veel zon verwacht" if solar_potential > 70 else
                "Gedeeltelijk bewolkt" if solar_potential > 40 else
                "Weinig zon verwacht"
            )
        }

# Global instance
weather_service = WeatherService()
