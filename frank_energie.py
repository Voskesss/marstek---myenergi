"""Frank Energie market prices (public GraphQL, no login required).

Levert ook een laadplanning op basis van actuele SOC en dagprijzen.
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

    async def get_overview(
        self,
        force: bool = False,
        soc_pct: Optional[float] = None,
        battery_capacity_kwh: float = 10.0,
        charge_power_kw: float = 2.5,
        target_soc_pct: float = 95.0,
    ) -> Dict[str, Any]:
        import time
        now_ts = time.time()
        cache_hit = (
            not force
            and self._cache
            and (now_ts - self._cache_ts) < self._cache_ttl_s
        )
        if cache_hit and soc_pct is None:
            return self._cache

        if cache_hit and soc_pct is not None:
            # Prijzen gecached, maar laadplanning altijd opnieuw met actuele SOC
            cached = dict(self._cache)
            today_prices = (cached.get("today") or {}).get("prices") or []
            tomorrow_prices = (cached.get("tomorrow") or {}).get("prices") or []
            current = cached.get("current")
            cached["plan"] = _build_daily_plan(
                today_prices,
                current,
                tomorrow_prices=tomorrow_prices if tomorrow_prices else None,
                soc_pct=soc_pct,
                battery_capacity_kwh=battery_capacity_kwh,
                charge_power_kw=charge_power_kw,
                target_soc_pct=target_soc_pct,
            )
            return cached

        if cache_hit:
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
            "plan": _build_daily_plan(
                today_prices,
                current,
                tomorrow_prices=tomorrow_prices if tomorrow_prices else None,
                soc_pct=soc_pct,
                battery_capacity_kwh=battery_capacity_kwh,
                charge_power_kw=charge_power_kw,
                target_soc_pct=target_soc_pct,
            ),
        }
        self._cache = result
        self._cache_ts = now_ts
        return result


frank_client = FrankEnergieClient()


def _build_daily_plan(
    prices: List[Dict[str, Any]],
    current: Optional[Dict[str, Any]],
    tomorrow_prices: Optional[List[Dict[str, Any]]] = None,
    soc_pct: Optional[float] = None,
    battery_capacity_kwh: float = 10.0,
    charge_power_kw: float = 2.5,
    target_soc_pct: float = 95.0,
) -> Dict[str, Any]:
    """Dagelijkse drempels t.o.v. min/max van vandaag — geen vaste €0,10.

    Goedkoop = onderste 25% van de dagspreiding.
    Duur = bovenste 25%.

    Bouwt ook een charge_schedule op basis van de huidige SOC:
    - Berekent hoeveel kWh nog nodig is tot target_soc_pct
    - Bepaalt hoeveel uur laden nodig is bij charge_power_kw
    - Kiest de goedkoopste toekomstige uren (ook morgen als al beschikbaar)
    - Geeft active_now=True als het huidige uur in die selectie zit
    """
    empty = {
        "method": "daily_spread_25_75",
        "cheap_threshold_eur_kwh": None,
        "expensive_threshold_eur_kwh": None,
        "min": None,
        "max": None,
        "spread": None,
        "band": "unknown",
        "cheap_hours": [],
        "expensive_hours": [],
        "very_cheap": False,
        "charge_target_soc": 95,
        "charge_schedule": None,
    }
    if not prices:
        return empty

    vals = [p["price_eur_kwh"] for p in prices]
    mn, mx = min(vals), max(vals)
    spread = max(0.01, mx - mn)
    cheap_thr = round(mn + 0.25 * spread, 5)
    exp_thr = round(mn + 0.75 * spread, 5)
    sorted_p = sorted(prices, key=lambda x: x["price_eur_kwh"])
    cheap_hours = sorted({p["hour"] for p in sorted_p[:6]})
    exp_hours = sorted({p["hour"] for p in sorted_p[-6:]})
    very_cheap_hours = {p["hour"] for p in sorted_p[:2]}

    cur_price = current["price_eur_kwh"] if current else None
    cur_hour = current["hour"] if current else None
    if cur_price is None:
        band = "unknown"
    elif cur_price <= cheap_thr:
        band = "cheap"
    elif cur_price >= exp_thr:
        band = "expensive"
    else:
        band = "mid"
    very_cheap = cur_hour in very_cheap_hours if cur_hour is not None else False

    # --- Laadplanning ---
    charge_schedule = _build_charge_schedule(
        today_prices=prices,
        tomorrow_prices=tomorrow_prices,
        current=current,
        soc_pct=soc_pct,
        battery_capacity_kwh=battery_capacity_kwh,
        charge_power_kw=charge_power_kw,
        target_soc_pct=target_soc_pct,
        cheap_threshold_eur_kwh=cheap_thr,
    )

    return {
        "method": "daily_spread_25_75",
        "cheap_threshold_eur_kwh": cheap_thr,
        "expensive_threshold_eur_kwh": exp_thr,
        "min": round(mn, 5),
        "max": round(mx, 5),
        "spread": round(spread, 5),
        "band": band,
        "cheap_hours": cheap_hours,
        "expensive_hours": exp_hours,
        "very_cheap": very_cheap,
        "charge_target_soc": int(target_soc_pct) if target_soc_pct is not None else 95,
        "charge_schedule": charge_schedule,
    }


def _build_charge_schedule(
    today_prices: List[Dict[str, Any]],
    tomorrow_prices: Optional[List[Dict[str, Any]]],
    current: Optional[Dict[str, Any]],
    soc_pct: Optional[float],
    battery_capacity_kwh: float,
    charge_power_kw: float,
    target_soc_pct: float,
    cheap_threshold_eur_kwh: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Bepaal de optimale laaduren op basis van huidige SOC en toekomstige prijzen.

    Retourneert None als SOC onbekend is of al op doel.
    """
    if soc_pct is None:
        return {"active_now": False, "reason": "soc_unknown", "planned_slots": [], "needed_hours": 0}

    soc_now = float(soc_pct)
    target = float(target_soc_pct)

    if soc_now >= target:
        return {
            "active_now": False,
            "reason": "already_at_target",
            "soc_now": round(soc_now, 1),
            "target_soc": target,
            "needed_kwh": 0.0,
            "needed_hours": 0.0,
            "planned_slots": [],
            "total_cost_eur": 0.0,
        }

    needed_kwh = round((target - soc_now) / 100.0 * battery_capacity_kwh, 2)
    needed_hours = needed_kwh / charge_power_kw  # bijv. 4.5 kWh / 2.5 kW = 1.8 uur

    now_nl = datetime.now(TZ_NL)
    cur_hour = current["hour"] if current else now_nl.hour

    # Bouw kandidaatlijst: toekomstige uren van vandaag + morgen (als beschikbaar)
    # Alleen vandaag: batterijen gaan 's nachts leeg, morgen opnieuw plannen
    candidates: List[Dict[str, Any]] = []
    for p in today_prices:
        if p["hour"] >= cur_hour:
            if cheap_threshold_eur_kwh is not None and p["price_eur_kwh"] > cheap_threshold_eur_kwh:
                continue
            candidates.append({"day": "today", "hour": p["hour"], "price": p["price_eur_kwh"], "from": p["from"]})

    if not candidates:
        return {
            "active_now": False,
            "reason": "no_cheap_slots_left",
            "soc_now": round(soc_now, 1),
            "target_soc": target,
            "needed_kwh": needed_kwh,
            "needed_hours": round(needed_hours, 2),
            "planned_slots": [],
        }

    # Sorteer op prijs, pak genoeg uren (ceil naar boven)
    import math
    n_slots = math.ceil(needed_hours)
    cheapest = sorted(candidates, key=lambda x: x["price"])[:n_slots]

    # Bereken totale verwachte kosten
    total_cost = sum(s["price"] * charge_power_kw for s in cheapest)

    # Is het huidige uur gepland?
    planned_today_hours = {s["hour"] for s in cheapest if s["day"] == "today"}
    active_now = cur_hour in planned_today_hours

    return {
        "active_now": active_now,
        "reason": "scheduled" if active_now else "waiting_for_cheap_slot",
        "soc_now": round(soc_now, 1),
        "target_soc": target,
        "needed_kwh": needed_kwh,
        "needed_hours": round(needed_hours, 2),
        "n_slots": n_slots,
        "planned_slots": sorted(cheapest, key=lambda x: (x["day"], x["hour"])),
        "total_cost_eur": round(total_cost, 4),
        "cheapest_price_eur_kwh": cheapest[0]["price"] if cheapest else None,
        "most_expensive_planned_eur_kwh": cheapest[-1]["price"] if cheapest else None,
    }
