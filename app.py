import os
import logging
from logging.handlers import RotatingFileHandler
import json

# Logging configuration (must run after importing os/logging)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "logs/app.log")

if not logging.getLogger().handlers:
    handlers = []
    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname)s %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    # Ensure directory
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    except Exception:
        pass
    try:
        rot = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
        rot.setFormatter(formatter)
        handlers.append(rot)
    except Exception:
        pass
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
from enum import Enum
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, BackgroundTasks, Request, Query, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymodbus.client import ModbusTcpClient
from venus_e_register_map import format_value, get_all_sensors
from battery_manager import BatteryManager
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
            32102: "battery_power",    # W (signed) - holding register, int32
            35100: "work_mode",        # enum
            # Control/Setpoint registers (holding)
            42000: "rs485_control_enable",     # 0/1 or magic token
            42010: "control_mode_command",     # 0=Stop,1=Charge,2=Discharge
            42020: "charge_setpoint_power",    # W
            42021: "discharge_setpoint_power", # W
            43000: "user_work_mode",           # 0=Manual, 1=Anti-Feed, 2=Trade Mode
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
        
        # Calculate actual power from voltage × current if we have both
        if "battery_voltage" in battery_data and "battery_current" in battery_data:
            voltage = battery_data["battery_voltage"]["value"]
            current = battery_data["battery_current"]["value"] 
            calculated_power = voltage * current
            
            # Apply scaling to match Marstek app (divide by ~10)
            scaled_power = calculated_power * 0.1
            
            # Override battery_power with scaled calculated value
            battery_data["battery_power"] = {
                "value": scaled_power,
                "formatted": f"{scaled_power:.0f} W",
                "unit": "W", 
                "description": "Battery Power (calculated)",
                "register": "calc",
                "timestamp": datetime.now().isoformat()
            }
            logging.info(f"Calculated power: {voltage}V × {current}A = {calculated_power}W, scaled = {scaled_power}W")
        
        return battery_data

    # -------------------------
    # Control helpers (holding registers)
    # -------------------------
    def write_holding(self, address: int, value: int) -> tuple[bool, list[dict]]:
        attempts: list[dict] = []
        try:
            if not self.connected and not self.connect():
                return False, attempts
            # Try a range of common unit IDs and both keyword styles (unit/slave)
            units_to_try = list(range(1, 11)) + [0, 247]
            for unit in units_to_try:
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

    def set_work_mode(self, mode: int) -> dict:
        """Sets the main work mode of the battery.
        - 42001: User Work Mode (0=Auto, 1=Manual, 2=Trade, 3=Backup)
        """
        REG_USER_WORK_MODE = 43000  # Correct register for user work mode
        REG_CONTROL_MODE = 42000    # RS485 control enable/disable
        CONTROL_ENABLE = 21930      # 0x55AA
        CONTROL_DISABLE = 21947     # 0x55BB
        
        result = {"ok": False, "attempts": []}
        if mode not in {0, 1, 2, 3}:
            result["error"] = "Invalid mode. Must be 0, 1, 2, or 3."
            return result

        try:
            if not self.connected and not self.connect():
                result["error"] = "connect failed"
                return result

            # Step 1: Enable RS485 control
            ok_enable, tries_enable = self.write_holding(REG_CONTROL_MODE, CONTROL_ENABLE)
            result["attempts"] += [{"addr": REG_CONTROL_MODE, "val": CONTROL_ENABLE, **t} for t in tries_enable]
            
            if not ok_enable:
                result["error"] = "Failed to enable RS485 control"
                return result

            # Small delay after enabling control
            try:
                import time as _t
                _t.sleep(0.1)
            except Exception:
                pass

            # Step 2: Set user work mode to register 43000
            ok_mode, tries_mode = self.write_holding(REG_USER_WORK_MODE, mode)
            result["attempts"] += [{"addr": REG_USER_WORK_MODE, "val": mode, **t} for t in tries_mode]
            
            if not ok_mode:
                result["error"] = "Failed to set work mode"
                return result

            # Small delay after setting mode
            try:
                import time as _t
                _t.sleep(0.1)
            except Exception:
                pass

            # Step 3: Disable RS485 control (let app manage battery again)
            ok_disable, tries_disable = self.write_holding(REG_CONTROL_MODE, CONTROL_DISABLE)
            result["attempts"] += [{"addr": REG_CONTROL_MODE, "val": CONTROL_DISABLE, **t} for t in tries_disable]
            
            # Note: We don't fail if disable fails, as the mode was already set

            # Readback attempt from register 43000
            try:
                rr = self.client.read_holding_registers(address=REG_USER_WORK_MODE, count=1, slave=1)
                if hasattr(rr, 'registers') and not rr.isError():
                    result["readback"] = rr.registers[0]
            except Exception:
                pass

            mode_names = {0: "Manual", 1: "Anti-Feed", 2: "Trade Mode"}
            result.update({
                "ok": True, 
                "action": "set_work_mode", 
                "mode": mode,
                "mode_name": mode_names.get(mode, f"Mode {mode}")
            })
            return result
        finally:
            try:
                self.disconnect()
            except Exception:
                pass

    def check_minimum_soc(self, min_soc_percent: float = 20.0, hysteresis: float = 2.0) -> dict:
        """Check if current SoC is above minimum and take action if needed
        Uses hysteresis to prevent toggling around the threshold
        """
        try:
            # Get current battery data
            battery_data = self.read_battery_data()
            if not battery_data or "soc_percent" not in battery_data:
                return {"ok": False, "error": "Could not read SoC data"}
            
            current_soc = battery_data["soc_percent"]["value"]
            stop_threshold = min_soc_percent + hysteresis  # e.g. 20% + 2% = 22%
            
            result = {
                "ok": True,
                "current_soc": current_soc,
                "min_soc_limit": min_soc_percent,
                "stop_threshold": stop_threshold,
                "action_taken": None
            }
            
            if current_soc <= min_soc_percent:
                # SoC too low - activate emergency charge
                emergency_power = 500  # Conservative charging power
                charge_result = self.set_control("charge", emergency_power)
                
                result.update({
                    "action_taken": "emergency_charge",
                    "emergency_power": emergency_power,
                    "charge_result": charge_result,
                    "warning": f"SoC {current_soc}% ≤ {min_soc_percent}% - Emergency charging activated"
                })
            elif current_soc >= stop_threshold:
                # SoC is safe with hysteresis - stop emergency charge and return to previous mode
                stop_result = self.set_control("stop")
                
                result.update({
                    "action_taken": "stop_emergency_charge",
                    "stop_result": stop_result,
                    "status": f"SoC {current_soc}% ≥ {stop_threshold}% - Emergency charge stopped, returning to previous mode"
                })
            else:
                # In hysteresis zone - no action to prevent toggling
                result.update({
                    "action_taken": "hysteresis_zone",
                    "status": f"SoC {current_soc}% in hysteresis zone ({min_soc_percent}% - {stop_threshold}%) - No action"
                })
            
            return result
            
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_control(self, action: str, power_w: Optional[int] = None) -> dict:
        """High-level control for charge/discharge/stop using community-provided registers.
        - 42000: Control mode (0x55AA to enable, 0x55BB to disable)
        - 42010: Mode (1=Charge, 2=Discharge, 0=Stop)
        - 42020: Charge power (W)
        - 42021: Discharge power (W)
        """
        CONTROL_ENABLE = 21930  # 0x55AA
        CONTROL_DISABLE = 21947 # 0x55BB
        REG_CONTROL_MODE = 42000
        REG_SET_MODE = 42010
        REG_CHARGE_POWER = 42020
        REG_DISCHARGE_POWER = 42021

        result = {"ok": False, "attempts": []}
        power_w = max(0, int(power_w or 0))

        if not self.connected and not self.connect():
            result["error"] = "connect failed"
            return result

        # Step 1: Enable control mode (unless we are stopping)
        if action != "stop":
            ok_en, tries_en = self.write_holding(REG_CONTROL_MODE, CONTROL_ENABLE)
            result["attempts"] += [{"addr": REG_CONTROL_MODE, "val": CONTROL_ENABLE, **t} for t in tries_en]
            if not ok_en:
                result["error"] = "Failed to enable control mode"
                return result
            time.sleep(0.1) # Wait a moment after enabling control

        # Step 2: Set power and mode
        ok_cmd = False
        if action == "charge":
            ok_p, tries_p = self.write_holding(REG_CHARGE_POWER, power_w)
            result["attempts"] += [{"addr": REG_CHARGE_POWER, "val": power_w, **t} for t in tries_p]
            ok_m, tries_m = self.write_holding(REG_SET_MODE, 1)
            result["attempts"] += [{"addr": REG_SET_MODE, "val": 1, **t} for t in tries_m]
            ok_cmd = ok_p and ok_m

        elif action == "discharge":
            ok_p, tries_p = self.write_holding(REG_DISCHARGE_POWER, power_w)
            result["attempts"] += [{"addr": REG_DISCHARGE_POWER, "val": power_w, **t} for t in tries_p]
            ok_m, tries_m = self.write_holding(REG_SET_MODE, 2)
            result["attempts"] += [{"addr": REG_SET_MODE, "val": 2, **t} for t in tries_m]
            ok_cmd = ok_p and ok_m

        elif action == "stop":
            # Explicitly set powers to 0 first for a clean stop
            ok_pc, tries_pc = self.write_holding(REG_CHARGE_POWER, 0)
            result["attempts"] += [{"addr": REG_CHARGE_POWER, "val": 0, **t} for t in tries_pc]
            ok_pd, tries_pd = self.write_holding(REG_DISCHARGE_POWER, 0)
            result["attempts"] += [{"addr": REG_DISCHARGE_POWER, "val": 0, **t} for t in tries_pd]
            
            # Then, set mode to stop
            ok_m, tries_m = self.write_holding(REG_SET_MODE, 0)
            result["attempts"] += [{"addr": REG_SET_MODE, "val": 0, **t} for t in tries_m]
            time.sleep(0.1)
            
            # Finally, disable remote control to return to normal operation
            ok_dis, tries_dis = self.write_holding(REG_CONTROL_MODE, CONTROL_DISABLE)
            result["attempts"] += [{"addr": REG_CONTROL_MODE, "val": CONTROL_DISABLE, **t} for t in tries_dis]
            ok_cmd = ok_pc and ok_pd and ok_m and ok_dis
        else:
            result["error"] = f"unknown action: {action}"
            return result

        # Final result
        if ok_cmd:
            result.update({"ok": True, "action": action, "power_w": power_w})
        else:
            result["error"] = f"Command '{action}' failed."
        
        return result

# Global Modbus clients
venus_modbus = VenusEModbusClient()  # Battery 1 (default host 192.168.68.92)
# Battery 2 (WiFi converter), configurable via env VENUS_MODBUS_HOST2
venus_modbus2 = VenusEModbusClient(host=os.getenv('VENUS_MODBUS_HOST2', '192.168.68.74'))
# Ensure only one Modbus read at a time (per device)
modbus_lock = asyncio.Lock()
modbus_lock2 = asyncio.Lock()

# Multi-battery manager (ids aligned to user naming)
#  - venus_ev2_92 → 192.168.68.92 (Battery 1)
#  - venus_ev2_74 → 192.168.68.74 (Battery 2)
manager = BatteryManager({
    'venus_ev2_92': {'client': venus_modbus,  'lock': modbus_lock},
    'venus_ev2_74': {'client': venus_modbus2, 'lock': modbus_lock2},
})

def _get_entry_for(bid: str):
    entry = None
    try:
        entry = {'client': manager.registry[bid]['client'], 'lock': manager.registry[bid]['lock']}
    except Exception:
        entry = None
    return entry

# Battery configuration management
BATTERY_CONFIG_FILE = "battery_config.json"

def load_battery_config() -> dict:
    """Load battery configuration from file"""
    try:
        if os.path.exists(BATTERY_CONFIG_FILE):
            with open(BATTERY_CONFIG_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"Could not load battery config: {e}")
    
    # Default config
    return {
        "venus_e_78": {
            "minimum_soc_percent": 20.0,
            "auto_charge_enabled": True,
            "original_work_mode": None,
            "emergency_charge_active": False,
            "last_updated": datetime.now().isoformat()
        }
    }

def save_battery_config(config: dict) -> bool:
    """Save battery configuration to file"""
    try:
        config["venus_e_78"]["last_updated"] = datetime.now().isoformat()
        with open(BATTERY_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"Could not save battery config: {e}")
        return False

# Lock to prevent concurrent requests to the MyEnergi API, which can cause auth issues
myenergi_lock = asyncio.Lock()

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

def extract_house_consumption_w(myenergi_status: Dict[str, Any], battery_power_w: int = 0) -> Optional[int]:
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
                logger.info(f"House consumption from CT clamps: {ct_consumption}W")
                return ct_consumption
            
            # Fallback: derive from grid and device loads
            eddi_w = extract_eddi_power_w(myenergi_status) or 0
            zappi_w = extract_zappi_power_w(myenergi_status) or 0
            grid_w = extract_grid_export_w(myenergi_status) or 0
            pv_gen = extract_pv_generation_w(myenergi_status) or 0

            # Huisverbruik = PV Generatie + Grid Import - Eddi Verbruik - Zappi Verbruik - Batterij Laden
            # Let op: grid_w is positief bij import (vanuit huis perspectief), negatief bij export.
            # Batterij laden is positief (verbruikt energie), ontladen is negatief (levert energie)
            # De formule `pv_gen + grid_w` dekt dus zowel import als export correct.
            # Voorbeeld Import: 0 (pv) + 2000 (grid import) - 0 - 0 - 500 (batterij laden) = 1500 (huis verbruik)
            # Voorbeeld Export: 5000 (pv) + (-1000) (grid export) - 0 - 0 - 0 = 4000 (huis verbruik)
            house_consumption = pv_gen + grid_w - eddi_w - zappi_w - battery_power_w
            logger.info(f"House consumption fallback: pv={pv_gen}, grid={grid_w}, eddi={eddi_w}, zappi={zappi_w}, battery={battery_power_w} -> house={house_consumption}")
            return max(0, int(house_consumption))
                
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

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/api/status")
async def get_status():
    """Samengevoegde status van myenergi + marstek."""
    try:
        # myenergi data (always try this first)
        async with myenergi_lock:
            m = await myenergi.status_all()
        export_w = extract_grid_export_w(m)
        eddi_w = extract_eddi_power_w(m)
        zappi_w = extract_zappi_power_w(m)
        pv_w = extract_pv_generation_w(m)
        eddi_temps = extract_eddi_temperatures(m)
        should_block, block_reason = should_block_battery_for_priority(m, state.battery_blocked)
        
        # Marstek data (with timeout protection)
        soc = None
        power = None
        marstek_error = None
        battery_power_w = 0
        
        try:
            # Try to get battery data with short timeout
            import asyncio
            soc = await asyncio.wait_for(marstek.get_soc(), timeout=2.0)
            power = await asyncio.wait_for(marstek.get_power(), timeout=2.0)
            
            # Extract battery power for house consumption calculation
            if power and hasattr(power, 'value'):
                battery_power_w = int(power.value)  # Positive = charging (consuming), Negative = discharging (providing)
        except asyncio.TimeoutError:
            marstek_error = "Battery connection timeout"
        except Exception as e:
            marstek_error = f"Battery error: {str(e)[:50]}"
        
        # Calculate house consumption with battery power included
        house_w = extract_house_consumption_w(m, battery_power_w)
        
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
            async with myenergi_lock:
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
# Simple Battery Rule Engine (export-driven, manual setpoints)
# =========================
class SimpleRuleState:
    def __init__(self):
        self.enabled: bool = False
        self.task: Optional[asyncio.Task] = None
        # defaults (can be overridden via enable payload)
        self.cfg: Dict[str, Any] = {
            "buffer_w": 200,
            "export_margin_w": 100,
            "threshold_start_w": 150,
            "threshold_stop_w": 100,
            "ramp_step_w": 150,        # per tick
            "loop_interval_s": 0.5,    # 500ms
            "cooldown_s": 8,
            "max_batt_total_w": 5000,  # total across all batteries
            "per_battery_max_w": 2500, # hard cap per battery
        }
        self.last: Dict[str, Any] = {
            "grid_w": None,
            "overschot_w": 0,
            "target_export_w": 0,
            "batt_target_total_w": 0,
            "batt_set_total_w": 0,
            "per_battery": {},
            "cooldown": False,
            "ts": None,
            "source": "zappi_ct",
            "health": {
                "myenergi_ok": False,
                "myenergi_fail_count": 0,
                "last_myenergi_ok_ts": None,
                "simple_rule_ok": False,
                "simple_rule_fail_count": 0,
                "last_simple_rule_ok_ts": None,
            },
        }
        self.prev_set_total: float = 0.0
        self.cooldown_until: float = 0.0

simple_rule = SimpleRuleState()
def _extract_grid_from_raw(raw: Dict[str, Any]) -> Optional[int]:
    """Prefer Zappi CT ectp4..6 sum. Fallback to Eddi 'grd' or top-level 'grd'."""
    try:
        blocks = raw.get("raw") or []
        z_sum = None
        for b in blocks:
            if "zappi" in b and isinstance(b["zappi"], list):
                for z in b["zappi"]:
                    try:
                        e4 = int(z.get("ectp4") or 0)
                        e5 = int(z.get("ectp5") or 0)
                        e6 = int(z.get("ectp6") or 0)
                        z_sum = (z_sum or 0) + (e4 + e5 + e6)
                    except Exception:
                        continue
        if z_sum is not None:
            return z_sum
        # fallback: see if eddi.grd exists
        for b in blocks:
            if "eddi" in b and isinstance(b["eddi"], list):
                for e in b["eddi"]:
                    if e.get("grd") is not None:
                        return int(e.get("grd"))
        # last chance: top-level
        if isinstance(raw, dict) and raw.get("grd") is not None:
            return int(raw.get("grd"))
    except Exception:
        return None
    return None

async def _set_battery_power(bid: str, power_w: int) -> Dict[str, Any]:
    """Helper to send manual charge setpoint to a battery id; power_w=0 -> stop."""
    try:
        if power_w and power_w > 0:
            payload = {"action": "charge", "power_w": int(power_w)}
        else:
            payload = {"action": "stop"}
        # reuse endpoint logic directly
        result = await battery_control_by_id(bid, payload)  # type: ignore[arg-type]
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

async def _simple_rule_loop():
    global simple_rule
    cfg = simple_rule.cfg
    alpha = 0.3  # light smoothing for overschot
    ema_overschot = 0.0
    while simple_rule.enabled:
        t0 = time.time()
        try:
            # fetch myenergi raw with small retry/backoff and compute grid
            data = None
            last_err = None
            for attempt in range(3):
                try:
                    data = await myenergi.status_all()
                    break
                except Exception as e:
                    last_err = str(e)
                    await asyncio.sleep(0.2 * (attempt + 1))
            grid_w = _extract_grid_from_raw(data) if data is not None else None
            if grid_w is None:
                # no data -> safe stop
                target_total = 0
                simple_rule.last.update({
                    "grid_w": None,
                    "overschot_w": 0,
                    "target_export_w": cfg["buffer_w"] + cfg["export_margin_w"],
                    "batt_target_total_w": target_total,
                    "batt_set_total_w": 0,
                    "per_battery": {},
                    "cooldown": False,
                    "ts": time.time(),
                    "error": last_err or "no_grid_data",
                })
                # degrade health
                try:
                    h = simple_rule.last.get("health", {})
                    h["myenergi_ok"] = False
                    h["myenergi_fail_count"] = int(h.get("myenergi_fail_count", 0)) + 1
                    simple_rule.last["health"] = h
                except Exception:
                    pass
                # stop all
                for it in (await list_batteries())['items']:  # type: ignore[index]
                    await _set_battery_power(it['id'], 0)
            else:
                overschot_raw = max(0, -int(grid_w))
                ema_overschot = alpha * overschot_raw + (1 - alpha) * ema_overschot
                target_export = cfg["buffer_w"] + cfg["export_margin_w"]
                error = ema_overschot - target_export

                # cooldown logic
                now = time.time()
                in_cooldown = now < simple_rule.cooldown_until

                target_total = simple_rule.prev_set_total
                if grid_w >= 0:
                    # importing -> immediate stop + cooldown
                    target_total = 0
                    simple_rule.cooldown_until = now + cfg["cooldown_s"]
                else:
                    if error > cfg["threshold_start_w"] and not in_cooldown:
                        # ramp up proportionally but bounded
                        step = min(cfg["ramp_step_w"], int(error))
                        target_total = min(cfg["max_batt_total_w"], simple_rule.prev_set_total + step)
                    elif error < -cfg["threshold_stop_w"]:
                        # ramp down quickly
                        step = cfg["ramp_step_w"]
                        target_total = max(0, simple_rule.prev_set_total - step)
                        if target_total == 0:
                            simple_rule.cooldown_until = now + cfg["cooldown_s"]
                    # else hold

                # distribute across batteries with per-battery cap and leftover redistribution
                per: Dict[str, Any] = {}
                items = (await list_batteries())['items']  # type: ignore[index]
                n = max(1, len(items))
                setpoints: Dict[str, int] = {}
                remaining = int(target_total)
                base_share = int(target_total / n) if n > 0 else 0
                base_share = min(base_share, int(simple_rule.cfg.get("per_battery_max_w", 2500)))
                # first pass: assign base share
                for it in items:
                    bid = it['id']
                    sp = max(0, min(base_share, remaining))
                    setpoints[bid] = sp
                    remaining -= sp
                # second pass: distribute leftover up to per-battery max
                if remaining > 0 and items:
                    cap = int(simple_rule.cfg.get("per_battery_max_w", 2500))
                    idx = 0
                    L = len(items)
                    while remaining > 0 and idx < L * 2:  # limited cycles
                        bid = items[idx % L]['id']
                        space = max(0, cap - setpoints.get(bid, 0))
                        if space > 0:
                            give = min(space, remaining)
                            setpoints[bid] = setpoints.get(bid, 0) + give
                            remaining -= give
                        idx += 1
                # apply setpoints
                set_total = 0
                for it in items:
                    bid = it['id']
                    sp = int(setpoints.get(bid, 0))
                    res = await _set_battery_power(bid, sp)
                    per[bid] = {"set": sp, "ok": bool(res.get("success"))}
                    set_total += sp

                simple_rule.prev_set_total = set_total
                # mark health ok
                try:
                    h = simple_rule.last.get("health", {})
                    h.update({"myenergi_ok": True, "myenergi_fail_count": 0, "last_myenergi_ok_ts": time.time()})
                    simple_rule.last["health"] = h
                except Exception:
                    pass
                simple_rule.last.update({
                    "grid_w": grid_w,
                    "overschot_w": int(ema_overschot),
                    "target_export_w": target_export,
                    "batt_target_total_w": int(target_total),
                    "batt_set_total_w": int(set_total),
                    "per_battery": per,
                    "cooldown": in_cooldown,
                    "ts": time.time(),
                })
        except Exception as e:
            simple_rule.last.update({"error": str(e), "ts": time.time()})
        # sleep remaining interval
        dt = time.time() - t0
        await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))

@app.post("/api/simple_rule/enable")
async def simple_rule_enable(payload: Dict[str, Any] = Body(default={})):  # type: ignore[assignment]
    """Enable the simple export-driven battery rule engine.
    Optional payload overrides defaults: buffer_w, export_margin_w, threshold_start_w, threshold_stop_w, ramp_step_w, loop_interval_s, cooldown_s, max_batt_total_w
    """
    if simple_rule.enabled and simple_rule.task and not simple_rule.task.done():
        return {"success": True, "status": "already_enabled", "cfg": simple_rule.cfg}
    # merge cfg
    for k, v in (payload or {}).items():
        if k in simple_rule.cfg:
            simple_rule.cfg[k] = v
    simple_rule.enabled = True
    simple_rule.prev_set_total = 0
    simple_rule.cooldown_until = 0
    simple_rule.task = asyncio.create_task(_simple_rule_loop())
    return {"success": True, "status": "enabled", "cfg": simple_rule.cfg}

@app.post("/api/simple_rule/disable")
async def simple_rule_disable():
    if not simple_rule.enabled:
        return {"success": True, "status": "already_disabled"}
    simple_rule.enabled = False
    if simple_rule.task:
        try:
            simple_rule.task.cancel()
        except Exception:
            pass
    # stop batteries safely
    try:
        items = (await list_batteries())['items']  # type: ignore[index]
        for it in items:
            await _set_battery_power(it['id'], 0)
    except Exception:
        pass
    return {"success": True, "status": "disabled"}

@app.get("/api/simple_rule/status")
async def simple_rule_status():
    return {"success": True, "enabled": simple_rule.enabled, "last": simple_rule.last, "cfg": simple_rule.cfg}

# ---------------------------------
# Startup/shutdown: auto-start simple rule
# ---------------------------------
@app.on_event("startup")
async def _startup_simple_rule():
    """Auto-start the simple export-driven rule on app boot.
    Keeps behavior resilient after crashes/restarts.
    """
    try:
        # If already running, do nothing
        if simple_rule.enabled and simple_rule.task and not simple_rule.task.done():
            return
        # Start with default cfg; can be overridden later via API
        simple_rule.enabled = True
        simple_rule.prev_set_total = 0
        simple_rule.cooldown_until = 0
        simple_rule.task = asyncio.create_task(_simple_rule_loop())
        logger.info("🚀 Simple Rule auto-started on startup")
    except Exception as e:
        logger.error(f"❌ Failed to auto-start Simple Rule: {e}")

@app.on_event("shutdown")
async def _shutdown_simple_rule():
    """Ensure the simple rule loop stops cleanly on shutdown."""
    try:
        if simple_rule.task and not simple_rule.task.done():
            try:
                simple_rule.task.cancel()
            except Exception:
                pass
        simple_rule.enabled = False
        logger.info("🛑 Simple Rule stopped on shutdown")
    except Exception as e:
        logger.error(f"❌ Failed to stop Simple Rule on shutdown: {e}")

# =========================
# Health and Logs endpoints
# =========================
@app.get("/api/health")
async def api_health():
    try:
        sr = {"enabled": simple_rule.enabled, "last": simple_rule.last}
        return {"success": True, "simple_rule": sr}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/logs/tail")
async def api_logs_tail(n: int = 200):
    try:
        path = LOG_FILE
        if not path or not os.path.exists(path):
            return {"success": False, "error": "log file not found", "path": path}
        # Tail last n lines efficiently
        lines = []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = -1024
            data = b""
            while len(lines) <= n and -block < size:
                f.seek(block, os.SEEK_END)
                data = f.read(-block) + data
                lines = data.splitlines()
                block *= 2
        text_lines = [ln.decode("utf-8", errors="ignore") for ln in lines[-n:]]
        return {"success": True, "lines": text_lines, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/flow.html")
async def flow_visualization_page():
    """Serve the energy flow visualization page."""
    try:
        with open("flow.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"Flow page not available: {e}", status_code=500)

# ----------------------
# Helpers for per-battery config/control
# ----------------------
def _get_modbus_for(bid: str):
    try:
        if bid == "venus_ev2_92":
            return venus_modbus
        if bid == "venus_ev2_74":
            return venus_modbus2
    except Exception:
        pass
    return venus_modbus

def _get_or_init_battery_config(cfg: Dict[str, Any], bid: str) -> Dict[str, Any]:
    if bid not in cfg:
        cfg[bid] = {
            "minimum_soc_percent": 20.0,
            "auto_charge_enabled": True,
            "original_work_mode": None,
            "emergency_charge_active": False,
        }
    return cfg[bid]

@app.post("/api/batteries/{bid}/control")
async def battery_control_by_id(bid: str, payload: Dict[str, Any] = Body(...)):
    """Generic control endpoint: action in {'charge','discharge','stop'}, optional power_w."""
    entry = _get_entry_for(bid)
    if not entry:
        return {"success": False, "error": f"unknown battery id: {bid}"}
    action = str(payload.get("action") or "").strip().lower()
    power_w = payload.get("power_w")
    if action not in {"charge", "discharge", "stop"}:
        return {"success": False, "error": "invalid action"}
    client = entry['client']
    lock = entry['lock']
    async with lock:
        try:
            result = client.set_control(action, power_w)
        except Exception as e:
            return {"success": False, "error": str(e)}
    return {"success": bool(result.get("ok")), **result, "id": bid}

@app.post("/api/batteries/{bid}/mode")
async def battery_mode_by_id(bid: str, payload: Dict[str, Any] = Body(...)):
    """Generic work mode endpoint. Payload: { mode: 0|1|2|3 }"""
    entry = _get_entry_for(bid)
    if not entry:
        return {"success": False, "error": f"unknown battery id: {bid}"}
    try:
        mode = int(payload.get("mode"))
    except Exception:
        return {"success": False, "error": "invalid mode"}
    client = entry['client']
    lock = entry['lock']
    async with lock:
        try:
            result = client.set_work_mode(mode)
        except Exception as e:
            return {"success": False, "error": str(e)}
    return {"success": bool(result.get("ok")), **result, "id": bid}

@app.post("/api/battery/diagnostics/work_mode")
async def diagnostics_work_mode(payload: Dict[str, Any] = Body(default={})):  
    """Diagnose setting user work mode by trying multiple unit IDs and tokens.
    Optional payload: { "mode": 0|1|2|3 }
    Returns attempts and readbacks for 42000/42001/35100.
    """
    try:
        mode = payload.get("mode")
        if mode is None:
            mode = 1
        try:
            mode = int(mode)
        except Exception:
            return {"success": False, "error": "invalid mode"}

        report = {"attempts": [], "reads_before": {}, "reads_after": {}, "mode": mode}
        async with modbus_lock:
            if not venus_modbus.connected and not venus_modbus.connect():
                return {"success": False, "error": "connect failed"}

            client = venus_modbus.client
            # Read before
            for addr in (42000, 42001, 35100):
                try:
                    if addr >= 40000:
                        rr = client.read_holding_registers(address=addr, count=1, slave=1)
                    else:
                        rr = client.read_input_registers(address=addr, count=1, slave=1)
                    if hasattr(rr, 'registers') and not rr.isError():
                        report["reads_before"][addr] = rr.registers[0]
                except Exception:
                    report["reads_before"][addr] = None

            # Try control enable tokens for units
            units_to_try = list(range(1, 11)) + [0, 247]
            en_tokens = [21930, 43605, 1]
            for unit in units_to_try:
                for tok in en_tokens:
                    try:
                        rr = client.write_register(address=42000, value=tok, unit=unit)
                        ok = (not getattr(rr, 'isError', lambda: False)())
                        report["attempts"].append({"addr": 42000, "val": tok, "unit": unit, "ok": ok})
                        if ok:
                            break
                    except Exception as e:
                        report["attempts"].append({"addr": 42000, "val": tok, "unit": unit, "ok": False, "err": str(e)})
                else:
                    continue
                break

            # Try writing 42001
            wrote = False
            for unit in units_to_try:
                try:
                    rr = client.write_register(address=42001, value=mode, unit=unit)
                    ok = (not getattr(rr, 'isError', lambda: False)())
                    report["attempts"].append({"addr": 42001, "val": mode, "unit": unit, "ok": ok})
                    if ok:
                        wrote = True
                        break
                except Exception as e:
                    report["attempts"].append({"addr": 42001, "val": mode, "unit": unit, "ok": False, "err": str(e)})

            # Read after
            for addr in (42000, 42001, 35100):
                try:
                    if addr >= 40000:
                        rr = client.read_holding_registers(address=addr, count=1, slave=1)
                    else:
                        rr = client.read_input_registers(address=addr, count=1, slave=1)
                    if hasattr(rr, 'registers') and not rr.isError():
                        report["reads_after"][addr] = rr.registers[0]
                except Exception:
                    report["reads_after"][addr] = None

            try:
                venus_modbus.disconnect()
            except Exception:
                pass
        report["success"] = True
        report["wrote"] = wrote
        return report
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/battery/set_work_mode")
async def api_set_work_mode(payload: Dict[str, Any] = Body(...)):
    """Set the main work mode using Modbus register 42001.
    Payload: { mode: 0|1|2|3 } where 0=Auto, 1=Manual, 2=Trade, 3=Backup
    """
    try:
        # Validate payload
        if payload is None or "mode" not in payload:
            return {"success": False, "error": "missing 'mode'"}
        try:
            mode = int(payload.get("mode"))
        except Exception:
            return {"success": False, "error": "invalid 'mode'"}

        if mode not in {0, 1, 2, 3}:
            return {"success": False, "error": "mode must be 0,1,2,3"}

        # Serialize Modbus access like other endpoints
        async with modbus_lock:
            result = venus_modbus.set_work_mode(mode)
        return {"success": bool(result.get("ok")), **result}
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
    try:
        ble_client = get_ble_client()
        await ble_client.disconnect()
        return {"success": True, "connected": ble_client.is_connected}
    except Exception as e:
        return {"success": False, "error": str(e)}

# =========================
# App lifecycle
# =========================
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

@app.get("/api/batteries")
async def list_batteries():
    """List available batteries (ids and hosts)."""
    return {
        "success": True,
        "items": [
            {"id": "venus_ev2_92", "host": venus_modbus.host,  "port": venus_modbus.port},
            {"id": "venus_ev2_74", "host": venus_modbus2.host, "port": venus_modbus2.port},
        ]
    }

@app.get("/api/batteries/{bid}/status")
async def battery_status_by_id(bid: str):
    """Generic status endpoint using BatteryManager by id."""
    try:
        result = await manager.read_status(bid)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery2/status")
async def get_battery2_status():
    """Get real-time battery 2 status via Modbus (WiFi converter)."""
    try:
        async with modbus_lock2:
            battery_data = venus_modbus2.read_battery_data()
            try:
                venus_modbus2.disconnect()
            except Exception:
                pass

        if battery_data:
            # Derived energy metrics
            soc = None
            try:
                soc = float(battery_data.get("soc_percent", {}).get("value"))
            except Exception:
                soc = None
            try:
                v = float(battery_data.get("battery_voltage", {}).get("value", 0.0))
            except Exception:
                v = 0.0
            try:
                i = float(battery_data.get("battery_current", {}).get("value", 0.0))
            except Exception:
                i = 0.0
            calc_power_w = v * i
            raw_bp = battery_data.get("battery_power", {})
            power_w = raw_bp.get("value") if isinstance(raw_bp, dict) else None
            if not isinstance(power_w, (int, float)):
                power_w = calc_power_w
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
                "host": venus_modbus2.host,
                "port": venus_modbus2.port,
                "timestamp": datetime.now().isoformat()
            }
        else:
            return {
                "success": False,
                "error": "No battery data available",
                "source": "modbus",
                "host": venus_modbus2.host
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "source": "modbus"
        }

@app.get("/api/battery/config")
async def get_battery_config():
    """Get current battery configuration"""
    try:
        config = load_battery_config()
        return {"success": True, "config": config["venus_e_78"]}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/batteries/{bid}/config")
async def get_battery_config_by_id(bid: str):
    """Get per-battery configuration (min SoC etc.)."""
    try:
        cfg = load_battery_config()
        bc = _get_or_init_battery_config(cfg, bid)
        # persist defaults if missing
        save_battery_config(cfg)
        return {"success": True, "config": bc, "id": bid}
    except Exception as e:
        return {"success": False, "error": str(e), "id": bid}

@app.post("/api/battery/minimum_soc")
async def api_check_minimum_soc(payload: Dict[str, Any] = Body(...)):
    """Check and enforce minimum SoC limit.
    Payload: { min_soc_percent: float, auto_charge?: bool }
    """
    try:
        min_soc = payload.get("min_soc_percent", 20.0)
        auto_charge = payload.get("auto_charge", True)
        
        if not (15.0 <= min_soc <= 100.0):
            return {"success": False, "error": "min_soc_percent must be between 15% (hardware limit) and 100%"}
        
        # Save configuration
        config = load_battery_config()
        config["venus_e_78"]["minimum_soc_percent"] = min_soc
        config["venus_e_78"]["auto_charge_enabled"] = auto_charge
        save_battery_config(config)
        
        if auto_charge:
            result = venus_modbus.check_minimum_soc(min_soc)
        else:
            # Just check, don't take action
            battery_data = venus_modbus.read_battery_data()
            if not battery_data or "soc_percent" not in battery_data:
                return {"success": False, "error": "Could not read SoC data"}
            
            current_soc = battery_data["soc_percent"]["value"]
            result = {
                "ok": True,
                "current_soc": current_soc,
                "min_soc_limit": min_soc,
                "below_limit": current_soc <= min_soc,
                "action_taken": None
            }
        
        return {"success": result["ok"], **result}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/batteries/{bid}/minimum_soc")
async def api_minimum_soc_per_battery(bid: str, payload: Dict[str, Any] = Body(...)):
    """Set/check minimum SoC limit per battery.
    Payload: { min_soc_percent: float, auto_charge?: bool }
    """
    try:
        min_soc = float(payload.get("min_soc_percent", 20.0))
        auto_charge = bool(payload.get("auto_charge", True))
        if not (15.0 <= min_soc <= 100.0):
            return {"success": False, "error": "min_soc_percent must be between 15 and 100"}

        cfg = load_battery_config()
        bc = _get_or_init_battery_config(cfg, bid)
        bc["minimum_soc_percent"] = min_soc
        bc["auto_charge_enabled"] = auto_charge
        save_battery_config(cfg)

        vm = _get_modbus_for(bid)
        if auto_charge:
            # enforce and/or start emergency charge if needed
            result = vm.check_minimum_soc(min_soc)
            ok = bool(result.get("ok", False))
            return {"success": ok, **result, "id": bid}
        else:
            # passive check
            bd = vm.read_battery_data()
            if not bd or "soc_percent" not in bd:
                return {"success": False, "error": "Could not read SoC data", "id": bid}
            current_soc = float(bd["soc_percent"]["value"]) if isinstance(bd["soc_percent"], dict) else float(bd["soc_percent"]) 
            return {
                "success": True,
                "current_soc": current_soc,
                "min_soc_limit": min_soc,
                "below_limit": current_soc <= min_soc,
                "action_taken": None,
                "id": bid,
            }
    except Exception as e:
        return {"success": False, "error": str(e), "id": bid}

@app.post("/api/battery/control")
async def set_battery_control(payload: Dict[str, Any] = Body(...)):
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

# =========================
# MyEnergi raw helpers (to inspect Harvi/CT data)
# =========================
@app.get("/api/myenergi/raw")
async def myenergi_raw():
    """Return the unmodified MyEnergi status payload for debugging CT/Harvi fields."""
    try:
        data = await myenergi.status_all()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/myenergi/summary")
async def myenergi_summary():
    """Summarize grid/export, eddi power, zappi power and any Harvi ectp* readings we can find."""
    try:
        data = await myenergi.status_all()
        grid_w = None
        eddi_w = 0
        zappi_w = 0
        harvi = []

        # Top-level grid if present
        try:
            grid_w = int(data.get("grd")) if isinstance(data.get("grd"), (int, float, str)) else None
        except Exception:
            grid_w = None

        # Walk devices
        for key in ("eddi", "zappi", "harvi", "as", "devices"):
            devs = data.get(key)
            if not isinstance(devs, list):
                continue
            for d in devs:
                typ = d.get("typ") or d.get("type") or key
                # Eddi
                if str(typ).lower().startswith("eddi") or key == "eddi":
                    try:
                        # 'div' diverter power (W) commonly used
                        eddi_w += int(d.get("div", 0) or 0)
                    except Exception:
                        pass
                    # Some payloads expose grid under device as 'grd'
                    if grid_w is None and d.get("grd") is not None:
                        try:
                            grid_w = int(d.get("grd"))
                        except Exception:
                            pass
                # Zappi
                if str(typ).lower().startswith("zappi") or key == "zappi":
                    try:
                        zappi_w += int(d.get("ectp1", 0) or 0)
                    except Exception:
                        pass
                    if grid_w is None and d.get("grd") is not None:
                        try:
                            grid_w = int(d.get("grd"))
                        except Exception:
                            pass
                # Harvi (wireless CT): ectp1..3 values
                if str(typ).lower().startswith("harvi") or key == "harvi":
                    rec = {
                        "sn": d.get("sno") or d.get("serial") or d.get("sn"),
                        "ectp1": d.get("ectp1"),
                        "ectp2": d.get("ectp2"),
                        "ectp3": d.get("ectp3"),
                        "ct1": d.get("ct1"),
                        "ct2": d.get("ct2"),
                        "ct3": d.get("ct3"),
                        "grd": d.get("grd"),
                    }
                    harvi.append(rec)

        return {
            "success": True,
            "grid_w": grid_w,
            "eddi_w": eddi_w,
            "zappi_w": zappi_w,
            "harvi": harvi,
            "raw_keys": list(data.keys()) if isinstance(data, dict) else None,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

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


# =========================



# =========================
# Simple Energy Rules Engine
# =========================

import json
import time
from datetime import datetime

ENERGY_RULES_FILE = "energy_rules.json"

def load_energy_rules():
    """Load energy rules from JSON file."""
    try:
        with open(ENERGY_RULES_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load energy rules: {e}")
        return {"rules": [], "global_settings": {}}

def save_energy_rules(rules_data):
    """Save energy rules to JSON file."""
    try:
        rules_data["global_settings"]["last_updated"] = time.time()
        with open(ENERGY_RULES_FILE, "w") as f:
            json.dump(rules_data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save energy rules: {e}")
        return False

class SimpleRulesEngine:
    def __init__(self):
        self.last_execution = {}
        self.last_battery_commands = {}
        self.running = False

    async def start_rules_loop(self):
        """Start the rules execution loop."""
        self.running = True
        logger.info("🎯 Rules Engine started")
        
        while self.running:
            try:
                await self.execute_active_rules()
                await asyncio.sleep(2)  # Check every 2 seconds
            except Exception as e:
                logger.error(f"Rules loop error: {e}")
                await asyncio.sleep(10)  # Wait longer on error
    
    def stop_rules_loop(self):
        """Stop the rules execution loop."""
        self.running = False
        logger.info("🛑 Rules Engine stopped")

    async def execute_active_rules(self):
        """Execute all active rules with mode management."""
        try:
            rules_data = load_energy_rules()
            active_rules = [r for r in rules_data.get("rules", []) if r.get("active", False)]
            
            # Determine target mode
            target_mode = await mode_manager.determine_target_mode(len(active_rules) > 0)
            
            # Ensure correct mode
            mode_ok = await mode_manager.ensure_correct_mode(target_mode)
            
            if not mode_ok:
                return  # Mode switch failed, try again later
            
            # Only execute rules if we are in rules mode
            if target_mode == "manual_rules" and active_rules:
                # Get current system data
                myenergi_data = await self.get_myenergi_data()
                battery_data = await self.get_battery_data()
                
                if myenergi_data and battery_data:
                    for rule in active_rules:
                        await self.execute_rule(rule, myenergi_data, battery_data)
            elif target_mode == "manual_user":
                logger.debug("👤 User override active - skipping rules")
            elif target_mode == "anti_feed":
                logger.debug("🔋 Anti-Feed mode - battery controls itself")
                
        except Exception as e:
            logger.error(f"Rules engine error: {e}")
    def __init__(self):
        self.last_execution = {}
        self.last_battery_commands = {}
    
    async def execute_active_rules(self):
        """Execute all active rules."""
        try:
            rules_data = load_energy_rules()
            
            # Get current system data
            myenergi_data = await self.get_myenergi_data()
            battery_data = await self.get_battery_data()
            
            if not myenergi_data or not battery_data:
                return
            
            # Execute each active rule
            for rule in rules_data.get("rules", []):
                if rule.get("active", False):
                    await self.execute_rule(rule, myenergi_data, battery_data)
                    
        except Exception as e:
            logger.error(f"Rules engine error: {e}")
    
    async def execute_rule(self, rule, myenergi_data, battery_data):
        """Execute a specific rule."""
        rule_id = rule.get("id")
        
        if rule_id == "eddi_priority":
            await self.execute_eddi_priority_rule(rule, myenergi_data, battery_data)
    
    async def execute_eddi_priority_rule(self, rule, myenergi_data, battery_data):
        """Execute the Eddi Priority rule."""
        try:
            # Extract data
            export_w = myenergi_data.get("grid_export_w", 0)  # Positive = export
            eddi_w = myenergi_data.get("eddi_power_w", 0)
            
            # Rule parameters
            params = rule.get("parameters", {})
            export_threshold = params.get("export_threshold_w", 100)
            eddi_buffer = params.get("eddi_buffer_w", 200)
            max_battery_w = params.get("max_battery_power_w", 1500)
            
            # Core logic: Export stoplicht
            if export_w < export_threshold:
                # No export = stop all batteries
                target_power = 0
                reason = f"No export ({export_w}W < {export_threshold}W)"
            else:
                # Calculate available power for batteries
                available = export_w - eddi_w - eddi_buffer
                target_power = max(0, min(available, max_battery_w))
                reason = f"Export {export_w}W - Eddi {eddi_w}W - Buffer {eddi_buffer}W = {available}W"
            
            logger.info(f"🔥 EDDI PRIORITY: {reason} → Battery target: {target_power}W")
            
            # Apply to selected batteries
            batteries = rule.get("batteries", {})
            allow = {"venus_e_78"}
            for battery_id, enabled in batteries.items():
                if enabled and battery_id in allow:
                    await self.set_battery_power(battery_id, target_power)
                elif enabled and battery_id not in allow:
                    logger.info(f"🎯 RULE EXEC: Skipping non-allowed battery '{battery_id}'")
                    
        except Exception as e:
            logger.error(f"Eddi priority rule error: {e}")
    
    async def get_myenergi_data(self):
        """Get MyEnergi data."""
        try:
            async with myenergi_lock:
                status = await myenergi.status_all()
            
            return {
                "grid_export_w": extract_grid_export_w(status),
                "eddi_power_w": extract_eddi_power_w(status),
                "pv_generation_w": extract_pv_generation_w(status)
            }
        except Exception as e:
            logger.error(f"Failed to get MyEnergi data: {e}")
            return None
    
    async def get_battery_data(self):
        """Get battery data."""
        try:
            soc = await asyncio.wait_for(marstek.get_soc(), timeout=2.0)
            return {
                "soc": soc.value if soc and hasattr(soc, "value") else 0
            }
        except Exception as e:
            logger.error(f"Failed to get battery data: {e}")
            return None
    
    async def set_battery_power(self, battery_id, power_w):
        """Set battery charging power."""
        try:
            # Avoid sending same command repeatedly
            if self.last_battery_commands.get(battery_id) == power_w:
                return
            
            if power_w <= 0:
                result = marstek.set_control("stop")
            else:
                result = marstek.set_control("charge", power_w)
            
            if result.get("ok", False):
                self.last_battery_commands[battery_id] = power_w
                logger.info(f"✅ Battery {battery_id}: {power_w}W")
            else:
                logger.error(f"❌ Battery {battery_id}: Failed to set {power_w}W")
                
        except Exception as e:
            logger.error(f"Failed to set battery {battery_id} power: {e}")

# Global rules engine
rules_engine = SimpleRulesEngine()

@app.get("/api/energy_rules")
async def get_energy_rules():
    """Get current energy rules configuration."""
    try:
        rules_data = load_energy_rules()
        return {"success": True, "data": rules_data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/energy_rules")
async def update_energy_rules(rules_data: dict):
    """Update energy rules configuration."""
    try:
        if save_energy_rules(rules_data):
            return {"success": True, "message": "Rules updated"}
        else:
            return {"success": False, "error": "Failed to save rules"}
    except Exception as e:
        return {"success": False, "error": str(e)}



# =========================
# Mode Management & User Override Detection
# =========================

class ModeManager:
    def __init__(self):
        self.user_override_active = False
        self.last_user_action = 0
        self.current_mode = "unknown"
        self.last_mode_switch = 0
        
    def detect_user_override(self):
        """Detect if user has manually controlled battery."""
        # This would be set by frontend when user presses buttons
        # For now, we can detect by checking if manual commands were sent recently
        current_time = time.time()
        
        # If user action within last 5 minutes, consider override active
        if current_time - self.last_user_action < 300:  # 5 minutes
            self.user_override_active = True
        else:
            self.user_override_active = False
            
        return self.user_override_active
    
    def set_user_action(self):
        """Mark that user has taken manual action."""
        self.last_user_action = time.time()
        self.user_override_active = True
        logger.info("👤 USER OVERRIDE: Manual control detected")
    
    async def determine_target_mode(self, rules_active=False):
        """Determine what mode battery should be in with logging."""
        logger.info(f"🔍 MODE DEBUG: Determining target mode - rules_active={rules_active}")
        """Determine what mode battery should be in."""
        if self.detect_user_override():
            return "manual_user"  # User has control
        elif rules_active:
            return "manual_rules"  # Rules have control
        else:
            return "anti_feed"  # Battery controls itself
    
    async def ensure_correct_mode(self, target_mode):
        """Ensure battery is in correct mode."""
        current_time = time.time()
        
        # Avoid too frequent mode switches (1 minute hysteresis)
        if current_time - self.last_mode_switch < 60:
            return False
            
        mode_map = {
            "manual_user": 1,    # Manual mode for user
            "manual_rules": 1,   # Manual mode for rules  
            "anti_feed": 0       # Anti-Feed mode
        }
        
        target_mode_value = mode_map.get(target_mode, 0)
        
        if self.current_mode != target_mode:
            logger.info(f"🔄 MODE SWITCH: {self.current_mode} → {target_mode}")
            
            # Use existing set_work_mode function
            result = await set_work_mode("venus_e_78", target_mode_value)
            
            if result.get("success", False):
                self.current_mode = target_mode
                self.last_mode_switch = current_time
                return True
            else:
                logger.error(f"❌ Failed to switch to {target_mode}")
                return False
        
        return True  # Already in correct mode

# Global mode manager
mode_manager = ModeManager()

# Update existing battery control functions to detect user actions
original_set_battery_mode = globals().get("set_battery_mode")
original_set_battery_power = globals().get("set_battery_power")

async def set_battery_mode_with_override(*args, **kwargs):
    """Wrapper to detect user override."""
    mode_manager.set_user_action()
    if original_set_battery_mode:
        return await original_set_battery_mode(*args, **kwargs)

async def set_battery_power_with_override(*args, **kwargs):
    """Wrapper to detect user override.""" 
    mode_manager.set_user_action()
    if original_set_battery_power:
        return await original_set_battery_power(*args, **kwargs)

# Override the functions
if original_set_battery_mode:
    globals()["set_battery_mode"] = set_battery_mode_with_override
if original_set_battery_power:
    globals()["set_battery_power"] = set_battery_power_with_override



# =========================
# Startup: Start Rules Engine
# =========================

@app.on_event("shutdown") 
async def stop_rules_engine():
    """Stop the rules engine on app shutdown."""
    try:
        rules_engine.stop_rules_loop()
        logger.info("✅ Rules Engine stopped successfully")
    except Exception as e:
        logger.error(f"❌ Failed to stop Rules Engine: {e}")

# API endpoint to manually trigger user override reset
@app.post("/api/rules/reset_override")
async def reset_user_override():
    """Reset user override to allow rules to take control again."""
    try:
        mode_manager.user_override_active = False
        mode_manager.last_user_action = 0
        return {"success": True, "message": "User override reset"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# API endpoint to get current mode status
@app.get("/api/rules/status")
async def get_rules_status():
    """Get current rules and mode status."""
    try:
        rules_data = load_energy_rules()
        active_rules = [r for r in rules_data.get("rules", []) if r.get("active", False)]
        
        return {
            "success": True,
            "current_mode": mode_manager.current_mode,
            "user_override": mode_manager.user_override_active,
            "active_rules_count": len(active_rules),
            "rules_engine_running": getattr(rules_engine, "running", False)
        }
    except Exception as e:
        return {"success": False, "error": str(e)}



# =========================
# Enhanced Rules Engine Debugging
# =========================

@app.get("/api/rules/debug")
async def debug_rules_engine():
    """Debug endpoint to see what the rules engine is doing."""
    try:
        # Get current rules
        rules_data = load_energy_rules()
        active_rules = [r for r in rules_data.get("rules", []) if r.get("active", False)]
        
        # Get current system data
        myenergi_data = await rules_engine.get_myenergi_data()
        battery_data = await rules_engine.get_battery_data()
        
        # Determine what mode should be active
        target_mode = await mode_manager.determine_target_mode(len(active_rules) > 0)
        
        debug_info = {
            "timestamp": time.time(),
            "rules_engine_running": getattr(rules_engine, "running", False),
            "active_rules_count": len(active_rules),
            "active_rules": [r.get("name", "Unknown") for r in active_rules],
            "current_mode": mode_manager.current_mode,
            "target_mode": target_mode,
            "user_override": mode_manager.user_override_active,
            "last_user_action": mode_manager.last_user_action,
            "last_mode_switch": mode_manager.last_mode_switch,
            "myenergi_data": myenergi_data,
            "battery_data": battery_data,
            "mode_switch_cooldown_remaining": max(0, 60 - (time.time() - mode_manager.last_mode_switch))
        }
        
        return {"success": True, "debug": debug_info}
        
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": str(e.__traceback__)}

# Add more logging to the rules engine
class EnhancedSimpleRulesEngine(SimpleRulesEngine):
    async def execute_active_rules(self):
        """Execute all active rules with enhanced logging."""
        try:
            logger.info("🔍 RULES DEBUG: Checking active rules...")
            
            rules_data = load_energy_rules()
            active_rules = [r for r in rules_data.get("rules", []) if r.get("active", False)]
            
            logger.info(f"🔍 RULES DEBUG: Found {len(active_rules)} active rules")
            
            # Determine target mode
            target_mode = await mode_manager.determine_target_mode(len(active_rules) > 0)
            logger.info(f"🔍 RULES DEBUG: Target mode: {target_mode}, Current mode: {mode_manager.current_mode}")
            
            # Ensure correct mode
            mode_ok = await mode_manager.ensure_correct_mode(target_mode)
            logger.info(f"🔍 RULES DEBUG: Mode switch OK: {mode_ok}")
            
            if not mode_ok:
                logger.warning("🔍 RULES DEBUG: Mode switch failed, skipping rule execution")
                return
            
            # Only execute rules if we are in rules mode
            if target_mode == "manual_rules" and active_rules:
                logger.info("🔍 RULES DEBUG: Executing rules in manual_rules mode")
                
                # Get current system data
                myenergi_data = await self.get_myenergi_data()
                battery_data = await self.get_battery_data()
                
                if myenergi_data and battery_data:
                    logger.info(f"🔍 RULES DEBUG: MyEnergi data: {myenergi_data}")
                    logger.info(f"🔍 RULES DEBUG: Battery data: {battery_data}")
                    
                    for rule in active_rules:
                        logger.info(f"🔍 RULES DEBUG: Executing rule: {rule.get(name)}")
                        await self.execute_rule(rule, myenergi_data, battery_data)
                else:
                    logger.warning("🔍 RULES DEBUG: No system data available")
                    
            elif target_mode == "manual_user":
                logger.info("🔍 RULES DEBUG: User override active - skipping rules")
            elif target_mode == "anti_feed":
                logger.info("🔍 RULES DEBUG: Anti-Feed mode - battery controls itself")
            else:
                logger.info(f"🔍 RULES DEBUG: Unknown target mode: {target_mode}")
                
        except Exception as e:
            logger.error(f"🔍 RULES DEBUG: Error in execute_active_rules: {e}")

# Replace the rules engine with enhanced version
rules_engine = EnhancedSimpleRulesEngine()



# =========================
# Temperature Override Support
# =========================

async def get_tank_temperature_with_override(rule_params):
    """Get tank temperature with optional override for testing."""
    try:
        # Check if override is enabled
        temp_override = rule_params.get("tank_temp_override")
        
        if temp_override is not None:
            logger.info(f"🌡️ TEMP OVERRIDE: Using manual temperature {temp_override}°C")
            return float(temp_override)
        
        # Normal temperature reading from MyEnergi
        myenergi_data = await myenergi.get_status()
        if myenergi_data and "eddi" in myenergi_data:
            eddi_data = myenergi_data["eddi"][0] if myenergi_data["eddi"] else {}
            tank_temp = eddi_data.get("tp2", 0)  # Tank 2 temperature
            logger.info(f"🌡️ REAL TEMP: Tank 2 temperature {tank_temp}°C")
            return float(tank_temp)
        
        logger.warning("🌡️ TEMP WARNING: No temperature data available")
        return 0.0
        
    except Exception as e:
        logger.error(f"🌡️ TEMP ERROR: {e}")
        return 0.0

# Update the rule execution to use temperature override
class EnhancedSimpleRulesEngine(SimpleRulesEngine):
    async def execute_rule(self, rule, myenergi_data, battery_data):
        """Execute a single rule with temperature override support."""
        try:
            rule_id = rule.get("id")
            rule_name = rule.get("name", "Unknown")
            rule_params = rule.get("parameters", {})
            
            logger.info(f"🎯 RULE EXEC: Executing {rule_name}")
            
            if rule_id == "eddi_priority":
                await self.execute_eddi_priority_rule(rule, myenergi_data, battery_data, rule_params)
            else:
                logger.warning(f"🎯 RULE EXEC: Unknown rule type: {rule_id}")
                
        except Exception as e:
            logger.error(f"🎯 RULE EXEC ERROR: {e}")
    
    async def execute_eddi_priority_rule(self, rule, myenergi_data, battery_data, rule_params):
        """Execute Eddi Priority rule with temperature checking."""
        try:
            # Get current system values
            grid_w = myenergi_data.get("grid_w", 0)
            eddi_w = myenergi_data.get("eddi_w", 0)
            
            # Get tank temperature (with override support)
            tank_temp = await get_tank_temperature_with_override(rule_params)
            target_temp = rule_params.get("tank_temp_target", 60)
            
            logger.info(f"🔥 EDDI RULE: Grid={grid_w}W, Eddi={eddi_w}W, Tank={tank_temp}°C (target={target_temp}°C)")
            
            # Check if tank is warm enough
            if tank_temp < target_temp:
                logger.info(f"🔥 EDDI RULE: Tank too cold ({tank_temp}°C < {target_temp}°C) - Eddi has priority")
                # Set battery to minimal power or stop charging
                await self.set_battery_minimal_power(rule)
                return
            
            # Tank is warm enough, apply normal Eddi priority logic
            export_w = max(0, -grid_w)  # Negative grid = export
            buffer_w = rule_params.get("eddi_buffer_w", 200)
            threshold_w = rule_params.get("export_threshold_w", 100)
            
            available_for_battery = export_w - eddi_w - buffer_w
            
            logger.info(f"�� EDDI RULE: Export={export_w}W, Available for battery={available_for_battery}W")
            
            if available_for_battery > threshold_w:
                max_battery_w = rule_params.get("max_battery_power_w", 1500)
                target_power = min(available_for_battery, max_battery_w)
                logger.info(f"🔥 EDDI RULE: Setting battery to {target_power}W")
                await self.set_battery_power(rule, target_power)
            else:
                logger.info(f"🔥 EDDI RULE: Not enough surplus ({available_for_battery}W <= {threshold_w}W)")
                await self.set_battery_minimal_power(rule)
                
        except Exception as e:
            logger.error(f"🔥 EDDI RULE ERROR: {e}")
    
    async def set_battery_minimal_power(self, rule):
        """Set battery to minimal power (stop charging)."""
        try:
            batteries = rule.get("batteries", {})
            for battery_id, enabled in batteries.items():
                if enabled and battery_id == "venus_e_78":
                    logger.info(f"🔋 Setting {battery_id} to minimal power")
                    # Set to very low power or stop
                    result = await set_battery_power("venus_e_78", 0)
                    logger.info(f"🔋 Battery power result: {result}")
        except Exception as e:
            logger.error(f"🔋 Battery minimal power error: {e}")
    
    async def set_battery_power(self, rule, power_w):
        """Set battery to specific power."""
        try:
            batteries = rule.get("batteries", {})
            for battery_id, enabled in batteries.items():
                if enabled and battery_id == "venus_e_78":
                    logger.info(f"🔋 Setting {battery_id} to {power_w}W")
                    result = await set_battery_power("venus_e_78", power_w)
                    logger.info(f"🔋 Battery power result: {result}")
        except Exception as e:
            logger.error(f"🔋 Battery power error: {e}")

# Replace the rules engine
rules_engine = EnhancedSimpleRulesEngine()

