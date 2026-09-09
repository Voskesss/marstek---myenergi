"""Dynamische uurtarieven van meerdere leveranciers (alleen tonen/vergelijken).

- Zonneplan: publieke website scrape (geen login)
- NextEnergy (+ fallback Zonneplan): Enever.nl feed (gratis token in .env)
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("price-providers")
TZ_NL = ZoneInfo("Europe/Amsterdam")

ENEVER_TODAY = "https://enever.nl/apiv3/stroomprijs_vandaag.php"
ENEVER_TOMORROW = "https://enever.nl/apiv3/stroomprijs_morgen.php"
ZONNEPLAN_URL = "https://www.zonneplan.nl/energie/dynamische-energieprijzen"

# Enever veldcodes
ENEVER_FIELDS = {
    "zonneplan": "prijsZP",
    "nextenergy": "prijsNE",
    "frank": "prijsFR",
}


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _hour_row(fr: datetime, price: float) -> Dict[str, Any]:
    till = fr + timedelta(hours=1)
    return {
        "from": fr.isoformat(),
        "till": till.isoformat(),
        "hour": fr.hour,
        "price_eur_kwh": round(float(price), 5),
    }


def _day_summary(prices: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    if not prices:
        return {
            "provider": label,
            "available": False,
            "prices": [],
            "current": None,
            "min": None,
            "max": None,
            "avg": None,
        }
    vals = [p["price_eur_kwh"] for p in prices]
    now = datetime.now(TZ_NL)
    current = next((p for p in prices if p["hour"] == now.hour), prices[0])
    # Filter op "vandaag" als er meerdere dagen in de lijst zitten
    today_prices = [
        p for p in prices
        if _parse_iso(p["from"]).astimezone(TZ_NL).date() == now.date()
    ] or prices
    today_vals = [p["price_eur_kwh"] for p in today_prices]
    imin = min(range(len(today_prices)), key=lambda i: today_prices[i]["price_eur_kwh"])
    imax = max(range(len(today_prices)), key=lambda i: today_prices[i]["price_eur_kwh"])
    return {
        "provider": label,
        "available": True,
        "prices": prices,
        "today_prices": today_prices,
        "current": current,
        "min": today_prices[imin],
        "max": today_prices[imax],
        "avg": round(sum(today_vals) / len(today_vals), 5) if today_vals else None,
    }


class PriceProviders:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl_s = 600.0  # 10 min — prijzen wijzigen 1x/dag

    def _enever_token(self) -> Optional[str]:
        tok = (os.getenv("ENEVER_TOKEN") or "").strip()
        return tok or None

    async def _fetch_zonneplan_scrape(self) -> List[Dict[str, Any]]:
        """Publieke Zonneplan-pagina → all-in uurprijzen (priceTotalTaxIncluded / 1e7)."""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.get(
                ZONNEPLAN_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; marstek-myenergi/1.0)",
                    "Accept": "text/html",
                },
            )
            r.raise_for_status()
            html = r.text

        idx = html.find('\\"energyData\\"')
        if idx < 0:
            idx = html.find('"energyData"')
        if idx < 0:
            raise RuntimeError("Zonneplan: energyData niet gevonden")

        chunk = html[idx : idx + 400000].replace('\\"', '"')
        m = re.search(r'"electricity"\s*:\s*\{\s*"hours"\s*:\s*(\[)', chunk)
        if not m:
            raise RuntimeError("Zonneplan: hours-array niet gevonden")
        start = m.start(1)
        depth = 0
        end = None
        for i in range(start, len(chunk)):
            if chunk[i] == "[":
                depth += 1
            elif chunk[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise RuntimeError("Zonneplan: hours-array niet afgesloten")
        hours = json_loads_safe(chunk[start:end])
        out: List[Dict[str, Any]] = []
        for h in hours:
            try:
                raw = h.get("priceTotalTaxIncluded")
                if raw is None:
                    continue
                # Zonneplan publiceert micro-cent integers → / 1e7 = €/kWh
                price = float(raw) / 1e7
                fr = _parse_iso(h["dateTime"]).astimezone(TZ_NL)
                out.append(_hour_row(fr, price))
            except Exception as e:
                logger.debug(f"ZP skip row: {e}")
        out.sort(key=lambda p: p["from"])
        return out

    async def _fetch_enever(
        self, which: str, field: str
    ) -> List[Dict[str, Any]]:
        token = self._enever_token()
        if not token:
            raise RuntimeError("ENEVER_TOKEN ontbreekt in .env")
        url = ENEVER_TODAY if which == "today" else ENEVER_TOMORROW
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.get(
                url,
                params={"token": token, "price": field, "resolution": "60"},
                headers={"User-Agent": "marstek-myenergi/1.0", "Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()

        rows = data if isinstance(data, list) else (data.get("data") or data.get("prices") or [])
        if isinstance(data, dict) and not rows:
            # Soms: { "2026-09-09": [ ... ] } of genest
            for v in data.values():
                if isinstance(v, list) and v:
                    rows = v
                    break

        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                ts = row.get("datum") or row.get("time") or row.get("datetime") or row.get("from")
                # veld kan prijsZP / prijsNE / genormaliseerd "prijs" zijn na filter
                price = None
                for key in (field, "prijs", "price", "price_eur_kwh"):
                    if row.get(key) is not None:
                        price = float(str(row[key]).replace(",", "."))
                        break
                if ts is None or price is None:
                    continue
                fr = _parse_iso(str(ts)).astimezone(TZ_NL)
                out.append(_hour_row(fr, price))
            except Exception as e:
                logger.debug(f"Enever skip: {e}")
        out.sort(key=lambda p: p["from"])
        return out

    async def get_compare(self, force: bool = False) -> Dict[str, Any]:
        now_ts = time.time()
        if not force and self._cache and (now_ts - self._cache_ts) < self._cache_ttl_s:
            return self._cache

        result: Dict[str, Any] = {
            "zonneplan": {"provider": "zonneplan", "available": False, "source": None, "error": None},
            "nextenergy": {"provider": "nextenergy", "available": False, "source": None, "error": None},
            "enever_token_configured": bool(self._enever_token()),
            "ts": now_ts,
        }

        # --- Zonneplan: scrape eerst, Enever als fallback ---
        zp_prices: List[Dict[str, Any]] = []
        zp_source = None
        try:
            zp_prices = await self._fetch_zonneplan_scrape()
            zp_source = "zonneplan.nl"
        except Exception as e:
            logger.warning(f"Zonneplan scrape mislukt: {e}")
            result["zonneplan"]["error"] = str(e)
            if self._enever_token():
                try:
                    today = await self._fetch_enever("today", "prijsZP")
                    try:
                        tom = await self._fetch_enever("tomorrow", "prijsZP")
                    except Exception:
                        tom = []
                    zp_prices = today + tom
                    zp_source = "enever.nl"
                    result["zonneplan"]["error"] = None
                except Exception as e2:
                    result["zonneplan"]["error"] = f"scrape: {e}; enever: {e2}"

        if zp_prices:
            summary = _day_summary(zp_prices, "zonneplan")
            summary["source"] = zp_source
            summary["error"] = None
            result["zonneplan"] = summary

        # --- NextEnergy: alleen via Enever ---
        if self._enever_token():
            try:
                today = await self._fetch_enever("today", "prijsNE")
                try:
                    tom = await self._fetch_enever("tomorrow", "prijsNE")
                except Exception:
                    tom = []
                ne_prices = today + tom
                if ne_prices:
                    summary = _day_summary(ne_prices, "nextenergy")
                    summary["source"] = "enever.nl"
                    summary["error"] = None
                    result["nextenergy"] = summary
                else:
                    result["nextenergy"]["error"] = "Geen NextEnergy-prijzen in Enever-response"
            except Exception as e:
                result["nextenergy"]["error"] = str(e)
        else:
            result["nextenergy"]["error"] = (
                "Zet ENEVER_TOKEN in .env (gratis op https://enever.nl/prijzenfeeds/) "
                "om NextEnergy-prijzen te tonen"
            )

        self._cache = result
        self._cache_ts = now_ts
        return result


def json_loads_safe(s: str):
    import json
    return json.loads(s)


price_providers = PriceProviders()
