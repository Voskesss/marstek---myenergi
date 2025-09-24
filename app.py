import os
import logging

# Logging configuration (must run after importing os/logging)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "")

if not logging.getLogger().handlers:
    handlers = []
    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname)s %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    if LOG_FILE:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=handlers)

logger = logging.getLogger("myenergi-marstek")

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
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, BackgroundTasks, Request, Query, Body, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymodbus.client import ModbusTcpClient
from venus_e_register_map import format_value, get_all_sensors
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

# Battery capacity (kWh) for SoC → kWh calculations
BATTERY_FULL_KWH      = float(os.getenv("BATTERY_FULL_KWH", "5.12"))
# Minimum SoC reserve (%) that must remain in the battery (manual/auto rules)
MIN_SOC_RESERVE       = int(os.getenv("MIN_SOC_RESERVE", "10"))

USER_AGENT = {"User-Agent": "Wget/1.14 (linux-gnu)"}

# =========================
# Modbus Client for Venus E Battery 78
# =========================
class VenusEModbusClient:
    def __init__(self, host=None, port=None):
        env_host = os.getenv('VENUS_MODBUS_HOST')
        env_port = os.getenv('VENUS_MODBUS_PORT')
        self.host = (host or env_host or '192.168.68.92')
        try:
            self.port = int(port or env_port or 502)
        except Exception:
            self.port = 502
        self.client = None
        self.connected = False
    
    def connect(self):
        try:
            # Add a short timeout to avoid hanging sockets
            self.client = ModbusTcpClient(self.host, port=self.port, timeout=2)
            self.connected = self.client.connect()
            return self.connected
        except Exception as e:
            logging.error(f"Modbus connection error: {e}")
            return False
    
    def disconnect(self):
        if self.client:
            self.client.close()
            self.connected = False

    def read_battery_data(self):
        """Read all battery data from Venus E via Modbus"""
        if not self.connected:
            if not self.connect():
                return None

        battery_data = {}

        # Read key registers (preferred v2 mapping + a few legacy extras)
        registers = {
            32104: "soc_percent",      # %
            32100: "battery_voltage",  # V
            32101: "battery_current",  # A (signed)
            32102: "battery_power",    # W (signed)
            35100: "work_mode",        # enum
            # Legacy/extras we still show if available
            30006: "system_status",
            30008: "cycle_count",
            30010: "internal_temp",
        }
        
        for reg_addr, param_name in registers.items():
            try:
                result = self.client.read_holding_registers(address=reg_addr, count=1, slave=1)
                if (not hasattr(result, 'registers')) or result.isError():
                    # retry once after reconnect
                    self.disconnect()
                    if self.connect():
                        result = self.client.read_holding_registers(address=reg_addr, count=1, slave=1)
                
                if hasattr(result, 'registers') and not result.isError():
                    raw_value = result.registers[0]
                    formatted = format_value(reg_addr, raw_value)
                    
                    battery_data[param_name] = {
                        "value": formatted.get("value", raw_value),
                        "formatted": formatted.get("formatted", str(raw_value)),
                        "unit": formatted.get("unit", ""),
                        "description": formatted.get("description", param_name),
                        "register": reg_addr,
                        "timestamp": datetime.now().isoformat()
                    }
                    
            except Exception as e:
                logging.error(f"Error reading register {reg_addr}: {e}")
        
        return battery_data

    # -------------------------
    # Control helpers (holding registers)
    # -------------------------
    def write_holding(self, address: int, value: int) -> tuple[bool, list[dict]]:
        attempts: list[dict] = []
        try:
            if not self.connected and not self.connect():
                return False, attempts
            # Try common unit IDs (1, 0, 247) and both keyword styles (unit/slave)
            for unit in (1, 0, 247):
                # First try 'unit='
                ok = False
                err = None
                try:
                    rr = self.client.write_register(address=address, value=value, unit=unit)
                    ok = (not getattr(rr, 'isError', lambda: False)())
                except Exception as ex:
                    err = str(ex)
                attempts.append({"unit": unit, "style": "unit", "ok": ok, "error": err})
                if ok:
                    return True, attempts
                # Then try 'slave='
                ok2 = False
                err2 = None
                try:
                    rr2 = self.client.write_register(address=address, value=value, slave=unit)
                    ok2 = (not getattr(rr2, 'isError', lambda: False)())
                except Exception as ex2:
                    err2 = str(ex2)
                attempts.append({"unit": unit, "style": "slave", "ok": ok2, "error": err2})
                if ok2:
                    return True, attempts
            return False, attempts
        except Exception as e:
            logging.error(f"Modbus write error @ {address}: {e}")
            attempts.append({"unit": None, "ok": False, "error": str(e)})
            return False, attempts

    def set_control(self, action: str, power_w: Optional[int] = None) -> dict:
        """High-level control for charge/discharge/stop using control map:
        - 42000 rs485_control_enable: 1 = enable
        - 42001 user_work_mode: 1 = Manual (optional)
        - 42002 force_charge_power (W)
        - 42003 force_discharge_power (W)
        - 42008 force_charge_discharge: 0 Stop, 1 Charge, 2 Discharge
        """
        result = {"ok": False, "attempts": []}
        try:
            if power_w is None:
                power_w = 0
            power_w = max(0, int(power_w))

            # Ensure connection
            if not self.connected and not self.connect():
                result["error"] = "connect failed"
                return result

            def do_command(primary: bool = True) -> tuple[bool, list[dict]]:
                all_tries: list[dict] = []
                if primary:
                    # Primary map: 42002/42003 (power), 42008 (command)
                    if action == "stop":
                        ok, tries = self.write_holding(42008, 0)
                        all_tries += [{"addr": 42008, **t} for t in tries]
                        return ok, all_tries
                    if action == "charge":
                        okp, triesp = self.write_holding(42002, power_w)
                        all_tries += [{"addr": 42002, **t} for t in triesp]
                        okc, triesc = self.write_holding(42008, 1)
                        all_tries += [{"addr": 42008, **t} for t in triesc]
                        return (okp and okc), all_tries
                    if action == "discharge":
                        okp, triesp = self.write_holding(42003, power_w)
                        all_tries += [{"addr": 42003, **t} for t in triesp]
                        okd, triesd = self.write_holding(42008, 2)
                        all_tries += [{"addr": 42008, **t} for t in triesd]
                        return (okp and okd), all_tries
                else:
                    # Alternate map: 42010/42011 (power), 42020 (command)
                    if action == "stop":
                        ok, tries = self.write_holding(42020, 0)
                        all_tries += [{"addr": 42020, **t} for t in tries]
                        return ok, all_tries
                    if action == "charge":
                        okp, triesp = self.write_holding(42010, power_w)
                        all_tries += [{"addr": 42010, **t} for t in triesp]
                        okc, triesc = self.write_holding(42020, 1)
                        all_tries += [{"addr": 42020, **t} for t in triesc]
                        return (okp and okc), all_tries
                    if action == "discharge":
                        okp, triesp = self.write_holding(42011, power_w)
                        all_tries += [{"addr": 42011, **t} for t in triesp]
                        okd, triesd = self.write_holding(42020, 2)
                        all_tries += [{"addr": 42020, **t} for t in triesd]
                        return (okp and okd), all_tries
                return False, all_tries

            # 1) Try direct command first (sommige firmwares laten dit toe)
            ok_cmd, tries_cmd = do_command(primary=True)
            result["attempts"] += tries_cmd
            if ok_cmd:
                result.update({"ok": True, "action": action, "power_w": power_w})
                return result

            # 2) Try enabling RS485 control and manual mode, then retry command
            ok_enable, tries_en = self.write_holding(42000, 1)
            result["attempts"] += [{"addr": 42000, **t} for t in tries_en]
            # optional manual mode
            ok_manual, tries_manual = self.write_holding(42001, 1)
            result["attempts"] += [{"addr": 42001, **t} for t in tries_manual]

            ok_cmd2, tries_cmd2 = do_command(primary=True)
            result["attempts"] += tries_cmd2
            if ok_cmd2:
                result.update({"ok": True, "action": action, "power_w": power_w})
                return result

            # 3) Try alternate mapping without and with enable/manual
            ok_alt, tries_alt = do_command(primary=False)
            result["attempts"] += tries_alt
            if ok_alt:
                result.update({"ok": True, "action": action, "power_w": power_w, "map": "alt"})
                return result

            ok_en_alt, tries_en_alt = self.write_holding(42000, 1)
            result["attempts"] += [{"addr": 42000, **t} for t in tries_en_alt]
            ok_man_alt, tries_man_alt = self.write_holding(42001, 1)
            result["attempts"] += [{"addr": 42001, **t} for t in tries_man_alt]

            ok_alt2, tries_alt2 = do_command(primary=False)
            result["attempts"] += tries_alt2
            if ok_alt2:
                result.update({"ok": True, "action": action, "power_w": power_w, "map": "alt"})
                return result

            result["error"] = "command failed (primary+alt)"
            return result

            result["error"] = f"unknown action: {action}"
            return result
        finally:
            # Keep connection policy consistent with reads: short session
            try:
                self.disconnect()
            except Exception:
                pass

# Global Modbus client
venus_modbus = VenusEModbusClient()
# Ensure only one Modbus read at a time
modbus_lock = asyncio.Lock()

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

    # -------------------------
    # UDP JSON-RPC (per Open API)
    # -------------------------
    async def _udp_call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 1.0) -> Dict[str, Any]:
        """Send a JSON-RPC message over UDP to the device. Host derived from base_url, port default 30000.
        Returns result dict or raises RuntimeError.
        """
        import socket, json as _json
        # Derive host from base_url
        try:
            host = self.base_url.split("//", 1)[-1].split(":", 1)[0]
        except Exception:
            host = self.base_url
        port = int(os.getenv("MARSTEK_UDP_PORT", "30000"))

        req = {"id": 1, "method": method, "params": {"id": 0} | (params or {})}
        data = _json.dumps(req).encode("utf-8")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(data, (host, port))
            try:
                buf, _ = sock.recvfrom(4096)
            except socket.timeout:
                raise RuntimeError(f"UDP timeout calling {method}")
        try:
            resp = _json.loads(buf.decode("utf-8", errors="ignore"))
        except Exception as e:
            raise RuntimeError(f"UDP parse error: {e}")
        if "result" in resp:
            return resp["result"]
        raise RuntimeError(resp.get("error", {"message": "Unknown UDP error"}))

    async def es_get_status(self) -> Optional[Dict[str, Any]]:
        """Call ES.GetStatus to retrieve overall power and battery info."""
        try:
            return await self._udp_call("ES.GetStatus")
        except Exception:
            return None

    async def bat_get_status(self) -> Optional[Dict[str, Any]]:
        """Call Bat.GetStatus to retrieve detailed battery info (soc, capacities in Wh)."""
        try:
            return await self._udp_call("Bat.GetStatus")
        except Exception:
            return None

    async def es_get_mode(self) -> Optional[Dict[str, Any]]:
        """Call ES.GetMode to retrieve operating mode and optional powers."""
        try:
            return await self._udp_call("ES.GetMode")
        except Exception:
            return None

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

def extract_house_consumption_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """Huis verbruik (W) - berekend uit CT clamps en devices."""
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        if isinstance(raw, list):
            # Prefer CT consumption from Harvi if available
            ct_consumption = 0
            pv_generation = 0
            
            for section in raw:
                if isinstance(section, dict) and "harvi" in section:
                    arr = section.get("harvi") or []
                    if arr and isinstance(arr[0], dict):
                        harvi = arr[0]
                        
                        # CT clamps power (ectp1, ectp2, ectp3)
                        for i in range(1, 4):
                            ct_power_key = f"ectp{i}"
                            ct_type_key = f"ectt{i}"
                            
                            if ct_power_key in harvi and ct_type_key in harvi:
                                try:
                                    power = int(harvi[ct_power_key])
                                except Exception:
                                    continue
                                ct_type = str(harvi[ct_type_key] or "").lower()
                                
                                if ct_type == "generation":
                                    pv_generation += power
                                else:
                                    # Treat non-generation clamps as house load; abs guards against sign config
                                    ct_consumption += abs(power)
            
            # If we have CT-based house load, use it directly
            if ct_consumption > 0:
                return ct_consumption
            
            # Fallback: derive from grid and device loads
            eddi_w = extract_eddi_power_w(myenergi_status) or 0
            zappi_w = extract_zappi_power_w(myenergi_status) or 0
            grid_w = extract_grid_export_w(myenergi_status) or 0
            if grid_w < 0:  # Importing
                # Approximate total house load
                return max(0, abs(grid_w) + max(0, eddi_w) + max(0, zappi_w))
            else:
                # Exporting: estimate from PV minus export and device loads
                return max(0, (pv_generation or 0) - grid_w - max(0, eddi_w) - max(0, zappi_w))
                
    except Exception:
        pass
    return None

def extract_pv_generation_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """PV generatie (W) - uit Harvi CT clamps."""
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        if isinstance(raw, list):
            total_generation = 0
            
            for section in raw:
                if isinstance(section, dict) and "harvi" in section:
                    arr = section.get("harvi") or []
                    if arr and isinstance(arr[0], dict):
                        harvi = arr[0]
                        
                        # Look for Generation CT clamps
                        for i in range(1, 4):
                            ct_power_key = f"ectp{i}"
                            ct_type_key = f"ectt{i}"
                            
                            if ct_power_key in harvi and ct_type_key in harvi:
                                if harvi[ct_type_key] == "Generation":
                                    total_generation += int(harvi[ct_power_key])
            
            return total_generation if total_generation > 0 else None
                
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
                        # Tank temperaturen: tp1, tp2 (al in hele graden)
                        if "tp1" in eddi and eddi["tp1"] != -1:
                            temps["tank1"] = int(eddi["tp1"])
                        if "tp2" in eddi and eddi["tp2"] != -1:
                            temps["tank2"] = int(eddi["tp2"])
        else:
            # Lokale response
            items = raw if isinstance(raw, dict) else {}
            if "tp1" in items and items["tp1"] != -1:
                temps["tank1"] = int(items["tp1"])
            if "tp2" in items and items["tp2"] != -1:
                temps["tank2"] = int(items["tp2"])
                
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
        # myenergi data (always try this first)
        m = await myenergi.status_all()
        export_w = extract_grid_export_w(m)
        eddi_w = extract_eddi_power_w(m)
        zappi_w = extract_zappi_power_w(m)
        house_w = extract_house_consumption_w(m)
        pv_w = extract_pv_generation_w(m)
        eddi_temps = extract_eddi_temperatures(m)
        should_block, block_reason = should_block_battery_for_priority(m, state.battery_blocked)
        
        # Marstek data (with timeout protection)
        soc = None
        power = None
        marstek_error = None
        
        try:
            # Try to get battery data with short timeout
            import asyncio
            soc = await asyncio.wait_for(marstek.get_soc(), timeout=2.0)
            power = await asyncio.wait_for(marstek.get_power(), timeout=2.0)
        except asyncio.TimeoutError:
            marstek_error = "Battery connection timeout"
        except Exception as e:
            marstek_error = f"Battery error: {str(e)[:50]}"
        
        return {
            "timestamp": time.time(),
            "myenergi_raw": m,
            "grid_export_w": export_w,
            "eddi_power_w": eddi_w,
            "zappi_power_w": zappi_w,
            "house_consumption_w": house_w,
            "pv_generation_w": pv_w,
            "eddi_temperatures": eddi_temps,
            "should_block": should_block,
            "block_reason": block_reason,
            "marstek_soc": soc,
            "marstek_power_w": power,
            "marstek_error": marstek_error,
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
        # no-store headers to prevent caching in browsers/proxies
        cache_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        return JSONResponse(content=payload, headers=cache_headers)
    except Exception as e:
        cache_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        return JSONResponse(content={"error": str(e), "timestamp": time.time()}, headers=cache_headers)

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
            export_w = extract_grid_export_w(m)  # >0 = export
            now = time.time()
            
            # Try to get battery SoC with timeout
            soc = None
            try:
                soc = await asyncio.wait_for(marstek.get_soc(), timeout=1.0)
            except:
                pass  # Continue without battery data

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

# =========================
# Settings Endpoints
# =========================
@app.get("/api/settings")
async def get_settings():
    return {
        "success": True,
        "min_soc_reserve": MIN_SOC_RESERVE,
        "battery_full_kwh": BATTERY_FULL_KWH,
    }

@app.post("/api/settings/reserve")
async def set_min_soc_reserve(payload: Dict[str, Any] = Body(...)):
    try:
        val = int(payload.get("min_soc_reserve"))
        val = max(0, min(val, 100))
        global MIN_SOC_RESERVE
        MIN_SOC_RESERVE = val
        return {"success": True, "min_soc_reserve": MIN_SOC_RESERVE}
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
    
    try:
        # Disconnect Modbus client
        if venus_modbus and venus_modbus.connected:
            venus_modbus.disconnect()
            print("📡 Modbus client disconnected")
    except Exception as e:
        print(f"⚠️  Modbus cleanup warning: {e}")
    
    try:
        # BLE cleanup if available
        if BLE_AVAILABLE:
            await cleanup_ble_client()
            print("🔵 BLE client cleaned up")
    except Exception as e:
        print(f"⚠️  BLE cleanup warning: {e}")
    
    print("✅ Shutdown complete")

# =========================
# Battery Modbus Endpoints
# =========================
@app.get("/api/battery/status")
async def get_battery_status():
    """Get real-time battery status via Modbus"""
    try:
        # Serialize access to the Modbus client to avoid broken pipes
        async with modbus_lock:
            battery_data = venus_modbus.read_battery_data()
            # Use short session: disconnect after a full read to prevent stale sockets
            try:
                venus_modbus.disconnect()
            except Exception:
                pass
        
        if battery_data:
            # Derived energy metrics
            soc = None
            try:
                soc = float(battery_data.get("soc_percent", {}).get("value"))
            except Exception:
                soc = None
            # Compute power from Modbus values
            try:
                v = float(battery_data.get("battery_voltage", {}).get("value", 0.0))
            except Exception:
                v = 0.0
            try:
                i = float(battery_data.get("battery_current", {}).get("value", 0.0))
            except Exception:
                i = 0.0
            calc_power_w = v * i
            # Prefer device-reported battery power if present
            raw_bp = battery_data.get("battery_power", {})
            power_w = raw_bp.get("value") if isinstance(raw_bp, dict) else None
            if not isinstance(power_w, (int, float)):
                power_w = calc_power_w
            # Mode: prefer work_mode register, else derive from calculated power (more reliable sign)
            work_mode_raw = battery_data.get("work_mode", {}).get("raw")
            mode_map = {0: "Standby", 1: "Charging", 2: "Discharging", 3: "Backup", 4: "Fault", 5: "Idle", 6: "Self-Regulating"}
            mode = mode_map.get(work_mode_raw)
            if not mode:
                mode = "Idle" if abs(calc_power_w) < 20 else ("Charging" if calc_power_w > 0 else "Discharging")
            remaining_kwh = (BATTERY_FULL_KWH * (soc/100.0)) if (soc is not None) else None

            return {
                "success": True,
                "data": battery_data,
                "derived": {
                    "full_kwh": BATTERY_FULL_KWH,
                    "remaining_kwh": remaining_kwh,
                    "soc_percent": soc,
                    "power_w": power_w,
                    "calc_power_w": calc_power_w,
                    "mode": mode,
                    "min_soc_reserve": MIN_SOC_RESERVE,
                },
                "source": "modbus",
                "host": venus_modbus.host,
                "port": venus_modbus.port,
                "timestamp": datetime.now().isoformat()
            }
        else:
            return {
                "success": False,
                "error": "No battery data available",
                "source": "modbus",
                "host": venus_modbus.host
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "source": "modbus"
        }

@app.post("/api/battery/control")
async def battery_control(payload: Dict[str, Any] = Body(...)):
    """Force battery actions via Modbus controls.
    Payload: { action: 'charge'|'discharge'|'stop', power_w?: int }
    """
    try:
        action = str(payload.get("action") or "").strip().lower()
        power_w = payload.get("power_w")
        if action not in {"charge", "discharge", "stop"}:
            return {"success": False, "error": "invalid action"}
        # Serialize reads/writes too
        async with modbus_lock:
            # Enforce SoC reserve for discharge
            try:
                bd = venus_modbus.read_battery_data()
                try:
                    venus_modbus.disconnect()
                except Exception:
                    pass
            except Exception:
                bd = None
            current_soc = None
            try:
                if bd:
                    current_soc = float(bd.get("soc_percent", {}).get("value"))
            except Exception:
                current_soc = None

            if action == "discharge" and current_soc is not None and current_soc <= MIN_SOC_RESERVE:
                return {"success": False, "error": f"blocked by reserve: SoC {current_soc:.1f}% <= {MIN_SOC_RESERVE}%"}

            result = venus_modbus.set_control(action, power_w)
        return {"success": bool(result.get("ok")), **result}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery/raw")
async def get_battery_raw():
    """Return raw Modbus battery data for debugging mapping/scaling."""
    try:
        async with modbus_lock:
            data = venus_modbus.read_battery_data()
            try:
                venus_modbus.disconnect()
            except Exception:
                pass
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery/ping")
async def battery_ping():
    """Quick connectivity probe: try to open Modbus TCP and read a trivial register.
    Returns host/port and simple success flag.
    """
    try:
        async with modbus_lock:
            # Open connection
            if not venus_modbus.connected:
                venus_modbus.connect()
            ok = venus_modbus.connected
            # Try a lightweight read using both keyword styles
            addr = 30000
            val = None
            try:
                rr = venus_modbus.client.read_input_registers(address=addr, count=1, unit=1)
                if hasattr(rr, 'registers') and not rr.isError():
                    val = rr.registers[0]
            except Exception:
                pass
            if val is None:
                try:
                    rr2 = venus_modbus.client.read_input_registers(address=addr, count=1, slave=1)
                    if hasattr(rr2, 'registers') and not rr2.isError():
                        val = rr2.registers[0]
                except Exception:
                    pass
            try:
                venus_modbus.disconnect()
            except Exception:
                pass
        return {"success": ok, "host": venus_modbus.host, "port": venus_modbus.port, "sample": {"address": addr, "value": val}}
    except Exception as e:
        return {"success": False, "error": str(e), "host": venus_modbus.host, "port": venus_modbus.port}

@app.get("/api/battery/read_many")
async def modbus_read_many(addrs: str, fn: str = Query("holding"), unit: int = Query(1), delay_ms: int = Query(0)):
    """Read many Modbus registers for diagnostics.
    Query:
      - addrs: comma-separated addresses, e.g. 42000,42001
      - fn: only 'holding' supported
      - unit: preferred unit/slave id
      - delay_ms: optional delay between reads
    """
    try:
        addresses = [int(x.strip()) for x in addrs.split(',') if x.strip()]
        results = []
        async with modbus_lock:
            if not venus_modbus.connected:
                venus_modbus.connect()
            for a in addresses:
                val = None
                attempts = []
                # Try 'unit' style
                try:
                    rr = venus_modbus.client.read_holding_registers(address=a, count=1, unit=unit)
                    ok = (not getattr(rr, 'isError', lambda: False)()) and hasattr(rr, 'registers')
                    attempts.append({"style": "unit", "ok": ok})
                    if ok:
                        val = rr.registers[0]
                except Exception as ex:
                    attempts.append({"style": "unit_exception", "ok": False, "error": str(ex)})
                # If still no val, try 'slave' style
                if val is None:
                    try:
                        rr2 = venus_modbus.client.read_holding_registers(address=a, count=1, slave=unit)
                        ok2 = (not getattr(rr2, 'isError', lambda: False)()) and hasattr(rr2, 'registers')
                        attempts.append({"style": "slave", "ok": ok2})
                        if ok2:
                            val = rr2.registers[0]
                    except Exception as ex2:
                        attempts.append({"style": "slave_exception", "ok": False, "error": str(ex2)})
                results.append({"address": a, "value": val, "attempts": attempts})
                if delay_ms:
                    await asyncio.sleep(delay_ms/1000.0)
            try:
                venus_modbus.disconnect()
            except Exception:
                pass
        return {"success": True, "values": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery/scan")
async def scan_battery_registers(start: int = 30000, count: int = 80, kind: str = "input"):
    """Scan a window of Modbus registers (input or holding) and return raw values.
    Reuses the same Modbus client/config as read_battery_data for maximum compatibility.
    Params:
      - start: first register address
      - count: number of registers to read (capped to 120)
      - kind: 'input' (function 4) or 'holding' (function 3)
    """
    count = max(1, min(int(count), 120))
    start = int(start)
    kind = (kind or "input").lower().strip()

    result = {"success": False, "host": venus_modbus.host, "port": venus_modbus.port, "start": start, "count": count, "kind": kind, "values": {}}
    try:
        async with modbus_lock:
            if not venus_modbus.connect():
                result["error"] = "connect failed"
                return result
            try:
                client = venus_modbus.client
                for addr in range(start, start + count):
                    try:
                        if kind == "holding":
                            rr = client.read_holding_registers(addr, 1, unit=1)
                        else:
                            rr = client.read_input_registers(addr, 1, unit=1)
                        if rr and not rr.isError():
                            result["values"][addr] = rr.registers[0]
                    except Exception:
                        continue
            finally:
                try:
                    venus_modbus.disconnect()
                except Exception:
                    pass
        result["success"] = True
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

@app.get("/api/battery/read_many")
async def read_many(addrs: str, fn: str = "input", unit: int = 1, delay_ms: int = 0):
    """Read a comma-separated list of Modbus register addresses one-by-one using the same
    client configuration as normal reads. Returns both raw and formatted values.
    Params:
      - addrs: comma-separated addresses (e.g. 29990,29991,...)
      - fn: 'input' (function 4) or 'holding' (function 3)
      - unit: Modbus unit id (commonly 1, some devices use 0)
      - delay_ms: optional delay between reads
    """
    try:
        # Parse addresses
        addresses = []
        for part in (addrs or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                addresses.append(int(part))
            except ValueError:
                pass
        if not addresses:
            return {"success": False, "error": "No addresses provided"}

        out = {}
        fn = (fn or "input").lower().strip()
        unit_id = int(unit)
        wait = max(0, int(delay_ms)) / 1000.0
        async with modbus_lock:
            if not venus_modbus.connect():
                return {"success": False, "error": "connect failed"}
            try:
                client = venus_modbus.client
                for addr in addresses:
                    raw = None
                    try:
                        if fn == "holding":
                            rr = client.read_holding_registers(addr, 1, unit=unit_id)
                        else:
                            rr = client.read_input_registers(addr, 1, unit=unit_id)
                        if rr and not rr.isError():
                            raw = rr.registers[0]
                    except Exception:
                        raw = None

                    if raw is None:
                        out[addr] = {"ok": False}
                    else:
                        try:
                            fmt = format_value(addr, raw)
                        except Exception:
                            fmt = {"value": raw, "formatted": str(raw)}
                        out[addr] = {"ok": True, "raw": raw, "formatted": fmt}
                    if wait:
                        import time as _t
                        _t.sleep(wait)
            finally:
                try:
                    venus_modbus.disconnect()
                except Exception:
                    pass
        return {"success": True, "values": out}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery/test")
async def test_battery_connection():
    """Test Modbus connection to battery"""
    try:
        connected = venus_modbus.connect()
        
        if connected:
            # Quick test read
            test_data = venus_modbus.read_battery_data()
            venus_modbus.disconnect()
            
            return {
                "success": True,
                "connected": True,
                "host": venus_modbus.host,
                "port": venus_modbus.port,
                "data_available": test_data is not None,
                "register_count": len(test_data) if test_data else 0
            }
        else:
            return {
                "success": False,
                "connected": False,
                "host": venus_modbus.host,
                "port": venus_modbus.port,
                "error": "Connection failed"
            }
            
    except Exception as e:
        return {
            "success": False,
            "connected": False,
            "error": str(e)
        }

# =========================
# System Control Endpoints
# =========================
@app.post("/api/system/restart")
async def restart_application():
    """Restart the application"""
    try:
        import os
        import signal
        import asyncio
        
        # Clean shutdown first
        print("🔄 Restart requested via API")
        
        # Schedule restart after response is sent
        async def delayed_restart():
            await asyncio.sleep(2)  # Give time for response to be sent
            print("🔄 Initiating restart...")
            
            # Clean disconnect
            try:
                if venus_modbus and venus_modbus.connected:
                    venus_modbus.disconnect()
            except:
                pass
            
            # Send SIGTERM for clean shutdown
            os.kill(os.getpid(), signal.SIGTERM)
        
        # Start the delayed restart task
        asyncio.create_task(delayed_restart())
        
        return {
            "success": True,
            "message": "Application restart initiated",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

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

# =========================
# MQTT Integration
# =========================
@app.post("/api/mqtt/publish")
async def mqtt_publish(payload: Dict[str, str] = Body(...)):
    """Publish MQTT message via external mosquitto_pub"""
    try:
        topic = payload.get("topic")
        message = payload.get("message")
        
        if not topic or not message:
            return {"success": False, "error": "Missing topic or message"}
        
        # Use mosquitto_pub command to publish
        import subprocess
        result = subprocess.run([
            "mosquitto_pub", 
            "-h", "localhost", 
            "-t", topic, 
            "-m", message
        ], capture_output=True, text=True, timeout=5)
        
        if result.returncode == 0:
            print(f"📡 MQTT Published: {topic} = {message}")
            return {"success": True, "topic": topic, "message": message}
        else:
            print(f"❌ MQTT Publish failed: {result.stderr}")
            return {"success": False, "error": result.stderr}
            
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "MQTT publish timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# =========================
# Multi-Battery Discovery
# =========================
@app.get("/api/batteries/discover")
async def discover_batteries():
    """Discover all available batteries"""
    print("🔍 API: Starting battery discovery...")
    try:
        # Import battery discovery
        import sys
        sys.path.insert(0, '.')
        from battery_discovery import BatteryDiscovery
        
        discovery = BatteryDiscovery()
        batteries = await discovery.discover_all()
        
        print(f"✅ API: Discovery complete - {batteries.get('total', 0)} batteries found")
        print(f"📊 API: BLE: {len(batteries.get('ble', []))}, Network: {len(batteries.get('network', []))}")
        
        return batteries
    except Exception as e:
        print(f"❌ API: Discovery failed - {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "ble": [], "network": [], "total": 0}

@app.post("/api/batteries/connect")
async def connect_to_battery(payload: Dict[str, str] = Body(...)):
    """Connect to specific battery"""
    try:
        battery_type = payload.get("type")
        address = payload.get("address")
        name = payload.get("name", "Unknown")
        
        if battery_type == "ble":
            # Connect to BLE battery
            if BLE_AVAILABLE:
                ble_client = get_ble_client()
                # Update client to use specific address
                ble_client.device_address = address
                ble_client.device_name = name
                success = await ble_client.connect()
                return {"success": success, "type": "ble", "name": name}
            else:
                return {"success": False, "error": "BLE not available"}
        
        elif battery_type == "network":
            # Connect to network battery
            ip_port = address.split(":")
            if len(ip_port) == 2:
                ip, port = ip_port
                # Update marstek client to use this IP
                global marstek
                marstek = MarstekClient(f"http://{ip}:{port}", "")
                return {"success": True, "type": "network", "name": f"{ip}:{port}"}
            else:
                return {"success": False, "error": "Invalid address format"}
        
        else:
            return {"success": False, "error": "Unknown battery type"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}
