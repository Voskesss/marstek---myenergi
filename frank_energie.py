"""Frank Energie market prices (public GraphQL, no login required).

Used for dashboard display for now. Battery control on price comes later.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("frank-energie")

GRAPHQL_URL = "https://www.frankenergie.nl/graphql"
TZ_NL = ZoneInfo("Europe/Amsterdam")

_QUERY = """
query MarketPrices($date: String!, $resolution: PriceResolution!) {
  marketPrices(date: $date, resolution: $resolution) {
    electricityPrices {
      from
      till
      marketPrice
      marketPriceTax
      sourcingMarkupPrice
      energyTaxPrice
    }
  }
}
"""


def _total_price(p: Dict[str, Any]) -> float:
    return float(
        (p.get("marketPrice") or 0)
        + (p.get("marketPriceTax") or 0)
        + (p.get("sourcingMarkupPrice") or 0)
        + (p.get("energyTaxPrice") or 0)
    )


def _parse_iso(ts: str) -> datetime:
    # Frank returns e.g. 2026-08-05T10:00:00.000Z
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class FrankEnergieClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl_s = 300.0  # 5 min

    async def _fetch_day(self, day: date) -> List[Dict[str, Any]]:
        payload = {
            "query": _QUERY,
            "variables": {"date": day.isoformat(), "resolution": "PT60M"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                GRAPHQL_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "marstek-myenergi/1.0",
                },
            )
            r.raise_for_status()
            data = r.json()

        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))

        raw = (
            (data.get("data") or {})
            .get("marketPrices", {})
            .get("electricityPrices")
            or []
        )
        out: List[Dict[str, Any]] = []
        for p in raw:
            try:
                fr = _parse_iso(p["from"]).astimezone(TZ_NL)
                till = _parse_iso(p["till"]).astimezone(TZ_NL)
                total = round(_total_price(p), 5)
                out.append({
                    "from": fr.isoformat(),
                    "till": till.isoformat(),
                    "hour": fr.hour,
                    "price_eur_kwh": total,
                    "market_price": p.get("marketPrice"),
                    "tax": p.get("marketPriceTax"),
                    "sourcing": p.get("sourcingMarkupPrice"),
                    "energy_tax": p.get("energyTaxPrice"),
                })
            except Exception as e:
                logger.debug(f"Skip price row: {e}")
        return out

    async def get_overview(self, force: bool = False) -> Dict[str, Any]:
        import time
        now_ts = time.time()
        if (
            not force
            and self._cache
            and (now_ts - self._cache_ts) < self._cache_ttl_s
        ):
            return self._cache

        now_nl = datetime.now(TZ_NL)
        today = now_nl.date()
        tomorrow = today + timedelta(days=1)

        today_prices = await self._fetch_day(today)
        tomorrow_prices: List[Dict[str, Any]] = []
        try:
            tomorrow_prices = await self._fetch_day(tomorrow)
        except Exception as e:
            logger.info(f"Frank tomorrow prices not available yet: {e}")

        current: Optional[Dict[str, Any]] = None
        for p in today_prices:
            fr = datetime.fromisoformat(p["from"])
            till = datetime.fromisoformat(p["till"])
            if fr <= now_nl < till:
                current = p
                break
        if current is None and today_prices:
            # fallback: closest hour
            current = min(
                today_prices,
                key=lambda p: abs(datetime.fromisoformat(p["from"]) - now_nl),
            )

        def _extremes(prices: List[Dict[str, Any]]) -> Dict[str, Any]:
            if not prices:
                return {"min": None, "max": None, "avg": None}
            sorted_p = sorted(prices, key=lambda x: x["price_eur_kwh"])
            avg = sum(x["price_eur_kwh"] for x in prices) / len(prices)
            return {
                "min": sorted_p[0],
                "max": sorted_p[-1],
                "avg": round(avg, 5),
            }

        result = {
            "provider": "frank_energie",
            "timezone": "Europe/Amsterdam",
            "fetched_at": now_nl.isoformat(),
            "current": current,
            "today": {
                "date": today.isoformat(),
                "prices": today_prices,
                **_extremes(today_prices),
            },
            "tomorrow": {
                "date": tomorrow.isoformat(),
                "available": bool(tomorrow_prices),
                "prices": tomorrow_prices,
                **_extremes(tomorrow_prices),
            },
        }
        self._cache = result
        self._cache_ts = now_ts
        return result


frank_client = FrankEnergieClient()
