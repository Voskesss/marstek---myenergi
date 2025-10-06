# Weather API Setup

## Stap 1: Voeg API Key toe aan .env

Open je `.env` bestand en voeg deze regel toe:

```bash
OPENWEATHER_API_KEY=cc59efe7950dceaaec398962c50a87c5
```

## Stap 2: (Optioneel) Pas locatie aan

Standaard locatie is "Almere,NL". Om dit te wijzigen:

In `weather.py`, regel 12, wijzig:
```python
def __init__(self, api_key: Optional[str] = None, location: str = "Almere,NL"):
```

Naar je gewenste locatie, bijvoorbeeld:
```python
def __init__(self, api_key: Optional[str] = None, location: str = "Amsterdam,NL"):
```

## Stap 3: Herstart de app

```bash
./start_production.sh
```

## Test de Weather API

```bash
# Test current weather
curl http://localhost:8000/api/weather/current

# Test forecast
curl http://localhost:8000/api/weather/forecast

# Test solar forecast (voor PV predictions)
curl http://localhost:8000/api/weather/solar
```

## Dashboard

De weer card verschijnt automatisch op het dashboard met:
- **Temperatuur**: Huidige temperatuur
- **Bewolking**: Percentage bewolking
- **PV Potentieel**: Geschatte zonne-energie potentieel (0-100%)
- **Advies**: "Veel zon verwacht" / "Gedeeltelijk bewolkt" / "Weinig zon verwacht"

Updates elke 10 minuten (binnen gratis API limiet van 1000 calls/dag).
