"""
Windsurf prompt — 1-file app (FastAPI) voor myenergi + Marstek met automatische regellogica.

Wat het doet:
- Leest myenergi (cloud of lokale hub) voor Eddi/Zappi/Harvi status (via /cgi-jstatus-*).
- Stuurt je Marstek-batterij aan (charge inhibit/allow) tijdens Eddi-verwarming.
- Exporteert /api/status (samengevoegd beeld) en /api/control (handmatige override).

Configuratie via omgevingsvariabelen (.env of echt):
  MYENERGI_BASE_URL   (bv. https://s18.myenergi.net of http://192.168.1.50)
  MYENERGI_HUB_SERIAL (serienummer hub of device, bv. Z12345678)
  MYENERGI_API_KEY    (api key uit myenergi app)
  MARSTEK_BASE_URL    (bv. http://192.168.1.60)
  MARSTEK_API_TOKEN   (optioneel)

Run:
  pip install -r requirements.txt
  uvicorn app:app --reload --port 8000
"""

import os
import time
import asyncio
from enum import Enum
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, BackgroundTasks, Query, Body
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv

# BLE integration
try:
    from ble_client import get_ble_client, cleanup_ble_client
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False
    print("⚠️  BLE not available (install: pip install bleak)")

# =========================
# Config
# =========================
load_dotenv()

ENV_DEFAULTS = {
    "MYENERGI_BASE_URL":   "https://s18.myenergi.net",
    "MYENERGI_HUB_SERIAL": "Z12345678",
    "MYENERGI_API_KEY":    "replace_me",
    "MARSTEK_BASE_URL":    "http://192.168.1.60",
    "MARSTEK_API_TOKEN":   "",
    "MARSTEK_BLE_BRIDGE":  "http://localhost:8001",  # BLE bridge fallback
    "MARSTEK_USE_BLE":     "false",  # Use BLE bridge instead of direct network
}

MYENERGI_BASE_URL   = os.getenv("MYENERGI_BASE_URL",   ENV_DEFAULTS["MYENERGI_BASE_URL"]).rstrip("/")
MYENERGI_HUB_SERIAL = os.getenv("MYENERGI_HUB_SERIAL", ENV_DEFAULTS["MYENERGI_HUB_SERIAL"]).strip()
MYENERGI_API_KEY    = os.getenv("MYENERGI_API_KEY",    ENV_DEFAULTS["MYENERGI_API_KEY"]).strip()
MARSTEK_BASE_URL    = os.getenv("MARSTEK_BASE_URL",    ENV_DEFAULTS["MARSTEK_BASE_URL"]).rstrip("/")
MARSTEK_API_TOKEN   = os.getenv("MARSTEK_API_TOKEN",   ENV_DEFAULTS["MARSTEK_API_TOKEN"]).strip()
MARSTEK_BLE_BRIDGE  = os.getenv("MARSTEK_BLE_BRIDGE",  ENV_DEFAULTS["MARSTEK_BLE_BRIDGE"]).rstrip("/")
MARSTEK_USE_BLE     = os.getenv("MARSTEK_USE_BLE",     ENV_DEFAULTS["MARSTEK_USE_BLE"]).lower() == "true"

# Regellogica parameters (env-overrides mogelijk)
EDDI_PRIORITY_MODE     = os.getenv("EDDI_PRIORITY_MODE", "threshold").lower() # "power", "temp", "threshold"
EDDI_ACTIVE_W          = int(os.getenv("EDDI_ACTIVE_W", "200"))              # Eddi gebruikt stroom (W)
EDDI_MAX_CAPACITY_W    = int(os.getenv("EDDI_MAX_CAPACITY_W", "3600"))       # Eddi max vermogen (W)
EDDI_RESERVE_W         = int(os.getenv("EDDI_RESERVE_W", "3000"))            # Reserve voor Eddi (W)
ZAPPI_ACTIVE_W         = int(os.getenv("ZAPPI_ACTIVE_W", "200"))             # Zappi gebruikt stroom (W)
ZAPPI_RESERVE_W        = int(os.getenv("ZAPPI_RESERVE_W", "2000"))           # Reserve voor Zappi (W)
BATTERY_MIN_EXPORT_W   = int(os.getenv("BATTERY_MIN_EXPORT_W", "5000"))      # Min export voor batterij (W)
BATTERY_HYSTERESIS_W   = int(os.getenv("BATTERY_HYSTERESIS_W", "500"))       # Anti-toggle hysterese (W)
EDDI_TARGET_TEMP_1     = int(os.getenv("EDDI_TARGET_TEMP_1", "59"))          # Tank 1 doeltemperatuur (°C)
EDDI_TARGET_TEMP_2     = int(os.getenv("EDDI_TARGET_TEMP_2", "59"))          # Tank 2 doeltemperatuur (°C)
EDDI_TEMP_HYSTERESIS   = int(os.getenv("EDDI_TEMP_HYSTERESIS", "3"))         # Temperatuur hysterese (°C)
EDDI_USE_TANK_1        = os.getenv("EDDI_USE_TANK_1", "true").lower() == "true"   # Tank 1 actief
EDDI_USE_TANK_2        = os.getenv("EDDI_USE_TANK_2", "false").lower() == "true"  # Tank 2 actief
EXPORT_ENOUGH_W        = int(os.getenv("EXPORT_ENOUGH_W", "300"))
IMPORT_DIP_W           = int(os.getenv("IMPORT_DIP_W", "150"))
STABLE_EXPORT_SECONDS  = int(os.getenv("STABLE_EXPORT_SECONDS", "30"))
MIN_SWITCH_COOLDOWN_S  = int(os.getenv("MIN_SWITCH_COOLDOWN_S", "60"))
SOC_FAILSAFE_MIN       = int(os.getenv("SOC_FAILSAFE_MIN", "15"))
POLL_INTERVAL_S        = float(os.getenv("POLL_INTERVAL_S", "2"))

USER_AGENT = {"User-Agent": "Wget/1.14 (linux-gnu)"}

# =========================
# Clients
# =========================
class MyEnergiClient:
    """
    Leest myenergi via cloud (Digest) of lokaal (Basic).
    Cloud: base_url lijkt op https://sXX.myenergi.net -> DigestAuth + User-Agent vereist.
    Lokaal: base_url http(s)://hub-ip -> Basic auth.
    """

    def __init__(self, base_url: str, hub_serial: str, api_key: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.hub_serial = hub_serial
        self.api_key = api_key
        self.timeout = timeout
        self.is_cloud = self.base_url.startswith("https://s") and ".myenergi.net" in self.base_url

    def _auth(self):
        if self.is_cloud:
            return httpx.DigestAuth(self.hub_serial, self.api_key)
        return (self.hub_serial, self.api_key)

    def _headers(self) -> Dict[str, str]:
        return USER_AGENT if self.is_cloud else {}

    async def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout, auth=self._auth(), headers=self._headers()) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    async def status_all(self) -> Dict[str, Any]:
        """Probeer wildcard, val terug op specifieke endpoints."""
        # Sommige servers accepteren /cgi-jstatus-* (alles), anders apart per type.
        try:
            data = await self._get("/cgi-jstatus-*")
            return {"raw": data}
        except Exception:
            results: Dict[str, Any] = {}
            for code, key in [("Z", "zappi"), ("E", "eddi"), ("H", "harvi")]:
                try:
                    results[key] = await self._get(f"/cgi-jstatus-{code}")
                except Exception:
                    results[key] = None
            return results

class MarstekClient:
    """
    Placeholder voor Marstek batterij. Pas endpoints/velden aan jouw model.
    """
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _get(self, path: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            r = await client.get(f"{self.base_url}{path}")
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            r = await client.post(f"{self.base_url}{path}", json=payload)
            r.raise_for_status()
            return r.json() if r.content else {}

    # ---- Leesdata (pas aan) ----
    async def get_overview(self) -> Dict[str, Any]:
        """Try multiple common overview endpoints and accept JSON or simple text.
        Expected JSON example: {"soc": 72.5, "batt_power": -1200}
        """
        # Check if we should use integrated BLE instead
        if MARSTEK_USE_BLE and BLE_AVAILABLE:
            try:
                ble_client = get_ble_client()
                ble_data = await ble_client.get_battery_status()
                
                # Convert BLE response to expected format
                return {
                    "soc": ble_data.get("soc", 0),
                    "batt_power": ble_data.get("power", 0),
                    "voltage": ble_data.get("voltage", 0),
                    "current": ble_data.get("current", 0),
                    "connected": ble_data.get("connected", False),
                    "source": "ble_integrated",
                    "timestamp": ble_data.get("timestamp")
                }
            except Exception as e:
                return {"error": f"BLE error: {e}", "source": "ble_integrated"}
        
        # Use direct network API (original implementation)
        candidates = [
            "/api/overview",
            "/overview",
            "/api/status",
            "/status",
            "/api",
            "/",
        ]
        last_err: Optional[str] = None
        for p in candidates:
            try:
                async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                    r = await client.get(f"{self.base_url}{p}")
                    r.raise_for_status()
                    # Try JSON first
                    try:
                        data = r.json()
                        return data
                    except ValueError:
                        # Accept simple key=value or plain text by wrapping
                        text = r.text.strip()
                        if text:
                            return {"raw": text}
            except Exception as e:
                last_err = str(e)
                continue
        raise RuntimeError(last_err or "No endpoints matched")

    async def probe(self, ports: Optional[list[int]] = None) -> Dict[str, Any]:
        """Probe multiple ports and paths, return first working sample and the url.
        """
        ports = ports or [30000, 30001, 8080, 80]
        paths = [
            "/api/overview",
            "/overview",
            "/api/status",
            "/status",
            "/api",
            "/",
        ]
        tried = []
        base_host = self.base_url
        # Als base_url al een poort bevat, probeer eerst die
        bases: list[str] = []
        try:
            # httpx kan geen urlparse hier, dus simpele check
            has_port = ":" in base_host.rsplit("/", 1)[-1]
        except Exception:
            has_port = False
        if has_port:
            bases.append(base_host)
        # Voeg combinaties met alternatieve poorten toe
        try:
            scheme, rest = base_host.split("://", 1)
        except ValueError:
            scheme, rest = "http", base_host
        host = rest.split("/", 1)[0]
        # Strip existing port if present
        host_only = host.split(":", 1)[0]
        for port in ports:
            bases.append(f"{scheme}://{host_only}:{port}")

        for b in bases:
            for p in paths:
                url = f"{b}{p}"
                tried.append(url)
                try:
                    async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
                        r = await client.get(url)
                        r.raise_for_status()
                        # Prefer JSON
                        try:
                            sample = r.json()
                        except ValueError:
                            sample = {"raw": r.text}
                        return {"ok": True, "hit": url, "sample": sample, "tried": tried}
                except Exception:
                    continue
        return {"ok": False, "error": "All connection attempts failed", "tried": tried}

    async def get_soc(self) -> Optional[float]:
        try:
            data = await self.get_overview()
            return float(data.get("soc")) if "soc" in data else None
        except Exception:
            return None

    async def get_power(self) -> Optional[int]:
        try:
            data = await self.get_overview()
            return int(data.get("batt_power")) if "batt_power" in data else None
        except Exception:
            return None

    # ---- Stuurcommando's (pas aan) ----
    async def inhibit_charge(self) -> bool:
        try:
            await self._post("/api/control", {"charge": "off"})
            return True
        except Exception:
            return False

    async def allow_charge(self) -> bool:
        try:
            await self._post("/api/control", {"charge": "on"})
            return True
        except Exception:
            return False

# =========================
# Helpers voor parsing
# =========================
def extract_grid_export_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """Grid export/import uit myenergi halen (positief = export)."""
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        # Cloud response is lijst van secties: {"eddi":[...]} {"zappi":[...]}
        if isinstance(raw, list):
            # Probeer eerst zappi[0]['grd'] (grid power). Negatief = import, positief = export.
            for section in raw:
                if isinstance(section, dict) and "zappi" in section:
                    arr = section.get("zappi") or []
                    if arr and isinstance(arr[0], dict) and "grd" in arr[0]:
                        grd = int(arr[0]["grd"])
                        return grd  # hier is al conventie: pos = export, neg = import
            # Fallback: eddi[0]['grd'] indien aanwezig
            for section in raw:
                if isinstance(section, dict) and "eddi" in section:
                    arr = section.get("eddi") or []
                    if arr and isinstance(arr[0], dict) and "grd" in arr[0]:
                        grd = int(arr[0]["grd"])
                        return grd
        else:
            # Oudere/lokale vorm: direct pgrid of status.pgrid
            items = raw if isinstance(raw, dict) else {}
            if "pgrid" in items:
                pgrid = int(items["pgrid"])  # vaak: + = import, - = export
                return -pgrid
    except Exception:
        pass
    return None

def extract_eddi_power_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """Eddi-vermogen (W)."""
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        if isinstance(raw, list):
            # Cloud: eddi[0]['ectp1'] (of 'div' = delivered power) is een goede benadering.
            for section in raw:
                if isinstance(section, dict) and "eddi" in section:
                    arr = section.get("eddi") or []
                    if arr and isinstance(arr[0], dict):
                        eddi = arr[0]
                        if "ectp1" in eddi:
                            return int(eddi["ectp1"])  # vermogen kanaal 1
                        if "div" in eddi:
                            return int(eddi["div"])    # delivered/imported power
        else:
            # Lokale/legacy: direct ectp of p
            items = raw if isinstance(raw, dict) else {}
            v = items.get("ectp") or items.get("p")
            return int(v) if v is not None else None
    except Exception:
        pass
    return None

def extract_zappi_power_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """Zappi-vermogen (W) - auto opladen."""
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        if isinstance(raw, list):
            # Cloud: zappi[0]['div'] = delivered power
            for section in raw:
                if isinstance(section, dict) and "zappi" in section:
                    arr = section.get("zappi") or []
                    if arr and isinstance(arr[0], dict):
                        zappi = arr[0]
                        if "div" in zappi:
                            return int(zappi["div"])    # delivered power
                        if "che" in zappi:  # charge added
                            return int(zappi["che"])
        else:
            # Lokale/legacy: direct div of che
            items = raw if isinstance(raw, dict) else {}
            v = items.get("div") or items.get("che")
            return int(v) if v is not None else None
    except Exception:
        pass
    return None

def extract_eddi_temperatures(myenergi_status: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Eddi tank temperaturen (°C)."""
    raw = myenergi_status.get("raw", myenergi_status)
    temps = {"tank1": None, "tank2": None}
    
    try:
        if isinstance(raw, list):
            # Cloud response
            for section in raw:
                if isinstance(section, dict) and "eddi" in section:
                    arr = section.get("eddi") or []
                    if arr and isinstance(arr[0], dict):
                        eddi = arr[0]
                        # Tank temperaturen: tp1, tp2 (in 0.1°C, dus delen door 10)
                        if "tp1" in eddi and eddi["tp1"] != -1:
                            temps["tank1"] = int(eddi["tp1"]) // 10
                        if "tp2" in eddi and eddi["tp2"] != -1:
                            temps["tank2"] = int(eddi["tp2"]) // 10
        else:
            # Lokale response
            items = raw if isinstance(raw, dict) else {}
            if "tp1" in items and items["tp1"] != -1:
                temps["tank1"] = int(items["tp1"]) // 10
            if "tp2" in items and items["tp2"] != -1:
                temps["tank2"] = int(items["tp2"]) // 10
                
    except Exception:
        pass
    
    return temps

def should_block_battery_for_priority(myenergi_status: Dict[str, Any], current_blocked: bool) -> tuple[bool, str]:
    """
    Bepaal of batterij geblokkeerd moet worden voor myenergi prioriteit.
    Prioriteit: Zappi > Eddi > Batterij
    Returns: (should_block, reason)
    """
    eddi_power = extract_eddi_power_w(myenergi_status) or 0
    zappi_power = extract_zappi_power_w(myenergi_status) or 0
    export_w = extract_grid_export_w(myenergi_status) or 0
    
    if EDDI_PRIORITY_MODE == "threshold":
        # Smart threshold-based management met hysterese
        
        # 1. Zappi heeft altijd voorrang (auto laden)
        if zappi_power > ZAPPI_ACTIVE_W:
            return True, f"Zappi active: {zappi_power}W > {ZAPPI_ACTIVE_W}W (auto charging priority)"
        
        # 2. Bereken totale reserves (Zappi + Eddi)
        total_reserve = EDDI_RESERVE_W
        if zappi_power > 0:  # Zappi wil laden maar is niet actief genoeg
            total_reserve += ZAPPI_RESERVE_W
        
        # 3. Hysterese om toggle te voorkomen
        if current_blocked:
            # Batterij is UIT → hogere drempel om AAN te gaan (anti-toggle)
            min_export = BATTERY_MIN_EXPORT_W + BATTERY_HYSTERESIS_W
            if export_w < min_export:
                return True, f"Export {export_w}W < battery minimum+hysteresis {min_export}W"
        else:
            # Batterij is AAN → lagere drempel om UIT te gaan (anti-toggle)  
            min_export = BATTERY_MIN_EXPORT_W - BATTERY_HYSTERESIS_W
            if export_w < min_export:
                return True, f"Export {export_w}W < battery minimum-hysteresis {min_export}W"
        
        # 4. Check reserves
        if export_w < total_reserve:
            devices = ["Eddi"]
            if zappi_power > 0:
                devices.insert(0, "Zappi")
            return True, f"Export {export_w}W < {'+'.join(devices)} reserve {total_reserve}W"
        
        return False, f"Export {export_w}W sufficient (Zappi:{zappi_power}W, Eddi:{eddi_power}W)"
    
    elif EDDI_PRIORITY_MODE == "power":
        # Power-based: Eddi gebruikt stroom → batterij blokkeren
        if eddi_power > EDDI_ACTIVE_W:
            return True, f"Eddi active: {eddi_power}W > {EDDI_ACTIVE_W}W"
        return False, f"Eddi idle: {eddi_power}W ≤ {EDDI_ACTIVE_W}W"
    
    elif EDDI_PRIORITY_MODE == "temp":
        # Temperature-based: Tank(s) niet op temperatuur → batterij blokkeren
        temps = extract_eddi_temperatures(myenergi_status)
        
        reasons = []
        should_block = False
        
        if EDDI_USE_TANK_1 and temps["tank1"] is not None:
            if temps["tank1"] < EDDI_TARGET_TEMP_1:
                should_block = True
                reasons.append(f"Tank1: {temps['tank1']}°C < {EDDI_TARGET_TEMP_1}°C")
            else:
                reasons.append(f"Tank1: {temps['tank1']}°C ≥ {EDDI_TARGET_TEMP_1}°C")
        
        if EDDI_USE_TANK_2 and temps["tank2"] is not None:
            if temps["tank2"] < EDDI_TARGET_TEMP_2:
                should_block = True
                reasons.append(f"Tank2: {temps['tank2']}°C < {EDDI_TARGET_TEMP_2}°C")
            else:
                reasons.append(f"Tank2: {temps['tank2']}°C ≥ {EDDI_TARGET_TEMP_2}°C")
        
        if not reasons:
            return False, "No tank temperatures available"
        
        reason = "Eddi tanks: " + ", ".join(reasons)
        return should_block, reason
    
    else:
        return False, f"Unknown priority mode: {EDDI_PRIORITY_MODE}"

# =========================
# Regelaartje (state machine)
# =========================
class ControllerState:
    def __init__(self):
        self.battery_blocked: bool = False
        self.last_switch: float = 0.0
        self.export_over_threshold_since: Optional[float] = None

    def cooldown_ok(self) -> bool:
        return (time.time() - self.last_switch) > MIN_SWITCH_COOLDOWN_S

    def mark_switch(self):
        self.last_switch = time.time()

state = ControllerState()

# =========================
# FastAPI app
# =========================
from fastapi.staticfiles import StaticFiles
app = FastAPI(title="myenergi-marstek-autocontrol")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve de lokale BLE tool (geclonede repo) op /ble
try:
    app.mount("/ble", StaticFiles(directory="external/marstek-venus-monitor", html=True), name="ble")
except Exception:
    # Niet fataal als map ontbreekt
    pass

# Serve explicit BLE v1 (original) as its own endpoint so you can click a link
from pathlib import Path

@app.get("/ble-legacy")
async def ble_legacy():
    try:
        p = Path("external/marstek-venus-monitor/index.html.original")
        return HTMLResponse(p.read_text(encoding="utf-8"))
    except Exception as e:
        return HTMLResponse(f"<pre>BLE v1 not found: {e}</pre>", status_code=500)

@app.get("/ble/set-meter-ip")
async def ble_set_meter_ip_page():
    html = """
    <!doctype html>
    <html lang=\"nl\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>BLE: Set Meter IP</title>
      <style>
        body { font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }
        .card { background:#111827; border:1px solid #374151; border-radius:12px; padding:16px; margin:12px 0; }
        label { display:block; margin-top:8px; color:#cbd5e1; }
        input { width:100%; padding:8px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0; }
        button { background:#2563eb; color:#fff; border:0; padding:8px 12px; border-radius:8px; cursor:pointer; margin-top:12px; }
        .row { display:flex; gap:12px; flex-wrap:wrap; }
        pre { white-space:pre-wrap; word-break:break-word; background:#0b1220; padding:12px; border-radius:8px; border:1px solid #1f2937; }
      </style>
    </head>
    <body>
      <h1>BLE: Set Meter IP (0x21)</h1>
      <div class=\"card\">
        <div class=\"row\">
          <button onclick=\"connect()\">🔗 Connect (select MST_ACCP_...)</button>
          <button onclick=\"disconnect()\">Disconnect</button>
        </div>
        <label>Meter IP</label>
        <input id=\"meter_ip\" placeholder=\"192.168.68.73\" value=\"192.168.68.73\" />
        <div class=\"row\">
          <button onclick=\"writeIP()\">🌐 Write Meter IP (0x21, 0x0A)</button>
          <button onclick=\"readIP()\">📖 Read Meter IP (0x21, 0x0B)</button>
        </div>
        <div id=\"msg\"></div>
        <pre id=\"log\"></pre>
      </div>

      <script src=\"/ble/js/ui-controller.js\"></script>
      <script src=\"/ble/js/ble-protocol.js\"></script>
      <script>
        function logAppend(s){ const el = document.getElementById('log'); el.textContent += s + "\n"; el.scrollTop = el.scrollHeight; }
        async function writeIP(){
          const ip = document.getElementById('meter_ip').value.trim();
          if(!ip){ document.getElementById('msg').textContent='Vul IP in'; return; }
          const ok = /^\d{1,3}(\.\d{1,3}){3}$/.test(ip);
          if(!ok){ document.getElementById('msg').textContent='Ongeldig IP'; return; }
          const ascii = Array.from(new TextEncoder().encode(ip));
          const payload = [0x0A, ...ascii];
          try{ await sendCommand(0x21, 'Write Custom Meter IP', payload); document.getElementById('msg').textContent='Geschreven'; }
          catch(e){ document.getElementById('msg').textContent='Fout: '+e; }
        }
        async function readIP(){
          try{ await sendCommand(0x21, 'Read Meter IP', [0x0B]); document.getElementById('msg').textContent='Gelezen (zie log in UI)'; }
          catch(e){ document.getElementById('msg').textContent='Fout: '+e; }
        }
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.get("/ble-set-meter-ip")
async def ble_set_meter_ip_page2():
    # Same page, different route outside /ble to avoid static mount shadowing
    html = """
    <!doctype html>
    <html lang=\"nl\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>BLE: Set Meter IP</title>
      <style>
        body { font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }
        .card { background:#111827; border:1px solid #374151; border-radius:12px; padding:16px; margin:12px 0; }
        label { display:block; margin-top:8px; color:#cbd5e1; }
        input { width:100%; padding:8px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0; }
        button { background:#2563eb; color:#fff; border:0; padding:8px 12px; border-radius:8px; cursor:pointer; margin-top:12px; }
        .row { display:flex; gap:12px; flex-wrap:wrap; }
        pre { white-space:pre-wrap; word-break:break-word; background:#0b1220; padding:12px; border-radius:8px; border:1px solid #1f2937; }
      </style>
    </head>
    <body>
      <h1>BLE: Set Meter IP (0x21)</h1>
      <div class=\"card\">
        <div class=\"row\">
          <button onclick=\"connect()\">🔗 Connect (select MST_ACCP_...)</button>
          <button onclick=\"disconnect()\">Disconnect</button>
        </div>
        <label>Meter IP</label>
        <input id=\"meter_ip\" placeholder=\"192.168.68.73\" value=\"192.168.68.73\" />
        <div class=\"row\">
          <button onclick=\"writeIP()\">🌐 Write Meter IP (0x21, 0x0A)</button>
          <button onclick=\"readIP()\">📖 Read Meter IP (0x21, 0x0B)</button>
        </div>
        <div id=\"msg\"></div>
        <pre id=\"log\"></pre>
      </div>

      <script src=\"/ble/js/ui-controller.js\"></script>
      <script src=\"/ble/js/ble-protocol.js\"></script>
      <script>
        function logAppend(s){ const el = document.getElementById('log'); el.textContent += s + "\n"; el.scrollTop = el.scrollHeight; }
        async function writeIP(){
          const ip = document.getElementById('meter_ip').value.trim();
          if(!ip){ document.getElementById('msg').textContent='Vul IP in'; return; }
          const ok = /^\d{1,3}(\.\d{1,3}){3}$/.test(ip);
          if(!ok){ document.getElementById('msg').textContent='Ongeldig IP'; return; }
          const ascii = Array.from(new TextEncoder().encode(ip));
          const payload = [0x0A, ...ascii];
          try{ await sendCommand(0x21, 'Write Custom Meter IP', payload); document.getElementById('msg').textContent='Geschreven'; }
          catch(e){ document.getElementById('msg').textContent='Fout: '+e; }
        }
        async function readIP(){
          try{ await sendCommand(0x21, 'Read Meter IP', [0x0B]); document.getElementById('msg').textContent='Gelezen (zie log in UI)'; }
          catch(e){ document.getElementById('msg').textContent='Fout: '+e; }
        }
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

myenergi = MyEnergiClient(MYENERGI_BASE_URL, MYENERGI_HUB_SERIAL, MYENERGI_API_KEY)
marstek  = MarstekClient(MARSTEK_BASE_URL, MARSTEK_API_TOKEN)

@app.on_event("startup")
async def _startup():
    # Start background loop
    asyncio.create_task(control_loop())

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/api/status")
async def get_status():
    """Samengevoegde status van myenergi + marstek."""
    try:
        m = await myenergi.status_all()
        soc = await marstek.get_soc()
        power = await marstek.get_power()
        export_w = extract_grid_export_w(m)
        eddi_w = extract_eddi_power_w(m)
        zappi_w = extract_zappi_power_w(m)
        eddi_temps = extract_eddi_temperatures(m)
        should_block, block_reason = should_block_battery_for_priority(m, state.battery_blocked)
        
        return {
            "timestamp": time.time(),
            "myenergi_raw": m,
            "grid_export_w": export_w,
            "eddi_power_w": eddi_w,
            "zappi_power_w": zappi_w,
            "eddi_temperatures": eddi_temps,
            "should_block": should_block,
            "block_reason": block_reason,
            "marstek_soc": soc,
            "marstek_power_w": power,
            "battery_blocked": state.battery_blocked,
            "last_switch": state.last_switch,
            "config": {
                "priority_mode": EDDI_PRIORITY_MODE,
                "target_temp_1": EDDI_TARGET_TEMP_1,
                "target_temp_2": EDDI_TARGET_TEMP_2,
                "use_tank_1": EDDI_USE_TANK_1,
                "use_tank_2": EDDI_USE_TANK_2,
                "active_threshold_w": EDDI_ACTIVE_W,
                "marstek_use_ble": MARSTEK_USE_BLE
            }
        }
    except Exception as e:
        return {"error": str(e), "timestamp": time.time()}

@app.get("/dashboard")
async def live_dashboard():
    """Live monitoring dashboard"""
    with open("dashboard.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/")
async def dashboard():
    html = f"""
    <!doctype html>
    <html lang=\"nl\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>myenergi ↔ marstek</title>
      <style>
        body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
        .card {{ background:#111827; border:1px solid #374151; border-radius:12px; padding:16px; margin:12px 0; }}
        .row {{ display:flex; gap:12px; flex-wrap:wrap; }}
        .kpi {{ flex:1; min-width:220px; }}
        .label {{ color:#94a3b8; font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
        .value {{ font-size:28px; font-weight:700; margin-top:6px; }}
        .ok {{ color:#22c55e }} .warn {{ color:#f59e0b }} .bad {{ color:#ef4444 }}
        button {{ background:#2563eb; color:#fff; border:0; padding:8px 12px; border-radius:8px; cursor:pointer; }}
        button.secondary {{ background:#334155; }}
        pre {{ white-space:pre-wrap; word-break:break-word; background:#0b1220; padding:12px; border-radius:8px; border:1px solid #1f2937; }}
      </style>
    </head>
    <body>
      <h1>myenergi ↔ marstek</h1>
      <div style=\"margin:8px 0\">
        <a href=\"/setup\" style=\"color:#93c5fd\">⚙️ Setup</a>
        &nbsp;•&nbsp;
        <a href=\"/ble/\" style=\"color:#93c5fd\">🔗 BLE-tool (lokaal)</a>
        &nbsp;•&nbsp;
        <a href=\"/ble-set-meter-ip\" style=\"color:#93c5fd\">🌐 Set Meter IP (0x21)</a>
        &nbsp;•&nbsp;
        <a href=\"/ble-legacy\" style=\"color:#93c5fd\">🕰️ BLE v1 (legacy)</a>
      </div>
      <div id=\"msg\"></div>
      <div class=\"row\">
        <div class=\"card kpi\">
          <div class=\"label\">Grid</div>
          <div class=\"value\" id=\"grid\">—</div>
        </div>
        <div class=\"card kpi\">
          <div class=\"label\">Eddi vermogen</div>
          <div class=\"value\" id=\"eddi\">—</div>
        </div>
        <div class=\"card kpi\">
          <div class=\"label\">Batterij SoC</div>
          <div class=\"value\" id=\"soc\">—</div>
        </div>
        <div class=\"card kpi\">
          <div class=\"label\">Batterij status</div>
          <div class=\"value\" id=\"blocked\">—</div>
        </div>
      </div>
      <div class=\"card\">
        <div class=\"row\">
          <button onclick=\"send('allow')\">Allow charge</button>
          <button class=\"secondary\" onclick=\"send('inhibit')\">Inhibit charge</button>
          <button class=\"secondary\" onclick=\"send('status')\">Refresh status</button>
        </div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Ruwe data</div>
        <pre id=\"raw\"></pre>
      </div>
      <div class=\"card\">
        <div class=\"label\">Eddi details</div>
        <div id=\"eddi_details\"></div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Zappi details</div>
        <div id=\"zappi_details\"></div>
      </div>

      <script>
        async function refresh() {{
          try {{
            const r = await fetch('/api/status');
            const j = await r.json();
            const ge = j.derived.grid_export_w;
            const ed = j.derived.eddi_power_w;
            document.getElementById('grid').textContent =
              ge == null ? '—' : `${{ge}} W`;
            document.getElementById('grid').className = 'value ' + (ge == null ? '' : (ge >= 0 ? 'ok' : 'bad'));
            document.getElementById('eddi').textContent = ed == null ? '—' : `${{ed}} W`;
            document.getElementById('soc').textContent = j.battery.soc == null ? '—' : `${{j.battery.soc}} %`;
            document.getElementById('blocked').textContent = j.battery.blocked ? 'Geblokkeerd' : 'Toegestaan';
            document.getElementById('raw').textContent = JSON.stringify(j, null, 2);

            // Eddi/Zappi detail parsing (cloud raw)
            try {{
              let eddi = null, zappi = null;
              if (Array.isArray(j.myenergi.raw)) {{
                for (const sect of j.myenergi.raw) {{
                  if (sect.eddi && sect.eddi.length) eddi = sect.eddi[0];
                  if (sect.zappi && sect.zappi.length) zappi = sect.zappi[0];
                }}
              }}
              const eddiHtml = eddi ? `
                <ul>
                  <li><b>SN</b>: ${{eddi.sno ?? '—'}}</li>
                  <li><b>Vermogen</b>: ${{(eddi.ectp1 ?? eddi.div ?? '—')}} W</li>
                  <li><b>T1</b>: ${{eddi.tp1 ?? '—'}} °C</li>
                  <li><b>T2</b>: ${{eddi.tp2 ?? '—'}} °C</li>
                  <li><b>Spanning</b>: ${{eddi.vol ? (eddi.vol/10).toFixed(1)+' V' : '—'}}</li>
                  <li><b>Status</b>: ${{eddi.sta ?? '—'}}</li>
                </ul>` : '—';
              document.getElementById('eddi_details').innerHTML = eddiHtml;

              const zappiHtml = zappi ? `
                <ul>
                  <li><b>SN</b>: ${{zappi.sno ?? '—'}}</li>
                  <li><b>Grid</b>: ${{zappi.grd ?? '—'}} W</li>
                  <li><b>Gen</b>: ${{zappi.gen ?? '—'}} W</li>
                  <li><b>Spanning</b>: ${{zappi.vol ? (zappi.vol/10).toFixed(1)+' V' : '—'}}</li>
                  <li><b>Fase</b>: ${{zappi.phaseSetting ?? zappi.pha ?? '—'}}</li>
                  <li><b>Mode</b>: ${{zappi.zmo ?? '—'}}</li>
                </ul>` : '—';
              document.getElementById('zappi_details').innerHTML = zappiHtml;
            }} catch (e) {{ /* negeer parsing fouten */ }}
          }} catch(e) {{
            document.getElementById('msg').textContent = 'Fout bij ophalen status: ' + e;
          }}
        }}
        async function send(action) {{
          try {{
            const r = await fetch('/api/control?action=' + action, {{ method: 'POST' }});
            const j = await r.json();
            document.getElementById('msg').textContent = JSON.stringify(j);
            refresh();
          }} catch(e) {{
            document.getElementById('msg').textContent = 'Fout bij control: ' + e;
          }}
        }}
        refresh();
        setInterval(refresh, {int(POLL_INTERVAL_S*1000)});
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

# =========================
# Setup wizard (zonder externe site)
# =========================
@app.get("/setup")
async def setup_page():
    html = f"""
    <!doctype html>
    <html lang=\"nl\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>Marstek Setup</title>
      <style>
        body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
        .card {{ background:#111827; border:1px solid #374151; border-radius:12px; padding:16px; margin:12px 0; }}
        label {{ display:block; margin-top:8px; color:#cbd5e1; }}
        input {{ width:100%; padding:8px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:#e2e8f0; }}
        button {{ background:#2563eb; color:#fff; border:0; padding:8px 12px; border-radius:8px; cursor:pointer; margin-top:12px; }}
        .row {{ display:flex; gap:12px; flex-wrap:wrap; }}
        pre {{ white-space:pre-wrap; word-break:break-word; background:#0b1220; padding:12px; border-radius:8px; border:1px solid #1f2937; }}
      </style>
    </head>
    <body>
      <h1>Marstek Setup (lokaal)</h1>
      <div class="card">
        <h3>Netwerk scan (snel alle poorten proberen)</h3>
        <p>Scan het opgegeven IP met jouw eigen poorten (komma-gescheiden). Laat leeg voor standaardlijst.</p>
        <label>IP(s) (comma-sep)</label>
        <input id="scan_ip" placeholder="192.168.68.72,192.168.68.73,192.168.68.74,192.168.68.75" value="192.168.68.72" />
        <label>Poorten (comma-sep)</label>
        <input id="scan_ports" placeholder="30000,30001,8080,80,30002" value="30000,30001,8080,80,30002" />
        <div class="row">
          <button onclick="scanPorts()">Scan poorten</button>
        </div>
      </div>
      <div class="card">
        <div id="scan_result"></div>
      </div>
      <div class="card">
        <p>Voer het lokale IP en poort van je Marstek in (bijv. 30000) en test de verbinding. Dit blijft op je eigen netwerk.</p>
        <label>IP of host</label>
        <input id="ip" placeholder="192.168.x.y" />
        <label>Poort</label>
        <input id="port" placeholder="30000" value="30000" />
        <label>Token (optioneel)</label>
        <input id="token" placeholder="(laat leeg indien niet nodig)" />
        <div class="row">
          <button onclick="testConn()">Test verbinding</button>
          <button onclick="saveCfg()">Opslaan</button>
        </div>
      </div>
      <div class=\"card\">
        <div id=\"result\"></div>
        <pre id=\"preview\"></pre>
      </div>
      <script>
        async function scanPorts() {{
          const ipsStr = document.getElementById('scan_ip').value.trim();
          if (!ipsStr) {{ document.getElementById('scan_result').textContent = 'Vul IP(s) in'; return; }}
          const ips = ipsStr.split(',').map(s => s.trim()).filter(Boolean);
          const portsStr = (document.getElementById('scan_ports').value || '').trim();
          let ports = undefined;
          if (portsStr) {{
            ports = portsStr.split(',').map(s => parseInt(s.trim(), 10)).filter(n => Number.isInteger(n) && n>0 && n<65536);
            if (!ports.length) ports = undefined;
          }}
          document.getElementById('scan_result').textContent = 'Scanning...';
          try {{
            const r = await fetch('/api/marstek/scan', {{
              method: 'POST', headers: {{'Content-Type':'application/json'}},
              body: JSON.stringify({{ips: ips, ports: ports}})
            }});
            const j = await r.json();
            document.getElementById('scan_result').innerHTML =
              j.ok ? `<pre>${{JSON.stringify(j, null, 2)}}</pre>` : `Mislukt: ${{j.error}}`;
            // Vul ook het IP-veld
            if (ips && ips.length) document.getElementById('ip').value = ips[0];
          }} catch(e) {{ document.getElementById('scan_result').textContent = 'Fout: ' + e; }}
        }}
        async function testConn() {{
          const ip = document.getElementById('ip').value.trim();
          const port = document.getElementById('port').value.trim();
          const token = document.getElementById('token').value.trim();
          if (!ip || !port) {{ document.getElementById('result').textContent = 'Vul IP en poort in'; return; }}
          const base = `http://${{ip}}:${{port}}`;
          try {{
            const r = await fetch('/api/marstek/test', {{
              method: 'POST', headers: {{'Content-Type':'application/json'}},
              body: JSON.stringify({{ base_url: base, token }})
            }});
            const j = await r.json();
            document.getElementById('result').textContent = j.ok ? 'Verbinding OK' : ('Mislukt: ' + (j.error||''));
            document.getElementById('preview').textContent = JSON.stringify(j.sample||j, null, 2);
          }} catch(e) {{ document.getElementById('result').textContent = 'Fout: ' + e; }}
        }}
        async function saveCfg() {{
          const ip = document.getElementById('ip').value.trim();
          const port = document.getElementById('port').value.trim();
          const token = document.getElementById('token').value.trim();
          if (!ip || !port) {{ document.getElementById('result').textContent = 'Vul IP en poort in'; return; }}
          const base = `http://${{ip}}:${{port}}`;
          try {{
            const r = await fetch('/api/marstek/config', {{
              method: 'POST', headers: {{'Content-Type':'application/json'}},
              body: JSON.stringify({{ base_url: base, token }})
            }});
            const j = await r.json();
            document.getElementById('result').textContent = j.ok ? 'Opgeslagen' : ('Mislukt: ' + (j.error||''));
          }} catch(e) {{ document.getElementById('result').textContent = 'Fout: ' + e; }}
        }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@app.post("/api/marstek/test")
async def marstek_test(payload: Dict[str, str] = Body(...)):
    base = (payload.get("base_url") or "").rstrip("/")
    token = payload.get("token") or ""
    temp = MarstekClient(base, token)
    # Probeer uitgebreid te scannen naar juiste poort/pad
    result = await temp.probe()
    if result.get("ok"):
        return result
    # Fallback: enkel get_overview op exact base
    try:
        data = await temp.get_overview()
        return {"ok": True, "hit": f"{base}", "sample": data}
    except Exception as e:
        return {"ok": False, "error": str(e), "tried": result.get("tried")}

@app.post("/api/marstek/scan")
async def marstek_scan(payload: Dict[str, Any] = Body(...)):
    # Accept either a single 'ip' or a list of 'ips'
    ip_single = (payload.get("ip") or "").strip()
    ips_list = payload.get("ips") or ([] if not ip_single else [ip_single])
    if not ips_list:
        return {"ok": False, "error": "IP(s) ontbreken"}

    # Optional custom ports list, else default
    custom_ports = payload.get("ports")
    if isinstance(custom_ports, list):
        try:
            ports = [int(p) for p in custom_ports if int(p) > 0 and int(p) < 65536]
        except Exception:
            ports = [30000, 30001, 8080, 80, 30002]
        if not ports:
            ports = [30000, 30001, 8080, 80, 30002]
    else:
        ports = [30000, 30001, 8080, 80, 30002]

    paths = [
        "/api/overview",
        "/overview",
        "/api/status",
        "/status",
        "/api",
        "/",
    ]

    all_results: Dict[str, Any] = {"ok": False, "results": []}

    for ip in ips_list:
        ip = (ip or "").strip()
        if not ip:
            continue
        ip_results = []
        for port in ports:
            base = f"http://{ip}:{port}"
            for path in paths:
                url = f"{base}{path}"
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        r = await client.get(url)
                        r.raise_for_status()
                        # Try JSON
                        try:
                            sample = r.json()
                            ip_results.append({
                                "url": url,
                                "status": r.status_code,
                                "sample": sample,
                                "type": "json"
                            })
                        except ValueError:
                            # Plain text
                            sample = r.text.strip()
                            if sample:
                                ip_results.append({
                                    "url": url,
                                    "status": r.status_code,
                                    "sample": sample,
                                    "type": "text"
                                })
                except Exception:
                    continue
        all_results["results"].append({
            "ip": ip,
            "open_ports": ip_results,
            "tried_ports": ports,
            "tried_paths": paths,
        })

    # ok = True if any ip had hits
    any_hits = any(r.get("open_ports") for r in all_results["results"]) if all_results["results"] else False
    all_results["ok"] = bool(any_hits)
    if not any_hits:
        all_results["error"] = "Geen open poorten/paden gevonden"
    return all_results

# =========================
# Control loop
# =========================
async def control_loop():
    """
    Nieuwe kernlogica met Eddi prioriteit:
      - Eddi heeft ALTIJD voorrang op batterijen
      - Power mode: Eddi gebruikt stroom → batterij blokkeren
      - Temp mode: Tank(s) niet op temperatuur → batterij blokkeren  
      - Failsafe: SoC < minimum → batterij toestaan (bescherming)
      - Configureerbaar per seizoen (tank 1/2, temperaturen)
    """
    while True:
        try:
            m = await myenergi.status_all()
            soc = await marstek.get_soc()
            export_w = extract_grid_export_w(m)  # >0 = export
            now = time.time()

            # Failsafe: Batterij beschermen bij lage SoC
            if soc is not None and soc < SOC_FAILSAFE_MIN:
                if state.battery_blocked and state.cooldown_ok():
                    ok = await marstek.allow_charge()
                    if ok:
                        state.battery_blocked = False
                        state.mark_switch()
                        print(f"🔋 Failsafe: Battery allowed (SoC: {soc}% < {SOC_FAILSAFE_MIN}%)")
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Hoofdlogica: myenergi prioriteit (Zappi > Eddi > Batterij)
            should_block, reason = should_block_battery_for_priority(m, state.battery_blocked)

            # Batterij blokkeren voor Eddi prioriteit
            if should_block:
                if not state.battery_blocked and state.cooldown_ok():
                    ok = await marstek.inhibit_charge()
                    if ok:
                        state.battery_blocked = True
                        state.mark_switch()
                        print(f"🚫 Battery blocked: {reason}")
                state.export_over_threshold_since = None
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Eddi heeft geen prioriteit → batterij mag laden bij voldoende export
            if export_w is not None and export_w > EXPORT_ENOUGH_W:
                if state.export_over_threshold_since is None:
                    state.export_over_threshold_since = now
            else:
                state.export_over_threshold_since = None

            stable_ok = (
                state.export_over_threshold_since is not None and
                (now - state.export_over_threshold_since) >= STABLE_EXPORT_SECONDS
            )

            if stable_ok and state.battery_blocked and state.cooldown_ok():
                ok = await marstek.allow_charge()
                if ok:
                    state.battery_blocked = False
                    state.mark_switch()
                    print(f"✅ Battery allowed: {reason}, stable export {export_w}W")

        except Exception:
            # Rustig blijven bij netwerkfout; volgende tick opnieuw
            pass

        await asyncio.sleep(POLL_INTERVAL_S)

# =========================
# BLE Endpoints
# =========================
@app.get("/api/ble/status")
async def ble_battery_status():
    """Get battery status via integrated BLE"""
    if not BLE_AVAILABLE:
        return {"error": "BLE not available", "available": False}
    
    try:
        ble_client = get_ble_client()
        status = await ble_client.get_battery_status()
        return status
    except Exception as e:
        return {"error": str(e), "available": True}

@app.get("/api/ble/info")
async def ble_system_info():
    """Get BLE system information"""
    if not BLE_AVAILABLE:
        return {"error": "BLE not available", "available": False}
    
    try:
        ble_client = get_ble_client()
        info = await ble_client.get_system_info()
        return info
    except Exception as e:
        return {"error": str(e), "available": True}

@app.post("/api/ble/connect")
async def ble_connect():
    """Manually trigger BLE connection"""
    if not BLE_AVAILABLE:
        return {"success": False, "error": "BLE not available"}
    
    try:
        ble_client = get_ble_client()
        success = await ble_client.connect()
        return {"success": success, "connected": ble_client.is_connected}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/ble/disconnect")
async def ble_disconnect():
    """Manually disconnect BLE"""
    if not BLE_AVAILABLE:
        return {"success": False, "error": "BLE not available"}
    
    try:
        ble_client = get_ble_client()
        await ble_client.disconnect()
        return {"success": True, "connected": ble_client.is_connected}
    except Exception as e:
        return {"success": False, "error": str(e)}

# =========================
# App lifecycle
# =========================
@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    print("🚀 Starting myenergi-marstek integration...")
    
    if MARSTEK_USE_BLE and BLE_AVAILABLE:
        print("🔵 BLE mode enabled - will use integrated BLE client")
        # Pre-initialize BLE client
        try:
            ble_client = get_ble_client()
            await ble_client.discover_device()
        except Exception as e:
            print(f"⚠️  BLE initialization warning: {e}")
    
    # Start background control loop
    asyncio.create_task(control_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    print("🛑 Shutting down myenergi-marstek integration...")
    
    if BLE_AVAILABLE:
        try:
            await cleanup_ble_client()
        except Exception as e:
            print(f"⚠️  BLE cleanup warning: {e}")

# =========================
# Manual Control Endpoints
# =========================
@app.post("/api/marstek/allow")
async def marstek_allow_manual():
    """Handmatig batterij toestaan"""
    try:
        result = await marstek.allow_charge()
        if result:
            state.battery_blocked = False
            state.mark_switch()
            print("✅ Manual battery allow")
        return {"ok": result, "action": "allow", "timestamp": time.time()}
    except Exception as e:
        return {"ok": False, "error": str(e), "action": "allow"}

@app.post("/api/marstek/inhibit")
async def marstek_inhibit_manual():
    """Handmatig batterij blokkeren"""
    try:
        result = await marstek.inhibit_charge()
        if result:
            state.battery_blocked = True
            state.mark_switch()
            print("🚫 Manual battery block")
        return {"ok": result, "action": "inhibit", "timestamp": time.time()}
    except Exception as e:
        return {"ok": False, "error": str(e), "action": "inhibit"}
