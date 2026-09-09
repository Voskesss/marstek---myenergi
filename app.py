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
        # Smaller rotation for Raspberry Pi: 2MB per file, max 2 backups = ~6MB total
        rot = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2)
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
from datetime import datetime
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
from phase_monitor import PhaseMonitor
from p1_reader import P1Reader
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
    def __init__(self, host=None, port=None, timeout=5, retries=3):
        env_host = os.getenv('VENUS_MODBUS_HOST')
        env_port = os.getenv('VENUS_MODBUS_PORT')
        self.host = (host or env_host or '192.168.68.92')
        try:
            self.port = int(port or env_port or 502)
        except Exception:
            self.port = 502
        self.timeout = timeout
        self.retries = retries
        self.client = None
        self.connected = False
        self.last_valid_soc = None  # Cache for last known good SoC value
    
    def connect(self):
        try:
            # Close old connection first if exists
            if self.client:
                try:
                    self.client.close()
                except:
                    pass
            
            # Add a short timeout to avoid hanging sockets
            self.client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=self.retries)
            self.connected = self.client.connect()
            if not self.connected:
                logging.warning(f"Failed to connect to Modbus {self.host}:{self.port}")
            return self.connected
        except Exception as e:
            logging.error(f"Modbus connection error: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                logging.debug(f"Error closing Modbus connection: {e}")
            finally:
                self.connected = False
                self.client = None

    def read_battery_data(self):
        """Read all battery data from Venus E via Modbus"""
        try:
            if not self.connected:
                if not self.connect():
                    logging.warning(f"Modbus connection to {self.host}:{self.port} failed, returning None")
                    return None
        except Exception as e:
            logging.error(f"Modbus connect exception on {self.host}:{self.port}: {e}")
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
            result = None
            retry_count = 0
            max_retries = 3
            
            while retry_count < max_retries and result is None:
                try:
                    # Ensure we have a valid client connection
                    if not self.client or not self.connected:
                        if not self.connect():
                            retry_count += 1
                            time.sleep(0.1)
                            continue
                    
                    # Special handling for SOC register - read multiple times due to firmware timing bug
                    if reg_addr == 32104:  # SOC register
                        # Read 5 times and use most common valid value
                        soc_readings = []
                        for attempt in range(5):
                            try:
                                res = self.client.read_holding_registers(address=reg_addr, count=1, slave=1)
                                if hasattr(res, 'registers') and not res.isError():
                                    val = res.registers[0]
                                    if 0 <= val <= 100:  # Valid SOC range
                                        soc_readings.append(val)
                                time.sleep(0.05)  # Small delay between reads
                            except:
                                pass
                        
                        if soc_readings:
                            # Use most common value (majority vote)
                            from collections import Counter
                            most_common = Counter(soc_readings).most_common(1)[0][0]
                            raw_value = most_common
                            # Create mock result object
                            class MockResult:
                                def __init__(self, val):
                                    self.registers = [val]
                                def isError(self):
                                    return False
                            result = MockResult(raw_value)
                        else:
                            # No valid readings - use cached if available
                            if self.last_valid_soc:
                                battery_data[param_name] = self.last_valid_soc.copy()
                                battery_data[param_name]["timestamp"] = datetime.now().isoformat()
                                battery_data[param_name]["cached"] = True
                                logging.warning(f"⚠️ SOC: No valid reads after 5 attempts - using cache")
                            break
                    else:
                        # Normal single read for other registers
                        result = self.client.read_holding_registers(address=reg_addr, count=1, slave=1)
                    
                    if hasattr(result, 'registers') and not result.isError():
                        raw_value = result.registers[0]
                        formatted = format_value(reg_addr, raw_value)
                        
                        # format_value returns None for invalid SOC readings (Modbus race condition)
                        if formatted is None:
                            if param_name == "soc_percent" and self.last_valid_soc:
                                # Use cached SOC value for display
                                battery_data[param_name] = self.last_valid_soc.copy()
                                battery_data[param_name]["timestamp"] = datetime.now().isoformat()
                                battery_data[param_name]["cached"] = True
                                logging.debug(f"Using cached SOC value due to invalid read")
                            # Skip this register - invalid data
                            break
                        
                        battery_data[param_name] = {
                            "value": formatted.get("value", raw_value),
                            "formatted": formatted.get("formatted", str(raw_value)),
                            "unit": formatted.get("unit", ""),
                            "description": formatted.get("description", param_name),
                            "register": reg_addr,
                            "timestamp": datetime.now().isoformat()
                        }
                        break  # Success, move to next register
                    else:
                        # Error response, retry with reconnect only on last attempt
                        result = None
                        retry_count += 1
                        if retry_count >= max_retries:
                            logging.debug(f"Register {reg_addr} error after {max_retries} attempts")
                        
                except Exception as e:
                    retry_count += 1
                    error_str = str(e).lower()
                    
                    # Detect connection errors that need reconnect
                    is_connection_error = any(x in error_str for x in ["broken pipe", "connection", "bad file descriptor", "closed"])
                    
                    if is_connection_error:
                        logging.debug(f"Connection error on {reg_addr}, reconnecting...")
                        self.disconnect()
                        self.connected = False
                        if retry_count < max_retries:
                            time.sleep(0.1)  # Give device time to recover
                            continue
                    elif retry_count < max_retries:
                        # Brief pause before retry, keep connection alive
                        time.sleep(0.05)
                    else:
                        # Log only on last attempt
                        logging.warning(f"Error reading register {reg_addr} from {self.host}: {e}")
                    result = None
        
        # Calculate actual power from voltage × current if we have both
        if "battery_voltage" in battery_data and "battery_current" in battery_data:
            voltage = battery_data["battery_voltage"]["value"]
            current = battery_data["battery_current"]["value"] 
            calculated_power = voltage * current
            
            # CRITICAL: Venus E registers have pre-scaled values that need correction
            # Voltage scale: 0.1 (raw 5167 → 516.7V but should be 51.67V)
            # Current scale: 0.01 (raw -30 → -0.3A but should be -0.03A)
            # Result: V × A gives 10x too high power
            # Fix: Apply 0.1 correction factor
            corrected_power = calculated_power * 0.1
            
            # Override battery_power with corrected calculated value
            battery_data["battery_power"] = {
                "value": corrected_power,
                "formatted": f"{corrected_power:.0f} W",
                "unit": "W", 
                "description": "Battery Power (calculated, corrected)",
                "register": "calc",
                "timestamp": datetime.now().isoformat()
            }
            logging.info(f"✅ Power calc: {voltage}V × {current}A = {calculated_power}W → corrected: {corrected_power}W")
        
        # If we got no data at all, return None to signal complete failure
        if not battery_data:
            logging.error(f"No battery data retrieved from {self.host}:{self.port}")
            return None
        
        # SANITY CHECK: Validate SoC and use cached value if invalid
        if "soc_percent" in battery_data:
            soc_value = battery_data["soc_percent"]["value"]
            # Check if SoC is in valid range (0-100%)
            if 0 <= soc_value <= 100:
                # Valid - update cache
                self.last_valid_soc = battery_data["soc_percent"].copy()
            else:
                # Invalid - use cached value if available
                if self.last_valid_soc:
                    logging.warning(f"⚠️ Invalid SoC {soc_value}% from {self.host} - using cached {self.last_valid_soc['value']}%")
                    battery_data["soc_percent"] = self.last_valid_soc.copy()
                    battery_data["soc_percent"]["cached"] = True
                else:
                    logging.error(f"❌ Invalid SoC {soc_value}% from {self.host} and no cache available")
        
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
                    err_str = str(ex).lower()
                    # Reconnect on connection errors
                    if any(x in err_str for x in ["broken pipe", "connection", "bad file descriptor"]):
                        logging.debug(f"Write connection error, reconnecting...")
                        self.disconnect()
                        self.connected = False
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
                    err2_str = str(ex2).lower()
                    if any(x in err2_str for x in ["broken pipe", "connection", "bad file descriptor"]):
                        logging.debug(f"Write connection error, reconnecting...")
                        self.disconnect()
                        self.connected = False
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

    def check_minimum_soc(self, min_soc_percent: float = 20.0, hysteresis: float = 2.0, simple_rule_enabled: bool = False) -> dict:
        """Check if current SoC is above minimum and take action if needed
        Uses hysteresis to prevent toggling around the threshold
        
        Args:
            min_soc_percent: Minimum SOC threshold
            hysteresis: Hysteresis band to prevent toggling
            simple_rule_enabled: If True, emergency charge is DISABLED (Simple Rule manages charging)
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
                "action_taken": None,
                "simple_rule_enabled": simple_rule_enabled
            }
            
            # BELANGRIJK: Emergency charge alleen als Simple Rule UIT staat!
            if simple_rule_enabled:
                result.update({
                    "action_taken": "simple_rule_active",
                    "status": f"SoC {current_soc}% - Simple Rule manages charging (emergency charge disabled)"
                })
                return result
            
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

        # Track of RS485 force-sessie op deze client (voorkomt herhaald 0x55AA → klikken)
        if not hasattr(self, "_rs485_force_on"):
            self._rs485_force_on = False
            self._rs485_last_action = None
            self._rs485_last_power = None

        # Step 1: Enable control mode alleen bij start / actie-wissel (niet elke power-update)
        need_enable = action != "stop" and (
            not self._rs485_force_on
            or self._rs485_last_action != action
        )
        if need_enable:
            ok_en, tries_en = self.write_holding(REG_CONTROL_MODE, CONTROL_ENABLE)
            result["attempts"] += [{"addr": REG_CONTROL_MODE, "val": CONTROL_ENABLE, **t} for t in tries_en]
            if not ok_en:
                result["error"] = "Failed to enable control mode"
                return result
            self._rs485_force_on = True
            time.sleep(0.1)

        # Step 2: Set power and mode
        ok_cmd = False
        if action == "charge":
            ok_p, tries_p = self.write_holding(REG_CHARGE_POWER, power_w)
            result["attempts"] += [{"addr": REG_CHARGE_POWER, "val": power_w, **t} for t in tries_p]
            if self._rs485_last_action != "charge":
                ok_m, tries_m = self.write_holding(REG_SET_MODE, 1)
                result["attempts"] += [{"addr": REG_SET_MODE, "val": 1, **t} for t in tries_m]
            else:
                ok_m = True
            ok_cmd = ok_p and ok_m

        elif action == "discharge":
            ok_p, tries_p = self.write_holding(REG_DISCHARGE_POWER, power_w)
            result["attempts"] += [{"addr": REG_DISCHARGE_POWER, "val": power_w, **t} for t in tries_p]
            if self._rs485_last_action != "discharge":
                ok_m, tries_m = self.write_holding(REG_SET_MODE, 2)
                result["attempts"] += [{"addr": REG_SET_MODE, "val": 2, **t} for t in tries_m]
            else:
                ok_m = True
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
            self._rs485_force_on = False
            self._rs485_last_action = None
            self._rs485_last_power = None
        else:
            result["error"] = f"unknown action: {action}"
            return result

        # Final result
        if ok_cmd:
            result.update({"ok": True, "action": action, "power_w": power_w})
            if action != "stop":
                self._rs485_last_action = action
                self._rs485_last_power = power_w
        else:
            result["error"] = f"Command '{action}' failed."
        
        return result

# Global Modbus clients
venus_modbus = VenusEModbusClient()  # Battery 1 (default host 192.168.68.92)
# Battery 2 (WiFi converter) - needs longer timeout due to WiFi latency
venus_modbus2 = VenusEModbusClient(
    host=os.getenv('VENUS_MODBUS_HOST2', '192.168.68.74'),
    timeout=10,  # WiFi converter needs more time
    retries=5    # More retries for unstable WiFi
)
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
def _extract_grid_import_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """
    Net via Zappi CT ectp4+5+6 (zelfde bron als fase-monitor, geverifieerd t.o.v. P1).
    Positief = import van net, negatief = export naar net.
    """
    raw = myenergi_status.get("raw", myenergi_status)
    try:
        if isinstance(raw, list):
            z_sum = None
            for section in raw:
                if isinstance(section, dict) and "zappi" in section:
                    for z in section.get("zappi") or []:
                        try:
                            e4 = int(z.get("ectp4") or 0)
                            e5 = int(z.get("ectp5") or 0)
                            e6 = int(z.get("ectp6") or 0)
                            z_sum = (z_sum or 0) + (e4 + e5 + e6)
                        except Exception:
                            continue
            if z_sum is not None:
                return int(z_sum)
            for section in raw:
                if isinstance(section, dict) and "zappi" in section:
                    for z in section.get("zappi") or []:
                        if z.get("grd") is not None:
                            return int(z["grd"])
            for section in raw:
                if isinstance(section, dict) and "eddi" in section:
                    for e in section.get("eddi") or []:
                        if e.get("grd") is not None:
                            return int(e["grd"])
        else:
            items = raw if isinstance(raw, dict) else {}
            if "pgrid" in items:
                return int(items["pgrid"])
            if items.get("grd") is not None:
                return int(items["grd"])
    except Exception:
        pass
    return None

def extract_grid_export_w(myenergi_status: Dict[str, Any]) -> Optional[int]:
    """Grid export/import (positief = export, negatief = import — flow.html conventie)."""
    imp = _extract_grid_import_w(myenergi_status)
    if imp is not None:
        return -int(imp)
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

            # grid_w uit extract_grid_export_w: positief = export, negatief = import
            # Huis = PV - export - Eddi - Zappi - batterij_laden (+ ontlaad)
            house_consumption = pv_gen - grid_w - eddi_w - zappi_w - battery_power_w
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
            
            return max(0, total_generation)
                
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

# Serve background images for flow dashboard
try:
    app.mount("/bg", StaticFiles(directory="bg"), name="bg")
except Exception:
    pass

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

# P1 meter (HomeWizard compatible) - Optional
P1_METER_IP = os.getenv("P1_METER_IP", "192.168.68.73")
p1_reader = P1Reader(P1_METER_IP) if P1_METER_IP else None

# Phase monitor voor 3x25A check
phase_monitor = PhaseMonitor(myenergi, myenergi_lock, p1_reader)

@app.get("/health")
async def health():
    return {"ok": True}

_last_good_status = None  # Cache for last successful /api/status response

@app.get("/api/status")
async def get_status():
    """Samengevoegde status van myenergi + marstek."""
    global _last_good_status
    cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
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
            # get_power() returns an int (W) or None. Convention:
            #  +ve = charging (consuming), -ve = discharging (providing)
            if isinstance(power, (int, float)):
                battery_power_w = int(power)
        except asyncio.TimeoutError:
            marstek_error = "Battery connection timeout"
        except Exception as e:
            marstek_error = f"Battery error: {str(e)[:50]}"
        
        # Calculate house consumption with battery power included
        house_w = extract_house_consumption_w(m, battery_power_w)
        
        # Grid: export_w positief = export, negatief = import (flow.html conventie)
        grid_import_w = None if export_w is None else max(0, -int(export_w))
        
        payload = {
            "timestamp": time.time(),
            "myenergi_raw": m,
            "grid_export_w": export_w,
            "grid_import_w": grid_import_w,
            "grid_w": export_w,
            "eddi_power_w": eddi_w,
            "zappi_power_w": zappi_w,
            "house_consumption_w": house_w,
            "pv_generation_w": pv_w,
            "eddi_temperatures": eddi_temps,
            "should_block": should_block,
            "block_reason": block_reason,
            "marstek_soc": soc,
            "marstek_power_w": battery_power_w,
            "marstek_error": marstek_error,
            "battery_blocked": state.battery_blocked,
            "last_switch": state.last_switch,
            # Minimal derived block for legacy UI on "/" route
            "derived": {
                "grid_export_w": export_w,
                "eddi_power_w": eddi_w,
                "zappi_power_w": zappi_w,
                "house_consumption_w": house_w,
                "pv_generation_w": pv_w,
                "battery_power_w": battery_power_w,
            },
            "config": {
                "priority_mode": EDDI_PRIORITY_MODE,
                "target_temp_1": EDDI_TARGET_TEMP_1,
                "target_temp_2": EDDI_TARGET_TEMP_2,
                "use_tank_1": EDDI_USE_TANK_1,
                "use_tank_2": EDDI_USE_TANK_2,
                "active_threshold_w": EDDI_ACTIVE_W,
                "marstek_use_ble": MARSTEK_USE_BLE
            },
            "stale": False,
        }
        _last_good_status = payload
        return JSONResponse(content=payload, headers=cache_headers)
    except Exception as e:
        # Return cached data if available, so dashboard stays alive
        if _last_good_status:
            stale = dict(_last_good_status)
            stale["stale"] = True
            stale["stale_reason"] = str(e)[:120]
            stale["timestamp"] = time.time()
            return JSONResponse(content=stale, headers=cache_headers)
        return JSONResponse(content={"error": str(e), "timestamp": time.time()}, headers=cache_headers)

@app.get("/phase")
async def phase_dashboard():
    """3-Fase monitor dashboard voor 3x25A check"""
    with open("phase_dashboard.html", "r") as f:
        html = f.read()
    return HTMLResponse(html)

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
      <div style=\"margin:8px 0;display:flex;gap:12px;\">
        <a href=\"/flow.html\" style=\"background:#2563eb;color:#fff;text-decoration:none;padding:8px 16px;border-radius:8px;font-weight:600;\">⚡ Energie Flow</a>
        <a href=\"/setup\" style=\"color:#93c5fd;padding:8px 0;\">⚙️ Setup</a>
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
# Daily Energy Tracker (kWh per dag)
# =========================
ENERGY_DAILY_FILE = "energy_daily.json"

class EnergyTracker:
    def __init__(self):
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.pv_wh = 0.0
        self.export_wh = 0.0      # naar net
        self.import_wh = 0.0      # van net
        self.house_wh = 0.0       # eigen verbruik
        self.batt_charge_wh = 0.0
        self.batt_discharge_wh = 0.0
        self.eddi_wh = 0.0
        self.zappi_wh = 0.0
        self.import_cost_eur = 0.0
        self.saved_cost_eur = 0.0
        self.last_ts = None
        self._load()

    def _load(self):
        try:
            with open(ENERGY_DAILY_FILE, "r") as f:
                data = json.load(f)
            today_data = data.get(self.today)
            if today_data:
                self.pv_wh = today_data.get("pv_wh", 0.0)
                self.export_wh = today_data.get("export_wh", 0.0)
                self.import_wh = today_data.get("import_wh", 0.0)
                self.house_wh = today_data.get("house_wh", 0.0)
                self.batt_charge_wh = today_data.get("batt_charge_wh", 0.0)
                self.batt_discharge_wh = today_data.get("batt_discharge_wh", 0.0)
                self.eddi_wh = today_data.get("eddi_wh", 0.0)
                self.zappi_wh = today_data.get("zappi_wh", 0.0)
                self.import_cost_eur = today_data.get("import_cost_eur", 0.0)
                self.saved_cost_eur = today_data.get("saved_cost_eur", 0.0)
                logging.info(f"📊 Energy tracker loaded for {self.today}: PV={self.pv_wh/1000:.1f}kWh")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            try:
                with open(ENERGY_DAILY_FILE, "r") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            data[self.today] = self._today_dict()
            # Keep max 90 days
            keys = sorted(data.keys())
            if len(keys) > 90:
                for k in keys[:-90]:
                    del data[k]
            with open(ENERGY_DAILY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logging.error(f"❌ Energy tracker save error: {e}")

    def _today_dict(self):
        return {
            "pv_wh": round(self.pv_wh, 1),
            "export_wh": round(self.export_wh, 1),
            "import_wh": round(self.import_wh, 1),
            "house_wh": round(self.house_wh, 1),
            "batt_charge_wh": round(self.batt_charge_wh, 1),
            "batt_discharge_wh": round(self.batt_discharge_wh, 1),
            "eddi_wh": round(self.eddi_wh, 1),
            "zappi_wh": round(self.zappi_wh, 1),
            "import_cost_eur": round(self.import_cost_eur, 4),
            "saved_cost_eur": round(self.saved_cost_eur, 4),
        }

    def tick(self, pv_w, grid_w, house_w, eddi_w, zappi_w, batt_w, frank_price_eur: Optional[float] = None):
        """Call every loop tick with current power values (watts).
        grid_w: negative = export, positive = import (myenergi raw convention)
        batt_w: positive = charging, negative = discharging
        frank_price_eur: huidig Frank uurtarief voor kostenberekening
        """
        now = time.time()
        # Check day rollover
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.today:
            self._save()  # save yesterday
            self.today = today
            self.pv_wh = 0.0
            self.export_wh = 0.0
            self.import_wh = 0.0
            self.house_wh = 0.0
            self.batt_charge_wh = 0.0
            self.batt_discharge_wh = 0.0
            self.eddi_wh = 0.0
            self.zappi_wh = 0.0
            self.import_cost_eur = 0.0
            self.saved_cost_eur = 0.0
            self.last_ts = now
            logging.info(f"📊 Energy tracker: new day {today}")
            return

        if self.last_ts is None:
            self.last_ts = now
            return

        dt_h = (now - self.last_ts) / 3600.0  # hours
        self.last_ts = now

        if dt_h > 0.05:  # skip if gap > 3 min (service restart etc)
            return

        # Accumulate Wh
        self.pv_wh += max(0, pv_w or 0) * dt_h
        if grid_w is not None:
            if grid_w < 0:  # export (myenergi: negative = export)
                self.export_wh += abs(grid_w) * dt_h
            else:
                self.import_wh += grid_w * dt_h
        self.house_wh += max(0, house_w or 0) * dt_h
        self.eddi_wh += max(0, eddi_w or 0) * dt_h
        self.zappi_wh += max(0, zappi_w or 0) * dt_h
        if batt_w is not None:
            if batt_w > 0:
                self.batt_charge_wh += batt_w * dt_h
            else:
                self.batt_discharge_wh += abs(batt_w) * dt_h

        if frank_price_eur is not None:
            try:
                price = float(frank_price_eur)
                import_wh = max(0, grid_w or 0) * dt_h if grid_w is not None else 0.0
                if import_wh > 0:
                    self.import_cost_eur += (import_wh / 1000.0) * price
                gen_wh = max(0, pv_w or 0) * dt_h
                if batt_w is not None and batt_w < 0:
                    gen_wh += abs(batt_w) * dt_h
                if gen_wh > 0:
                    self.saved_cost_eur += (gen_wh / 1000.0) * price
            except (TypeError, ValueError):
                pass

        # Save every ~60 ticks (~3 min)
        if int(now) % 180 < 4:
            self._save()

    def get_today(self):
        gen_kwh = round((self.pv_wh + self.batt_discharge_wh) / 1000, 2)
        imp_kwh = round(self.import_wh / 1000, 2)
        return {
            "date": self.today,
            "pv_kwh": round(self.pv_wh / 1000, 2),
            "export_kwh": round(self.export_wh / 1000, 2),
            "import_kwh": imp_kwh,
            "house_kwh": round(self.house_wh / 1000, 2),
            "batt_charge_kwh": round(self.batt_charge_wh / 1000, 2),
            "batt_discharge_kwh": round(self.batt_discharge_wh / 1000, 2),
            "eddi_kwh": round(self.eddi_wh / 1000, 2),
            "zappi_kwh": round(self.zappi_wh / 1000, 2),
            "import_cost_eur": round(self.import_cost_eur, 2),
            "saved_cost_eur": round(self.saved_cost_eur, 2),
            "cost_method": "frank_hourly",
            "self_consumption_pct": round(
                (((self.pv_wh + self.batt_discharge_wh) - self.export_wh) / (self.house_wh + self.eddi_wh + self.zappi_wh + self.batt_charge_wh) * 100)
                if (self.house_wh + self.eddi_wh + self.zappi_wh + self.batt_charge_wh) > 100 else 0, 1
            ),
        }

    def get_history(self, days=7):
        try:
            with open(ENERGY_DAILY_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        # Add today's live data
        data[self.today] = self._today_dict()
        # Return last N days
        keys = sorted(data.keys())[-days:]
        result = []
        for k in keys:
            d = data[k]
            pv = d.get("pv_wh", 0)
            exp = d.get("export_wh", 0)
            result.append({
                "date": k,
                "pv_kwh": round(pv / 1000, 2),
                "export_kwh": round(exp / 1000, 2),
                "import_kwh": round(d.get("import_wh", 0) / 1000, 2),
                "house_kwh": round(d.get("house_wh", 0) / 1000, 2),
                "eddi_kwh": round(d.get("eddi_wh", 0) / 1000, 2),
                "zappi_kwh": round(d.get("zappi_wh", 0) / 1000, 2),
                "batt_charge_kwh": round(d.get("batt_charge_wh", 0) / 1000, 2),
                "batt_discharge_kwh": round(d.get("batt_discharge_wh", 0) / 1000, 2),
                "self_consumption_pct": round(
                    (((pv + d.get("batt_discharge_wh", 0)) - exp) / (d.get("house_wh", 0) + d.get("eddi_wh", 0) + d.get("zappi_wh", 0) + d.get("batt_charge_wh", 0)) * 100)
                    if (d.get("house_wh", 0) + d.get("eddi_wh", 0) + d.get("zappi_wh", 0) + d.get("batt_charge_wh", 0)) > 100 else 0, 1
                ),
            })
        return result

energy_tracker = EnergyTracker()

# =========================
# Simple Battery Rule Engine (export-driven, manual setpoints)
# =========================
CHARGE_MODE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "simple_rule_charge_mode.json")


def _load_charge_mode() -> str:
    try:
        with open(CHARGE_MODE_PATH, encoding="utf-8") as f:
            mode = str(json.load(f).get("mode", "off"))
            return mode if mode in ("off", "price", "manual") else "off"
    except Exception:
        return "off"


def _save_charge_mode(mode: str) -> None:
    try:
        os.makedirs(os.path.dirname(CHARGE_MODE_PATH), exist_ok=True)
        with open(CHARGE_MODE_PATH, "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.debug(f"Charge mode save skip: {e}")


class SimpleRuleState:
    def __init__(self):
        self.enabled: bool = False
        self.task: Optional[asyncio.Task] = None
        # defaults (can be overridden via enable payload)
        self.cfg: Dict[str, Any] = {
            "buffer_w": 200,
            "export_margin_w": 50,
            "threshold_start_w": 150,
            "threshold_stop_w": 100,
            "ramp_step_w": 200,
            "loop_interval_s": 3.0,    # 3 seconds (was 5s)
            "cooldown_s": 8,
            "max_batt_total_w": 5000,  # total across all batteries
            "per_battery_max_w": 2500, # hard cap per battery
            "battery_config": self._load_battery_limits(),  # Load from battery_config.json
            "price_charge_enabled": False,
            "manual_charge_enabled": False,
            "anti_feed_enabled": False,
            "price_charge_total_w": 2500,
            "manual_charge_total_w": 2500,
            "price_charge_target_soc": 95.0,
            "price_hysteresis_s": 600,
            "manual_charge_hysteresis_s": 60,
            "battery_total_capacity_kwh": 10.0,
            "battery_charge_power_kw": 2.5,
            "weather_evening_start_hour": 15,
            "weather_horizon_hours": 4,
            "weather_confidence_min": 60,
            "weather_evening_target_soc": 95.0,
            "anti_feed_hysteresis_s": 90,
            "anti_feed_import_start_w": 250,
            "anti_feed_import_stop_w": 80,
            "discharge_ramp_step_w": 200,
            "discharge_setpoint_deadband_w": 250,
            "discharge_min_write_interval_s": 15.0,
            "price_charge_max_import_w": 100,
            # Frank: laag tarief ≤ €0.17 → laden/vasthouden; daarboven of morgen zon → ontladen
            "price_cheap_max_eur_kwh": 0.17,
            "tomorrow_sun_solar_min": 55.0,
            "tomorrow_sun_look_hours": 36,
            # Oude overschot-regel: stabiel export → batterij
            "stable_export_s": 30,
            "export_enough_w": 300,
        }
        _saved_mode = _load_charge_mode()
        self.cfg["price_charge_enabled"] = _saved_mode == "price"
        self.cfg["manual_charge_enabled"] = _saved_mode == "manual"
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
        self.battery_modes: Dict[str, str] = {}  # Track battery modes: bid -> "manual"|"anti-feed"|"unknown"
        self.price_charge_since: Optional[float] = None
        self.price_not_since: Optional[float] = None
        self.price_charge_active: bool = False
        self.manual_charge_since: Optional[float] = None
        self.manual_not_since: Optional[float] = None
        self.manual_charge_active: bool = False
        self.avg_soc_cache: Optional[float] = None  # laatste bekende gemiddelde SOC
        self.anti_feed_active: bool = False
        self.anti_feed_since: Optional[float] = None
        self.anti_feed_not_since: Optional[float] = None
        self.ema_import_w: float = 0.0
        self.last_discharge_total: float = 0.0
        self.surplus_since: Optional[float] = None  # stabiel overschot timer (oude regel)
        self.applied_control: Dict[str, Dict[str, Any]] = {}  # bid -> {action, power_w}
        self.rs485_force_active: Dict[str, bool] = {}  # bid -> RS485 force-control sessie actief
        self.unhealthy_batteries: Dict[str, float] = {}  # bid -> last_seen_unhealthy_ts
    
    def _load_battery_limits(self) -> dict:
        """Load minimum SOC limits from battery_config.json"""
        try:
            cfg = load_battery_config()
            limits = {}
            for bid in ["venus_ev2_92", "venus_ev2_74"]:
                if bid in cfg:
                    limits[bid] = {"minimum_soc_percent": cfg[bid].get("minimum_soc_percent", 35)}
                else:
                    limits[bid] = {"minimum_soc_percent": 35}
            logger.info(f"📋 Loaded battery SOC limits: {limits}")
            return limits
        except Exception as e:
            logger.warning(f"⚠️ Could not load battery config, using defaults: {e}")
            return {
                "venus_ev2_92": {"minimum_soc_percent": 35},
                "venus_ev2_74": {"minimum_soc_percent": 35},
            }
    
    def reload_battery_limits(self):
        """Reload battery limits from config file (call after config update)"""
        self.cfg["battery_config"] = self._load_battery_limits()
        logger.info(f"🔄 Battery limits reloaded: {self.cfg['battery_config']}")

simple_rule = SimpleRuleState()

def _extract_grid_from_raw(raw: Dict[str, Any]) -> Optional[int]:
    """Positief = import (zelfde als _extract_grid_import_w)."""
    return _extract_grid_import_w(raw)

def _extract_pv_from_raw(raw: Dict[str, Any]) -> Optional[int]:
    """Extract PV generation from raw data. Prefer 'gen' field."""
    try:
        blocks = raw.get("raw") or []
        # Try to get from eddi or zappi 'gen' field
        for b in blocks:
            if "eddi" in b and isinstance(b["eddi"], list):
                for e in b["eddi"]:
                    if e.get("gen") is not None:
                        return int(e.get("gen"))
            if "zappi" in b and isinstance(b["zappi"], list):
                for z in b["zappi"]:
                    if z.get("gen") is not None:
                        return int(z.get("gen"))
        # Fallback to top-level
        if isinstance(raw, dict) and raw.get("gen") is not None:
            return int(raw.get("gen"))
    except Exception:
        return None
    return None

async def _set_battery_power(bid: str, power_w: int) -> Dict[str, Any]:
    """Helper to send manual charge setpoint to a battery id; power_w=0 -> stop."""
    power_w = int(power_w or 0)
    action = "charge" if power_w > 0 else "stop"
    prev = simple_rule.applied_control.get(bid)
    if prev and prev.get("action") == action and int(prev.get("power_w") or 0) == power_w:
        return {"success": True, "ok": True, "skipped": True}
    try:
        if power_w > 0:
            payload = {"action": "charge", "power_w": power_w}
        else:
            payload = {"action": "stop"}
        result = await battery_control_by_id(bid, payload)  # type: ignore[arg-type]
        if result.get("success") or result.get("ok"):
            simple_rule.applied_control[bid] = {"action": action, "power_w": power_w}
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

async def _set_battery_discharge(bid: str, power_w: int) -> Dict[str, Any]:
    """Helper to send force-discharge setpoint; power_w=0 -> stop.

    Deadband + min-interval: voorkomt tikken door elke 3s opnieuw ENABLE/setpoint.
    """
    power_w = int(power_w or 0)
    action = "discharge" if power_w > 0 else "stop"
    prev = simple_rule.applied_control.get(bid) or {}
    prev_action = prev.get("action")
    prev_power = int(prev.get("power_w") or 0)
    deadband = int(simple_rule.cfg.get("discharge_setpoint_deadband_w", 250))
    min_interval = float(simple_rule.cfg.get("discharge_min_write_interval_s", 15.0))
    last_ts = float(prev.get("ts") or 0)
    now = time.time()

    if action == "stop":
        if prev_action in (None, "stop") and prev_power == 0:
            return {"success": True, "ok": True, "skipped": True}
    else:
        if prev_action == "discharge" and abs(prev_power - power_w) < deadband:
            return {"success": True, "ok": True, "skipped": True, "reason": "deadband"}
        if prev_action == "discharge" and (now - last_ts) < min_interval and abs(prev_power - power_w) < deadband * 2:
            return {"success": True, "ok": True, "skipped": True, "reason": "min_interval"}

    try:
        if power_w > 0:
            payload = {"action": "discharge", "power_w": power_w}
        else:
            payload = {"action": "stop"}
        result = await battery_control_by_id(bid, payload)  # type: ignore[arg-type]
        if result.get("success") or result.get("ok"):
            simple_rule.applied_control[bid] = {
                "action": action,
                "power_w": power_w,
                "ts": now,
            }
            simple_rule.rs485_force_active[bid] = action != "stop"
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def _battery_telemetry_ok(battery_data: Optional[Dict[str, Any]]) -> bool:
    """74 geeft soms garbage (7.6V, work_mode 65535) terwijl writes 'ok' zijn."""
    if not battery_data:
        return False
    try:
        v = float((battery_data.get("battery_voltage") or {}).get("value") or 0)
        wm = (battery_data.get("work_mode") or {}).get("value")
        if v < 40:  # Venus DC-bus is typisch ~400-550V
            return False
        if wm is not None and int(wm) >= 65000:
            return False
        return True
    except Exception:
        return False

async def _restore_battery_antifeed(bid: str) -> None:
    """Zet batterij terug naar Anti-Feed — standaard modus als we niet actief sturen."""
    if simple_rule.battery_modes.get(bid) == "anti-feed":
        return
    try:
        entry = _get_entry_for(bid)
        if not entry:
            return
        async with entry["lock"]:
            result = entry["client"].set_work_mode(1)
        if result.get("ok"):
            simple_rule.battery_modes[bid] = "anti-feed"
    except Exception as e:
        logger.debug(f"Anti-feed restore {bid}: {e}")

async def _restore_all_antifeed() -> None:
    try:
        for it in (await list_batteries())['items']:  # type: ignore[index]
            await _restore_battery_antifeed(it['id'])
    except Exception:
        pass

def _eddi_needs_heat_for_charge(temps: Dict[str, Optional[int]]) -> bool:
    """Eddi heeft voorrang op batterij-laden zolang tank(s) onder doel zitten.
    Tank2 wordt meegenomen als er een meting is (anders pakt Anti-Feed alles terwijl tank2 koud is).
    """
    needs = False
    if EDDI_USE_TANK_1 and temps.get("tank1") is not None:
        if int(temps["tank1"]) < EDDI_TARGET_TEMP_1:
            needs = True
    # Tank2: meenemen als er een geldige meting is (ook als USE_TANK_2 false is voor andere rules)
    if temps.get("tank2") is not None and int(temps["tank2"]) not in (-1, 127):
        if int(temps["tank2"]) < EDDI_TARGET_TEMP_2:
            needs = True
    return needs

async def _apply_grid_charge(
    *,
    mode: str,
    target_total: int,
    target_soc: int,
    price_info: Dict[str, Any],
    grid_w: int,
    pv_w: Optional[int],
    ema_overschot: float,
    cfg: Dict[str, Any],
    log_msg: str,
) -> None:
    """Laad batterijen uit net (Frank of handmatig). Caller doet continue."""
    if simple_rule.last_discharge_total > 0:
        try:
            for it in (await list_batteries())['items']:  # type: ignore[index]
                bid = it['id']
                if simple_rule.applied_control.get(bid, {}).get("action") == "discharge":
                    await _set_battery_discharge(bid, 0)
        except Exception:
            pass
        simple_rule.last_discharge_total = 0.0
    cap = int(cfg.get("per_battery_max_w", 2500))
    try:
        items = (await list_batteries())['items']  # type: ignore[index]
    except Exception as e:
        logger.error(f"❌ Failed to list batteries: {e}")
        return
    per: Dict[str, Any] = {}
    available: list = []
    blocked_bids = set()
    _soc_pc: list = []
    for it in items:
        bid = it['id']
        entry = _get_entry_for(bid)
        if not entry:
            per[bid] = {"mode": mode, "ok": False, "error": "not_in_registry", "set": 0}
            continue
        client = entry['client']
        try:
            battery_data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                timeout=5.0
            )
            current_soc = battery_data.get("soc_percent", {}).get("value", 0) if battery_data else 0
            if current_soc and 0 < current_soc <= 100:
                _soc_pc.append(float(current_soc))
            max_soc = cfg.get("battery_config", {}).get(bid, {}).get("maximum_soc_percent", 87)
            limit_soc = min(int(target_soc), int(max_soc))
            if current_soc >= limit_soc:
                per[bid] = {"set": 0, "ok": True, "mode": "blocked_target_soc", "soc": current_soc, "target_soc": limit_soc}
                blocked_bids.add(bid)
                continue
            available.append({"id": bid, "soc": current_soc, "entry": entry})
        except Exception as e:
            logger.warning(f"⚠️ Grid-charge SOC check {bid}: {e}")
            per[bid] = {"mode": "blocked", "ok": False, "error": str(e), "set": 0}

    if _soc_pc:
        simple_rule.avg_soc_cache = min(_soc_pc)

    n_avail = len(available)
    setpoints: Dict[str, int] = {}
    remaining = int(target_total) if n_avail else 0
    base_share = min(cap, remaining // n_avail) if n_avail else 0
    for bat in available:
        sp = max(0, min(base_share, remaining, cap))
        setpoints[bat["id"]] = sp
        remaining -= sp
    for bat in available:
        bid = bat["id"]
        sp = int(setpoints.get(bid, 0))
        setpoints[bid] = sp

    unchanged = all(
        simple_rule.applied_control.get(bid, {}).get("action") == ("charge" if setpoints.get(bid, 0) > 0 else "stop")
        and int(simple_rule.applied_control.get(bid, {}).get("power_w") or 0) == int(setpoints.get(bid, 0))
        for bid in setpoints
    ) and not blocked_bids
    if unchanged and setpoints:
        set_total = sum(int(v) for v in setpoints.values())
        for bat in available:
            bid = bat["id"]
            per[bid] = {"mode": mode, "ok": True, "set": setpoints[bid], "soc": bat["soc"], "skipped": True}
        simple_rule.prev_set_total = set_total
        simple_rule.last.update({
            "grid_w": grid_w,
            "pv_w": pv_w,
            "overschot_w": int(ema_overschot),
            "mode": mode,
            "price": price_info,
            "batt_target_total_w": int(target_total),
            "batt_set_total_w": int(set_total),
            "per_battery": per,
            "cooldown": False,
            "ts": time.time(),
            "error": None,
        })
        return

    logger.info(log_msg)
    set_total = 0
    for bat in available:
        bid = bat["id"]
        sp = int(setpoints.get(bid, 0))
        res = await _set_battery_power(bid, sp)
        ok = bool(res.get("success") or res.get("ok"))
        if ok:
            simple_rule.battery_modes[bid] = "charging" if sp > 0 else "stopped"
            set_total += sp
        per[bid] = {"mode": mode, "ok": ok, "set": sp, "soc": bat["soc"]}
    for bid in blocked_bids:
        await _set_battery_power(bid, 0)

    simple_rule.prev_set_total = set_total
    simple_rule.last.update({
        "grid_w": grid_w,
        "pv_w": pv_w,
        "overschot_w": int(ema_overschot),
        "mode": mode,
        "price": price_info,
        "batt_target_total_w": int(target_total),
        "batt_set_total_w": int(set_total),
        "per_battery": per,
        "cooldown": False,
        "ts": time.time(),
        "error": None,
    })


async def _hold_batteries_for_eddi() -> None:
    """Stop laden volledig (Manual + stop) zodat Eddi zon-overschot kan pakken.
    Niet terug naar Anti-Feed: die laadt zelf en steelt van Eddi.
    """
    try:
        for it in (await list_batteries())['items']:  # type: ignore[index]
            bid = it['id']
            entry = _get_entry_for(bid)
            if not entry:
                continue
            try:
                async with entry["lock"]:
                    entry["client"].set_work_mode(0)  # Manual
                    entry["client"].set_control("stop")
                simple_rule.battery_modes[bid] = "eddi_priority"
            except Exception as e:
                logger.warning(f"Eddi-priority hold {bid}: {e}")
    except Exception as e:
        logger.warning(f"Eddi-priority hold failed: {e}")


def _skip_surplus_for_grid_mode(
    charge_mode: str,
    *,
    has_real_surplus: bool,
    pv_w: Optional[int],
    pv_threshold: int,
    manual_charge_on: bool,
    price_charge_on: bool,
    schedule_active: bool,
    price_is_cheap: bool,
    schedule_needs_charge: bool,
    price_is_low: bool = False,
    tomorrow_sun_likely: bool = False,
    frank_should_discharge: bool = False,
) -> bool:
    """Overschot-/Anti-Feed-regel overslaan alleen als we echt moeten vasthouden/laden.

    - Uit → normale surplus
    - PV-overschot → altijd surplus (zon naar batterij)
    - Handmatig + nog laden → skip
    - Frank actief laden → skip
    - Frank + laag tarief + geen morgen-zon + nog laden → skip (vasthouden voor goedkoop laden)
    - Frank + duur tarief OF morgen-zon → NIET skippen (ontladen mag)
    """
    if charge_mode == "off":
        return False
    if has_real_surplus:
        return False
    if frank_should_discharge:
        return False
    if charge_mode == "manual" and schedule_needs_charge:
        return True
    if manual_charge_on or price_charge_on:
        return True
    if charge_mode == "price" and schedule_needs_charge and price_is_low and not tomorrow_sun_likely:
        return True
    return False


async def _simple_rule_loop():
    global simple_rule
    cfg = simple_rule.cfg
    alpha = 0.3  # light smoothing for overschot
    alpha_import = 0.25  # smoothing import voor anti-feed (voorkomt flipperen)
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
            pv_w = _extract_pv_from_raw(data) if data is not None else None
            eddi_w_raw = extract_eddi_power_w(data) if data is not None else 0
            zappi_w_raw = extract_zappi_power_w(data) if data is not None else 0
            house_w_raw = extract_house_consumption_w(data) if data is not None else 0
            frank_price_eur = None
            try:
                from frank_energie import frank_client as _frank_tick
                _fo_tick = await _frank_tick.get_overview()
                frank_price_eur = (_fo_tick.get("current") or {}).get("price_eur_kwh")
            except Exception:
                pass
            # Energy tracker tick (accumulate Wh + Frank €)
            energy_tracker.tick(
                pv_w or 0, grid_w, house_w_raw or 0, eddi_w_raw or 0, zappi_w_raw or 0, 0,
                frank_price_eur=frank_price_eur,
            )
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
                
                # SOC snel uitlezen voor laadplanning (lichtgewicht, enkel register 32104)
                try:
                    _soc_quick: list = []
                    for _bid, _reg in list(manager.registry.items()):
                        try:
                            _bd = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(None, _reg['client'].read_battery_data),
                                timeout=2.0
                            )
                            _sv = (_bd or {}).get("soc_percent", {}).get("value")
                            if _sv and 0 < float(_sv) <= 100:
                                _soc_quick.append(float(_sv))
                        except Exception:
                            pass
                    if _soc_quick:
                        # Gebruik de laagste SOC: als één batterij nog niet op doel zit, laden
                        simple_rule.avg_soc_cache = min(_soc_quick)
                except Exception:
                    pass

                # Doel-SOC voor netladen (vast 95%). Weer: morgen-zon → 's nachts toch ontladen.
                target_soc_for_plan = float(cfg.get("price_charge_target_soc", 95.0))
                weather_hint: Dict[str, Any] = {}
                tomorrow_sun_likely = False
                try:
                    now_hour = datetime.now().hour
                    # Avond/nacht: kijk of morgen veel zon komt
                    if now_hour >= int(cfg.get("weather_evening_start_hour", 15)) or now_hour < 8:
                        weather_hint = await weather_service.get_tomorrow_sun_likely(
                            solar_min=float(cfg.get("tomorrow_sun_solar_min", 55.0)),
                            look_hours=int(cfg.get("tomorrow_sun_look_hours", 36)),
                        )
                        tomorrow_sun_likely = bool(weather_hint.get("tomorrow_sun_likely"))
                    else:
                        # Overdag: korte horizon (geen zin om te ontladen voor "morgen" midden op de dag)
                        weather_hint = await weather_service.get_no_sun_likely(
                            horizon_hours=int(cfg.get("weather_horizon_hours", 4))
                        )
                except Exception as we:
                    logger.debug(f"Weather hint skipped: {we}")

                # Dagelijkse tariefband + laadplanning (SOC uit vorige iteratie of cache)
                price_plan: Dict[str, Any] = {}
                overview: Dict[str, Any] = {}
                _prev_soc = simple_rule.avg_soc_cache
                try:
                    from frank_energie import frank_client as _frank
                    overview = await _frank.get_overview(
                        soc_pct=float(_prev_soc) if _prev_soc is not None else None,
                        battery_capacity_kwh=float(cfg.get("battery_total_capacity_kwh", 10.0)),
                        charge_power_kw=float(cfg.get("battery_charge_power_kw", 2.5)),
                        target_soc_pct=target_soc_for_plan,
                    )
                    price_plan = overview.get("plan") or {}
                except Exception as pe:
                    logger.debug(f"Frank price plan skip: {pe}")
                price_band = price_plan.get("band") or "unknown"
                price_now = None
                try:
                    price_now = (overview.get("current") or {}).get("price_eur_kwh")
                except Exception:
                    price_now = None
                price_target_soc = int(price_plan.get("charge_target_soc") or target_soc_for_plan)
                house_now = int(house_w_raw or 0)
                eddi_now = int(eddi_w_raw or 0)
                zappi_now = int(zappi_w_raw or 0)
                # Echt overschot = gemeten export naar het net (niet PV minus huis/Eddi-reservering)
                # grid_w in simple rule: negatief = export, positief = import (CT clamp)
                export_now = max(0, -int(grid_w)) if int(grid_w) < 0 else 0
                import_now = max(0, int(grid_w)) if int(grid_w) > 0 else 0
                export_enough_w = int(cfg.get("export_enough_w", 300))
                has_real_surplus = export_now >= export_enough_w or ema_overschot >= export_enough_w
                low_after_priority = export_now < 100
                pv_threshold = cfg.get("pv_threshold_w", 50)
                af_start_w = int(cfg.get("anti_feed_import_start_w", 250))
                af_stop_w = int(cfg.get("anti_feed_import_stop_w", 80))
                af_hyst_s = float(cfg.get("anti_feed_hysteresis_s", 90))
                # EMA op import: voorkomt dat ontladen import onder drempel duwt → stop → import stijgt → herhaal
                simple_rule.ema_import_w = (
                    alpha_import * float(import_now)
                    + (1.0 - alpha_import) * float(simple_rule.ema_import_w or import_now)
                )
                import_smooth = int(simple_rule.ema_import_w)
                pv_low = pv_w is not None and int(pv_w) < pv_threshold
                if simple_rule.anti_feed_active:
                    af_candidate = pv_low and import_smooth > af_stop_w
                else:
                    af_candidate = pv_low and import_smooth > af_start_w
                if af_candidate:
                    simple_rule.anti_feed_not_since = None
                    if not simple_rule.anti_feed_since:
                        simple_rule.anti_feed_since = now
                    if (not simple_rule.anti_feed_active) and (now - simple_rule.anti_feed_since) >= af_hyst_s:
                        simple_rule.anti_feed_active = True
                else:
                    simple_rule.anti_feed_since = None
                    if not simple_rule.anti_feed_not_since:
                        simple_rule.anti_feed_not_since = now
                    if simple_rule.anti_feed_active and (now - simple_rule.anti_feed_not_since) >= af_hyst_s:
                        simple_rule.anti_feed_active = False
                anti_feed_needed = bool(simple_rule.anti_feed_active) and cfg.get("anti_feed_enabled", False)
                hyst_s = float(cfg.get("price_hysteresis_s", 600))
                price_charge_on = False
                manual_charge_on = False
                charge_schedule = (price_plan.get("charge_schedule") or {})
                schedule_active = bool(charge_schedule.get("active_now", False))
                cheap_thr = price_plan.get("cheap_threshold_eur_kwh")
                price_is_cheap = price_band == "cheap" or (
                    price_now is not None
                    and cheap_thr is not None
                    and float(price_now) <= float(cheap_thr)
                )
                # Vaste Frank-drempel: ≤ €0.17 = laag (laden/vasthouden), daarboven = ontladen
                price_cheap_max = float(cfg.get("price_cheap_max_eur_kwh", 0.17))
                price_is_low = (
                    price_now is not None and float(price_now) <= price_cheap_max
                ) or (price_now is None and price_is_cheap)
                schedule_needs_charge = charge_schedule.get("reason") not in ("already_at_target", "soc_unknown", None) \
                    and float(charge_schedule.get("needed_kwh") or 0) > 0.05
                needed_hours = float(charge_schedule.get("needed_hours") or 0)

                if cfg.get("manual_charge_enabled"):
                    charge_mode = "manual"
                elif cfg.get("price_charge_enabled"):
                    charge_mode = "price"
                else:
                    charge_mode = "off"

                # Frank: ontladen bij duur tarief OF morgen veel zon — niet wachten op anti_feed_enabled
                pv_low_now = pv_w is not None and int(pv_w) < int(cfg.get("pv_threshold_w", 50))
                frank_should_discharge = (
                    charge_mode == "price"
                    and not has_real_surplus
                    and (not price_is_low or tomorrow_sun_likely)
                    and pv_low_now
                    and import_smooth > int(cfg.get("anti_feed_import_stop_w", 80))
                )
                # Legacy force-discharge pad alleen als anti_feed_enabled aan staat
                if frank_should_discharge:
                    anti_feed_needed = False  # Frank gebruikt rustige Anti-Feed work-mode, geen force-spam

                manual_charge_on = False
                price_charge_on = False
                if has_real_surplus and charge_mode != "manual":
                    simple_rule.price_charge_since = None
                    simple_rule.price_not_since = None
                    simple_rule.price_charge_active = False
                    simple_rule.manual_charge_active = False
                    simple_rule.manual_charge_since = None
                    simple_rule.manual_not_since = None
                elif charge_mode == "manual":
                    manual_hyst_s = float(cfg.get("manual_charge_hysteresis_s", 60))
                    manual_ok = (
                        schedule_needs_charge
                        and not anti_feed_needed
                    )
                    if manual_ok:
                        simple_rule.manual_not_since = None
                        if not simple_rule.manual_charge_since:
                            simple_rule.manual_charge_since = now
                        if (not simple_rule.manual_charge_active) and (now - simple_rule.manual_charge_since) >= 5.0:
                            simple_rule.manual_charge_active = True
                    else:
                        simple_rule.manual_charge_since = None
                        if not simple_rule.manual_not_since:
                            simple_rule.manual_not_since = now
                        if simple_rule.manual_charge_active and (now - simple_rule.manual_not_since) >= manual_hyst_s:
                            simple_rule.manual_charge_active = False
                    manual_charge_on = bool(simple_rule.manual_charge_active)
                elif charge_mode == "price":
                    # Geen netladen als morgen zon komt — wacht op gratis PV
                    candidate = (
                        schedule_active
                        and schedule_needs_charge
                        and price_is_cheap
                        and price_is_low
                        and not tomorrow_sun_likely
                        and low_after_priority
                        and not anti_feed_needed
                    )
                    hard_stop = (
                        not schedule_needs_charge
                        or anti_feed_needed
                        or has_real_surplus
                        or tomorrow_sun_likely
                        or not price_is_low
                    )
                    if hard_stop:
                        simple_rule.price_charge_since = None
                        simple_rule.price_not_since = None
                        simple_rule.price_charge_active = False
                    elif candidate:
                        simple_rule.price_not_since = None
                        if not simple_rule.price_charge_since:
                            simple_rule.price_charge_since = now
                        if (not simple_rule.price_charge_active) and (now - simple_rule.price_charge_since) >= hyst_s:
                            simple_rule.price_charge_active = True
                    else:
                        simple_rule.price_charge_since = None
                        if not simple_rule.price_not_since:
                            simple_rule.price_not_since = now
                        if simple_rule.price_charge_active and (now - simple_rule.price_not_since) >= hyst_s:
                            simple_rule.price_charge_active = False
                    price_charge_on = bool(simple_rule.price_charge_active)

                price_info = {
                    "band": price_band,
                    "price_eur_kwh": price_now,
                    "cheap_threshold": price_plan.get("cheap_threshold_eur_kwh"),
                    "expensive_threshold": price_plan.get("expensive_threshold_eur_kwh"),
                    "price_cheap_max_eur_kwh": price_cheap_max,
                    "price_is_low": price_is_low,
                    "tomorrow_sun_likely": tomorrow_sun_likely,
                    "frank_should_discharge": frank_should_discharge,
                    "target_soc": price_target_soc,
                    "charging": price_charge_on or manual_charge_on,
                    "charge_mode": charge_mode,
                    "manual_charge_active": manual_charge_on,
                    "pv_surplus_w": export_now if has_real_surplus else 0,
                    "needed_hours": round(needed_hours, 2),
                    "pv_after_priority_w": export_now,
                    "schedule_active": schedule_active,
                    "charge_schedule": charge_schedule,
                    "weather_hint": weather_hint,
                }

                if anti_feed_needed and (price_charge_on or manual_charge_on):
                    price_charge_on = False
                    manual_charge_on = False
                    simple_rule.price_charge_active = False
                    simple_rule.manual_charge_active = False
                    simple_rule.price_charge_since = None
                    simple_rule.price_not_since = None

                if manual_charge_on:
                    target_total = int(cfg.get("manual_charge_total_w", 2500))
                    await _apply_grid_charge(
                        mode="manual_charge",
                        target_total=target_total,
                        target_soc=price_target_soc,
                        price_info=price_info,
                        grid_w=grid_w,
                        pv_w=pv_w,
                        ema_overschot=ema_overschot,
                        cfg=cfg,
                        log_msg=(
                            f"🔧 MANUAL CHARGE: soc→{price_target_soc}% "
                            f"need={charge_schedule.get('needed_kwh')}kWh → charge {target_total}W"
                        ),
                    )
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue

                if price_charge_on:
                    target_total = int(cfg.get("price_charge_total_w", 2500))
                    sched_slots = [f"{s['day']}@{s['hour']}h" for s in charge_schedule.get("planned_slots", [])]
                    await _apply_grid_charge(
                        mode="price_charge",
                        target_total=target_total,
                        target_soc=price_target_soc,
                        price_info=price_info,
                        grid_w=grid_w,
                        pv_w=pv_w,
                        ema_overschot=ema_overschot,
                        cfg=cfg,
                        log_msg=(
                            f"💶 PRICE CHARGE: schedule={sched_slots} price={price_now} "
                            f"soc={charge_schedule.get('soc_now')}%→{price_target_soc}% "
                            f"need={charge_schedule.get('needed_kwh')}kWh "
                            f"→ charge {target_total}W"
                        ),
                    )
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue

                # Frank/handmatig: overschot-regel alleen overslaan wanneer nodig (zie _skip_surplus_for_grid_mode).
                skip_surplus = _skip_surplus_for_grid_mode(
                    charge_mode,
                    has_real_surplus=has_real_surplus,
                    pv_w=pv_w,
                    pv_threshold=int(cfg.get("pv_threshold_w", 50)),
                    manual_charge_on=manual_charge_on,
                    price_charge_on=price_charge_on,
                    schedule_active=schedule_active,
                    price_is_cheap=price_is_cheap,
                    schedule_needs_charge=schedule_needs_charge,
                    price_is_low=price_is_low,
                    tomorrow_sun_likely=tomorrow_sun_likely,
                    frank_should_discharge=frank_should_discharge,
                )
                if skip_surplus:
                    # Frank/handmatig wachtfase: surplus overslaan, batterijen niet stoppen/ontladen
                    simple_rule.last.update({
                        "grid_w": grid_w,
                        "pv_w": pv_w,
                        "overschot_w": int(ema_overschot),
                        "mode": f"{charge_mode}_wait",
                        "price": price_info,
                        "batt_target_total_w": 0,
                        "batt_set_total_w": 0,
                        "per_battery": {},
                        "cooldown": False,
                        "ts": time.time(),
                        "error": None,
                    })
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue

                # Frank-ontladen: Anti-Feed work-mode (1x zetten) i.p.v. elke 3s force-discharge (tikken).
                if frank_should_discharge:
                    # Stop force-discharge sessie als die nog open stond
                    if simple_rule.last_discharge_total > 0 or any(
                        (simple_rule.applied_control.get(b) or {}).get("action") == "discharge"
                        for b in ("venus_ev2_92", "venus_ev2_74")
                    ):
                        try:
                            for it in (await list_batteries())['items']:  # type: ignore[index]
                                await _set_battery_discharge(it['id'], 0)
                        except Exception:
                            pass
                        simple_rule.last_discharge_total = 0.0
                    await _restore_all_antifeed()
                    # Probeer 74 opnieuw als telemetry kapot lijkt (WiFi-bridge)
                    per_af: Dict[str, Any] = {}
                    try:
                        for it in (await list_batteries())['items']:  # type: ignore[index]
                            bid = it['id']
                            entry = _get_entry_for(bid)
                            if not entry:
                                continue
                            client = entry['client']
                            try:
                                bd = await asyncio.wait_for(
                                    asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                                    timeout=5.0,
                                )
                                ok_tel = _battery_telemetry_ok(bd)
                                soc = (bd or {}).get("soc_percent", {}).get("value") if bd else None
                                if not ok_tel:
                                    last_uh = float(simple_rule.unhealthy_batteries.get(bid) or 0)
                                    simple_rule.unhealthy_batteries[bid] = time.time()
                                    # Reconnect max 1x / 60s — voorkomt WiFi-spam
                                    if (time.time() - last_uh) > 60.0 or last_uh == 0:
                                        try:
                                            client.disconnect()
                                        except Exception:
                                            pass
                                        await asyncio.sleep(0.3)
                                        try:
                                            client.connect()
                                        except Exception:
                                            pass
                                        async with entry['lock']:
                                            client.set_work_mode(1)
                                        logger.warning(
                                            f"⚠️ Battery {bid} slechte telemetry (V/work_mode) — reconnect + Anti-Feed retry"
                                        )
                                    per_af[bid] = {
                                        "mode": "anti-feed_retry",
                                        "ok": False,
                                        "error": "bad_telemetry",
                                        "soc": soc,
                                        "set": 0,
                                    }
                                else:
                                    simple_rule.unhealthy_batteries.pop(bid, None)
                                    pw = (bd or {}).get("battery_power", {}).get("value")
                                    per_af[bid] = {
                                        "mode": "anti-feed",
                                        "ok": True,
                                        "soc": soc,
                                        "set": int(abs(pw or 0)) if (pw or 0) < 0 else 0,
                                        "power_w": pw,
                                    }
                            except Exception as e:
                                per_af[bid] = {"mode": "error", "ok": False, "error": str(e), "set": 0}
                    except Exception as e:
                        logger.warning(f"Frank anti-feed status read failed: {e}")

                    simple_rule.last.update({
                        "grid_w": grid_w,
                        "pv_w": pv_w,
                        "overschot_w": 0,
                        "mode": "frank_antifeed",
                        "price": price_info,
                        "target_export_w": target_export,
                        "batt_target_total_w": 0,
                        "batt_set_total_w": sum(int(p.get("set") or 0) for p in per_af.values()),
                        "per_battery": per_af,
                        "cooldown": False,
                        "ts": time.time(),
                        "error": None,
                    })
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue

                # Anti-feed: zon weg + import → batterijen ontladen (force discharge, alleen als enabled).
                if anti_feed_needed:
                    # Eerst eventuele actieve laad-sessie stoppen — huis heeft voorrang
                    try:
                        for it in (await list_batteries())['items']:  # type: ignore[index]
                            await _set_battery_power(it['id'], 0)
                    except Exception:
                        pass
                    simple_rule.prev_set_total = 0.0
                    # Force discharge (Modbus control) — Anti-Feed work_mode alleen is na FW-update niet genoeg
                    import_margin = int(cfg.get("import_margin_w", cfg.get("buffer_w", 200)))
                    cap = int(cfg.get("per_battery_max_w", 2500))
                    raw_target = max(0, min(int(cfg.get("max_batt_total_w", 5000)), import_smooth - import_margin))
                    # Afronden op 250W → minder setpoint-writes
                    step_round = max(100, int(cfg.get("discharge_setpoint_deadband_w", 250)))
                    raw_target = int(round(raw_target / step_round) * step_round)
                    ramp = int(cfg.get("discharge_ramp_step_w", 200))
                    prev_d = float(simple_rule.last_discharge_total or 0)
                    if raw_target > prev_d:
                        target_total = int(min(raw_target, prev_d + ramp))
                    else:
                        target_total = int(max(raw_target, prev_d - ramp))
                    target_total = int(round(target_total / step_round) * step_round)
                    simple_rule.last_discharge_total = float(target_total)
                    logger.info(
                        f"☀️ SIMPLE RULE: anti-feed PV={pv_w}W import={import_now}W smooth={import_smooth}W "
                        f"→ discharge target={target_total}W"
                    )
                    try:
                        items = (await list_batteries())['items']  # type: ignore[index]
                    except Exception as e:
                        logger.error(f"❌ Failed to list batteries: {e}")
                        continue

                    # Eerst SOC checken: welke batterijen mogen ontladen?
                    per: Dict[str, Any] = {}
                    available: list = []
                    for it in items:
                        bid = it['id']
                        entry = _get_entry_for(bid)
                        if not entry:
                            per[bid] = {"mode": "discharge", "ok": False, "error": "not_in_registry", "set": 0}
                            continue
                        client = entry['client']
                        lock = entry['lock']
                        try:
                            battery_data = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                                timeout=5.0
                            )
                            if not _battery_telemetry_ok(battery_data):
                                simple_rule.unhealthy_batteries[bid] = time.time()
                                per[bid] = {
                                    "mode": "blocked_bad_telemetry",
                                    "ok": False,
                                    "error": "bad_telemetry",
                                    "set": 0,
                                    "soc": (battery_data or {}).get("soc_percent", {}).get("value") if battery_data else None,
                                }
                                logger.warning(f"⚠️ Battery {bid} overgeslagen (slechte Modbus-telemetry)")
                                continue
                            simple_rule.unhealthy_batteries.pop(bid, None)
                            current_soc = battery_data.get("soc_percent", {}).get("value", 100) if battery_data else 100
                            min_soc = cfg.get("battery_config", {}).get(bid, {}).get("minimum_soc_percent", 15)
                            if current_soc <= min_soc:
                                logger.warning(
                                    f"🔋 Battery {bid} SOC te laag ({current_soc}% <= {min_soc}%) - STOP ontladen"
                                )
                                async with lock:
                                    stop_result = client.set_control("stop")
                                simple_rule.battery_modes[bid] = "stopped_min_soc"
                                per[bid] = {
                                    "mode": "blocked_min_soc",
                                    "ok": True,
                                    "soc": current_soc,
                                    "min_soc": min_soc,
                                    "set": 0,
                                    "stop_ok": bool(stop_result.get("ok")),
                                }
                                continue
                            available.append({"id": bid, "soc": current_soc, "entry": entry})
                        except Exception as e:
                            error_type = (
                                "timeout" if "timeout" in str(e).lower()
                                else "connection" if any(x in str(e) for x in ["Broken pipe", "Connection", "Bad file descriptor"])
                                else "unknown"
                            )
                            logger.warning(f"⚠️ Modbus {error_type} voor {bid} - geen discharge deze ronde: {e}")
                            # Veiligheid: probeer te stoppen als we dachten te ontladen
                            if simple_rule.battery_modes.get(bid) in ("anti-feed", "discharging"):
                                try:
                                    async with lock:
                                        client.set_control("stop")
                                    simple_rule.battery_modes[bid] = "manual"
                                except Exception:
                                    pass
                            per[bid] = {"mode": "blocked", "ok": False, "error": f"modbus_{error_type}", "set": 0, "will_retry": True}

                    # Verdeel discharge over beschikbare batterijen
                    n_avail = len(available)
                    setpoints: Dict[str, int] = {}
                    if n_avail > 0 and target_total > 0:
                        remaining = int(target_total)
                        base_share = min(cap, remaining // n_avail)
                        for bat in available:
                            sp = max(0, min(base_share, remaining, cap))
                            setpoints[bat["id"]] = sp
                            remaining -= sp
                        # Rest verdelen
                        idx = 0
                        while remaining > 0 and idx < n_avail * 2:
                            bid = available[idx % n_avail]["id"]
                            space = max(0, cap - setpoints.get(bid, 0))
                            if space > 0:
                                give = min(space, remaining)
                                setpoints[bid] = setpoints.get(bid, 0) + give
                                remaining -= give
                            idx += 1
                    else:
                        for bat in available:
                            setpoints[bat["id"]] = 0

                    set_total = 0
                    for bat in available:
                        bid = bat["id"]
                        sp = int(setpoints.get(bid, 0))
                        try:
                            res = await _set_battery_discharge(bid, sp)
                            ok = bool(res.get("success") or res.get("ok"))
                            if ok:
                                simple_rule.battery_modes[bid] = "discharging" if sp > 0 else "stopped"
                            per[bid] = {
                                "mode": "discharge" if sp > 0 else "idle",
                                "ok": ok,
                                "set": sp,
                                "soc": bat["soc"],
                            }
                            if ok:
                                set_total += sp
                            logger.info(f"⚡ Battery {bid} force discharge {sp}W → ok={ok}")
                        except Exception as e:
                            logger.warning(f"⚠️ Battery {bid} discharge error: {e}")
                            per[bid] = {"mode": "error", "ok": False, "error": str(e), "set": 0, "will_retry": True}

                    try:
                        h = simple_rule.last.get("health", {})
                        h.update({
                            "myenergi_ok": True,
                            "myenergi_fail_count": 0,
                            "last_myenergi_ok_ts": time.time(),
                            "simple_rule_ok": True,
                            "simple_rule_fail_count": 0,
                            "last_simple_rule_ok_ts": time.time()
                        })
                        simple_rule.last["health"] = h
                    except Exception:
                        pass
                    
                    simple_rule.last.update({
                        "grid_w": grid_w,
                        "pv_w": pv_w,
                        "overschot_w": 0,
                        "mode": "anti-feed",
                        "price": price_info,
                        "target_export_w": target_export,
                        "batt_target_total_w": int(target_total),
                        "batt_set_total_w": int(set_total),
                        "per_battery": per,
                        "cooldown": False,
                        "ts": time.time(),
                        "error": None,
                    })
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue

                # Anti-feed uit: expliciet stoppen met ontladen (niet alleen charge=0)
                if simple_rule.last_discharge_total > 0:
                    try:
                        for it in (await list_batteries())['items']:  # type: ignore[index]
                            await _set_battery_discharge(it['id'], 0)
                    except Exception:
                        pass
                    simple_rule.last_discharge_total = 0.0

                # ===== OUDE OVERSCHOT-REGEL =====
                # 1) Geen PV → Anti-Feed (avond ontladen)
                # 2) PV + Eddi nog niet klaar + Eddi nog niet bezig → batterij stil (Eddi eerst)
                #    (ook na korte zon-dip: als zon terugkomt krijgt Eddi opnieuw voorrang)
                # 3) PV + (Eddi klaar OF Eddi al bezig) + stabiel overschot ~30s → rest naar batterij
                eddi_temps = extract_eddi_temperatures(data or {})
                eddi_needs = _eddi_needs_heat_for_charge(eddi_temps)
                eddi_busy = eddi_now > EDDI_ACTIVE_W
                pv_ok = pv_w is not None and int(pv_w) >= pv_threshold
                stable_s = float(cfg.get("stable_export_s", 30))
                export_enough = int(cfg.get("export_enough_w", 300))
                target_export = int(cfg["buffer_w"] + cfg["export_margin_w"])  # ~250W vroeger
                error = ema_overschot - target_export

                if not pv_ok:
                    # Geen zon: niet laden, Anti-Feed voor ontladen
                    simple_rule.surplus_since = None
                    target_total = 0
                    await _restore_all_antifeed()
                    if grid_w >= 0:
                        simple_rule.cooldown_until = now + cfg["cooldown_s"]
                elif eddi_needs and not eddi_busy:
                    # Zon terug / Eddi nog niet aan → batterijen omdraaien/stoppen, Eddi eerst
                    simple_rule.surplus_since = None
                    target_total = 0
                    await _hold_batteries_for_eddi()
                    logger.info(
                        f"🔥 EDDI PRIORITY: PV={pv_w}W tanks={eddi_temps} eddi={eddi_now}W "
                        f"→ batterij wijkt (zoals vroeger)"
                    )
                    simple_rule.prev_set_total = 0
                    simple_rule.last.update({
                        "grid_w": grid_w,
                        "pv_w": pv_w,
                        "overschot_w": int(ema_overschot),
                        "mode": "eddi_priority",
                        "price": price_info,
                        "target_export_w": target_export,
                        "batt_target_total_w": 0,
                        "batt_set_total_w": 0,
                        "per_battery": {},
                        "eddi_temperatures": eddi_temps,
                        "cooldown": False,
                        "ts": time.time(),
                        "error": None,
                    })
                    dt = time.time() - t0
                    await asyncio.sleep(max(0.05, cfg["loop_interval_s"] - dt))
                    continue
                else:
                    # Eddi klaar OF Eddi al bezig → rest-overschot mag naar batterij
                    # Stabiel overschot: pas laden na ~30s boven drempel (oude EXPORT_ENOUGH gedrag)
                    if ema_overschot >= export_enough:
                        if not simple_rule.surplus_since:
                            simple_rule.surplus_since = now
                    else:
                        simple_rule.surplus_since = None

                    surplus_stable = (
                        simple_rule.surplus_since is not None
                        and (now - simple_rule.surplus_since) >= stable_s
                    )

                    if grid_w >= 0:
                        target_total = 0
                        simple_rule.surplus_since = None
                        simple_rule.cooldown_until = now + cfg["cooldown_s"]
                    elif surplus_stable and error > cfg["threshold_start_w"] and not in_cooldown:
                        jump = int(error * 0.7)
                        step = max(cfg["ramp_step_w"], jump)
                        target_total = min(cfg["max_batt_total_w"], simple_rule.prev_set_total + step)
                        logger.info(
                            f"☀️ SURPLUS: overschot={int(ema_overschot)}W stabiel≥{stable_s:.0f}s "
                            f"eddi={eddi_now}W → batterij target={target_total}W"
                        )
                    elif error < -cfg["threshold_stop_w"]:
                        step = max(cfg["ramp_step_w"], int(abs(error) * 0.7))
                        target_total = max(0, simple_rule.prev_set_total - step)
                        if target_total == 0:
                            simple_rule.cooldown_until = now + cfg["cooldown_s"]
                    # else hold current target

                # distribute across batteries - first check which ones are available
                per: Dict[str, Any] = {}
                items = (await list_batteries())['items']  # type: ignore[index]
                cap = int(simple_rule.cfg.get("per_battery_max_w", 2500))
                blocked_bids = set()
                
                # Pre-check: which batteries are blocked (max SOC)?
                _soc_readings_this_iter: list = []
                for it in items:
                    bid = it['id']
                    try:
                        entry = _get_entry_for(bid)
                        if entry:
                            client = entry['client']
                            battery_data = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                                timeout=3.0
                            )
                            current_soc = battery_data.get("soc_percent", {}).get("value", 0) if battery_data else 0
                            if current_soc and 0 < current_soc <= 100:
                                _soc_readings_this_iter.append(float(current_soc))
                            max_soc = cfg.get("battery_config", {}).get(bid, {}).get("maximum_soc_percent", 87)
                            if current_soc >= max_soc:
                                logger.info(f"🔋 Battery {bid} at max SOC ({current_soc}% >= {max_soc}%) - SKIP")
                                per[bid] = {"set": 0, "ok": True, "mode": "blocked_max_soc", "soc": current_soc, "max_soc": max_soc}
                                blocked_bids.add(bid)
                    except Exception as e:
                        logger.warning(f"⚠️ Could not check SOC for {bid}: {e}")
                
                # Update SOC cache voor volgende Frank-aanroep (min = meest conservatief)
                if _soc_readings_this_iter:
                    simple_rule.avg_soc_cache = min(_soc_readings_this_iter)

                # Only distribute to available (non-blocked) batteries
                available = [it for it in items if it['id'] not in blocked_bids]
                n_avail = max(1, len(available))
                setpoints: Dict[str, int] = {}
                remaining = int(target_total)
                base_share = int(target_total / n_avail) if n_avail > 0 else 0
                base_share = min(base_share, cap)
                for it in available:
                    bid = it['id']
                    sp = max(0, min(base_share, remaining))
                    setpoints[bid] = sp
                    remaining -= sp
                # Redistribute leftover
                if remaining > 0 and available:
                    idx = 0
                    L = len(available)
                    while remaining > 0 and idx < L * 2:
                        bid = available[idx % L]['id']
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
                    if bid in blocked_bids:
                        continue
                    sp = int(setpoints.get(bid, 0))
                    
                    res = await _set_battery_power(bid, sp)
                    per[bid] = {"set": sp, "ok": bool(res.get("success")), "mode": "charging" if sp > 0 else "idle"}
                    set_total += sp
                    if sp > 0:
                        simple_rule.battery_modes[bid] = "charging"

                if set_total == 0 and not pv_ok:
                    await _restore_all_antifeed()
                elif set_total == 0 and not eddi_needs:
                    await _restore_all_antifeed()

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
                    "pv_w": pv_w,
                    "overschot_w": int(ema_overschot),
                    "mode": "surplus_charge" if int(set_total) > 0 else "idle",
                    "price": price_info,
                    "target_export_w": target_export,
                    "eddi_temperatures": eddi_temps,
                    "surplus_stable_s": (
                        round(now - simple_rule.surplus_since, 1) if simple_rule.surplus_since else 0
                    ),
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
    # stop batteries safely, terug naar Anti-Feed (oude stand)
    try:
        items = (await list_batteries())['items']  # type: ignore[index]
        for it in items:
            await _set_battery_power(it['id'], 0)
            await _set_battery_discharge(it['id'], 0)
            await _restore_battery_antifeed(it['id'])
        simple_rule.applied_control.clear()
    except Exception:
        pass
    return {"success": True, "status": "disabled"}

@app.get("/api/simple_rule/status")
async def simple_rule_status():
    cfg = simple_rule.cfg
    charge_mode = "off"
    if cfg.get("manual_charge_enabled"):
        charge_mode = "manual"
    elif cfg.get("price_charge_enabled"):
        charge_mode = "price"
    return {
        "success": True,
        "enabled": simple_rule.enabled,
        "charge_mode": charge_mode,
        "last": simple_rule.last,
        "cfg": simple_rule.cfg,
    }


@app.post("/api/simple_rule/charge_mode")
async def simple_rule_charge_mode(payload: Dict[str, Any] = Body(default={})):  # type: ignore[assignment]
    """Stel laadmodus in: off | price | manual. Frank en handmatig kunnen niet tegelijk."""
    mode = str((payload or {}).get("mode") or "off").lower().strip()
    if mode not in ("off", "price", "manual"):
        raise HTTPException(status_code=400, detail="mode moet off, price of manual zijn")
    simple_rule.cfg["price_charge_enabled"] = mode == "price"
    simple_rule.cfg["manual_charge_enabled"] = mode == "manual"
    simple_rule.price_charge_active = False
    simple_rule.manual_charge_active = False
    simple_rule.manual_charge_since = None
    simple_rule.manual_not_since = None
    simple_rule.price_charge_since = None
    simple_rule.price_not_since = None
    _save_charge_mode(mode)
    simple_rule.applied_control.clear()
    labels = {"off": "uit", "price": "Frank netladen", "manual": "handmatig laden"}
    logger.info(f"🔀 Charge mode → {labels.get(mode, mode)}")
    return {
        "success": True,
        "charge_mode": mode,
        "cfg": {
            "price_charge_enabled": simple_rule.cfg["price_charge_enabled"],
            "manual_charge_enabled": simple_rule.cfg["manual_charge_enabled"],
            "price_charge_target_soc": simple_rule.cfg.get("price_charge_target_soc"),
        },
    }

# ---------------------------------
# SOC Safety Monitor (Always running!)
# ---------------------------------
soc_safety_task = None

async def _soc_safety_monitor():
    """Independent SOC safety monitor - runs ALWAYS (even when Simple Rule is disabled).
    Prevents battery discharge below minimum SOC regardless of manual settings."""
    global simple_rule
    logger.info("🛡️ SOC Safety Monitor started (independent of Simple Rule)")
    
    while True:
        try:
            await asyncio.sleep(10)  # Check every 10 seconds
            
            cfg = simple_rule.cfg
            battery_config = cfg.get("battery_config", {})
            
            for bid, bat_cfg in battery_config.items():
                min_soc = bat_cfg.get("minimum_soc_percent", 15)
                
                try:
                    # Get battery client
                    entry = _get_entry_for(bid)
                    if not entry:
                        logger.debug(f"🛡️ SOC SAFETY: Battery {bid} not found in registry")
                        continue
                    
                    client = entry['client']
                    lock = entry['lock']
                    
                    # Read SOC
                    async with lock:
                        battery_data = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                            timeout=5.0
                        )
                    
                    if not battery_data:
                        continue
                    
                    current_soc = battery_data.get("soc_percent", {}).get("value", 100)
                    battery_power = battery_data.get("battery_power", {}).get("value", 0)
                    work_mode = battery_data.get("work_mode", {}).get("value", -1)
                    
                    # Safety check: if SOC low AND discharging
                    if current_soc <= min_soc and battery_power < -50:
                        logger.warning(f"🛡️ SOC SAFETY: {bid} at {current_soc}% (min {min_soc}%) and discharging ({battery_power}W)")
                        logger.warning(f"🛡️ Emergency stop - force stop via Modbus control")
                        
                        async with lock:
                            # Na FW-update: set_work_mode alleen is niet betrouwbaar; expliciet stoppen
                            result = client.set_control("stop")
                        
                        if result.get("ok"):
                            simple_rule.battery_modes[bid] = "stopped_min_soc"
                            logger.info(f"✅ SOC SAFETY: Successfully stopped {bid}")
                        else:
                            logger.warning(f"⚠️ SOC SAFETY: Failed to stop {bid}, will retry")
                
                except asyncio.TimeoutError:
                    logger.debug(f"⚠️ SOC SAFETY: Timeout reading {bid}")
                except Exception as e:
                    logger.debug(f"⚠️ SOC SAFETY: Error checking {bid}: {e}")
        
        except Exception as e:
            logger.error(f"❌ SOC Safety Monitor error: {e}")
            await asyncio.sleep(5)

# ---------------------------------
# Battery Offline Monitor + WhatsApp Alert
# ---------------------------------
battery_offline_task = None
_battery_offline_since = {}  # bid -> datetime when first detected offline
_battery_offline_alerted = {}  # bid -> True if alert already sent

async def _battery_offline_monitor():
    """Monitor battery connectivity and send WhatsApp alert if offline > 10 minutes."""
    global _battery_offline_since, _battery_offline_alerted
    
    try:
        from whatsapp_notifier import whatsapp
    except ImportError:
        logger.error("❌ whatsapp_notifier.py not found, offline monitor disabled")
        return
    
    logger.info("🔌 Battery Offline Monitor started (alert after 10 min)")
    OFFLINE_THRESHOLD_SECONDS = 600  # 10 minutes
    
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            
            for bid in manager.registry:
                entry = _get_entry_for(bid)
                if not entry:
                    continue
                
                client = entry['client']
                lock = entry['lock']
                
                try:
                    async with lock:
                        battery_data = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(None, client.read_battery_data),
                            timeout=8.0
                        )
                    
                    if battery_data and battery_data.get("soc_percent"):
                        # Battery is online - reset tracking
                        was_offline = bid in _battery_offline_since
                        _battery_offline_since.pop(bid, None)
                        _battery_offline_alerted.pop(bid, None)
                        if was_offline:
                            soc = battery_data.get("soc_percent", {}).get("value", "?")
                            logger.info(f"✅ Battery {bid} is back ONLINE (SOC: {soc}%)")
                            await whatsapp.send_message("jos", 
                                f"✅ *Batterij {bid} is weer online!*\n\nSOC: {soc}%\n\n_myEnergy systeem_")
                    else:
                        # Battery offline
                        if bid not in _battery_offline_since:
                            _battery_offline_since[bid] = datetime.now()
                            logger.warning(f"⚠️ Battery {bid} offline detected")
                        
                        offline_seconds = (datetime.now() - _battery_offline_since[bid]).total_seconds()
                        
                        if offline_seconds >= OFFLINE_THRESHOLD_SECONDS and not _battery_offline_alerted.get(bid):
                            minutes = int(offline_seconds / 60)
                            logger.error(f"🚨 Battery {bid} offline for {minutes} minutes - sending alert!")
                            await whatsapp.send_message("jos",
                                f"🚨 *Batterij {bid} is offline!*\n\n"
                                f"⏱️ Al {minutes} minuten niet bereikbaar\n"
                                f"🔌 Host: {client.host}:{client.port}\n\n"
                                f"Controleer WiFi verbinding of herstart de batterij.\n\n"
                                f"_myEnergy systeem_")
                            _battery_offline_alerted[bid] = True
                
                except (asyncio.TimeoutError, Exception) as e:
                    # Timeout or error = offline
                    if bid not in _battery_offline_since:
                        _battery_offline_since[bid] = datetime.now()
                        logger.warning(f"⚠️ Battery {bid} offline (error: {e})")
                    
                    offline_seconds = (datetime.now() - _battery_offline_since[bid]).total_seconds()
                    
                    if offline_seconds >= OFFLINE_THRESHOLD_SECONDS and not _battery_offline_alerted.get(bid):
                        minutes = int(offline_seconds / 60)
                        logger.error(f"🚨 Battery {bid} offline for {minutes} minutes - sending alert!")
                        await whatsapp.send_message("jos",
                            f"🚨 *Batterij {bid} is offline!*\n\n"
                            f"⏱️ Al {minutes} minuten niet bereikbaar\n"
                            f"🔌 Host: {client.host}:{client.port}\n"
                            f"❌ Error: {str(e)[:100]}\n\n"
                            f"Controleer WiFi verbinding of herstart de batterij.\n\n"
                            f"_myEnergy systeem_")
                        _battery_offline_alerted[bid] = True
        
        except Exception as e:
            logger.error(f"❌ Battery Offline Monitor error: {e}")
            await asyncio.sleep(30)

# ---------------------------------
# WhatsApp Energy Tips Scheduler
# ---------------------------------
whatsapp_tips_task = None

async def _whatsapp_tips_scheduler():
    """Send smart energy tips at 09:00 and 13:00"""
    try:
        from whatsapp_notifier import whatsapp
    except ImportError:
        logger.error("❌ whatsapp_notifier.py not found, tips disabled")
        return
    
    logger.info("📱 WhatsApp tips scheduler started (09:00 & 13:00)")
    
    # Track which tips we sent today
    sent_today = set()
    
    while True:
        try:
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            today_key = now.strftime("%Y-%m-%d")
            
            # Reset sent_today at midnight
            if current_hour == 0 and current_minute == 0:
                sent_today.clear()
            
            # Check if it's time to send (09:00 or 13:00)
            # Use 5-minute window to handle timing drift (09:00-09:04 or 13:00-13:04)
            should_send = False
            tip_time = ""
            
            if current_hour == 9 and current_minute < 5:
                tip_time = "09:00"
                should_send = f"{today_key}-09" not in sent_today
            elif current_hour == 13 and current_minute < 5:
                tip_time = "13:00"
                should_send = f"{today_key}-13" not in sent_today
            
            if should_send:
                logger.info(f"📱 Sending energy tip at {tip_time}")
                
                # Gather current energy data
                try:
                    # Get weather + FORECAST
                    weather_data = await weather_service.get_current_weather()
                    clouds = weather_data.get("clouds", 100)
                    temp = weather_data.get("temperature", 0)
                    
                    # Get hourly forecast for next 6 hours
                    forecast_data = await weather_service.get_forecast(hours=6)
                    forecast_6h = forecast_data.get("forecasts", [])[:6] if forecast_data else []
                    
                    # Calculate average clouds next 6 hours
                    future_clouds = [f.get("clouds", 100) for f in forecast_6h] if forecast_6h else [clouds]
                    avg_future_clouds = sum(future_clouds) / len(future_clouds) if future_clouds else clouds
                    
                    # Use same data as dashboard - call /api/status
                    status = await get_status()
                    
                    pv_w = status.get("pv_generation_w", 0) or 0
                    grid_w = status.get("grid_w", 0) or 0
                    eddi_w = status.get("eddi_power_w", 0) or 0
                    zappi_w = status.get("zappi_power_w", 0) or 0
                    house_w = status.get("house_consumption_w", 0) or 0
                    
                    # Get battery SOC from simple rule status (has all batteries)
                    avg_soc = 50  # default
                    total_batt_power = status.get("marstek_power_w", 0) or 0
                    
                    # Try to get more accurate SOC from simple rule
                    try:
                        sr_status = simple_rule.last
                        per_battery = sr_status.get("per_battery", {})
                        if per_battery:
                            socs = [b.get("soc", 50) for b in per_battery.values() if "soc" in b]
                            if socs:
                                avg_soc = sum(socs) / len(socs)
                    except:
                        pass
                    
                    # Calculate overschot (excluding Eddi AND Zappi)
                    export_w = max(0, -grid_w) if grid_w else 0
                    overschot = export_w - eddi_w - zappi_w if export_w > 0 else 0
                    
                    energy_data = {
                        "clouds": clouds,
                        "forecast_clouds": int(avg_future_clouds),  # Avg clouds next 6h
                        "temperature": temp,
                        "pv_now_w": pv_w,
                        "grid_w": grid_w,
                        "battery_soc": int(avg_soc),
                        "battery_power": int(total_batt_power),
                        "overschot_w": int(overschot),
                        "eddi_w": eddi_w,
                        "zappi_w": zappi_w,
                        "house_w": house_w,
                        "hour": current_hour  # 9 of 13
                    }
                    
                    # Send tip!
                    await whatsapp.send_energy_tip(energy_data)
                    
                    # Mark as sent
                    sent_today.add(f"{today_key}-{current_hour:02d}")
                    logger.info(f"✅ Energy tip sent at {tip_time}")
                
                except Exception as e:
                    logger.error(f"❌ Failed to gather data for energy tip: {e}")
            
            # Check every minute
            await asyncio.sleep(60)
        
        except Exception as e:
            logger.error(f"❌ WhatsApp tips scheduler error: {e}")
            await asyncio.sleep(60)

# ---------------------------------
# Startup/shutdown: auto-start simple rule + SOC safety + WhatsApp tips
# ---------------------------------
@app.on_event("startup")
async def _startup_simple_rule():
    """Auto-start the simple export-driven rule on app boot.
    Keeps behavior resilient after crashes/restarts.
    """
    global soc_safety_task, whatsapp_tips_task, battery_offline_task
    
    try:
        # Start SOC Safety Monitor (always running!)
        soc_safety_task = asyncio.create_task(_soc_safety_monitor())
        logger.info("🛡️ SOC Safety Monitor started")
        
        # Start WhatsApp tips scheduler
        whatsapp_tips_task = asyncio.create_task(_whatsapp_tips_scheduler())
        logger.info("📱 WhatsApp tips scheduler started")
        
        # Battery Offline Monitor - DISABLED (too many false alerts)
        # battery_offline_task = asyncio.create_task(_battery_offline_monitor())
        logger.info("🔌 Battery Offline Monitor DISABLED (te veel meldingen)")
        
        # Start Phase Monitor (3x25A check)
        await phase_monitor.start()
        logger.info("🔌 Phase Monitor started")
        
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
    """Ensure the simple rule loop and SOC safety monitor stop cleanly on shutdown."""
    global soc_safety_task, whatsapp_tips_task
    
    try:
        # Stop SOC Safety Monitor
        if soc_safety_task and not soc_safety_task.done():
            try:
                soc_safety_task.cancel()
                logger.info("🛑 SOC Safety Monitor stopped")
            except Exception:
                pass
        
        # Stop WhatsApp tips scheduler
        if whatsapp_tips_task and not whatsapp_tips_task.done():
            try:
                whatsapp_tips_task.cancel()
                logger.info("🛑 WhatsApp tips scheduler stopped")
            except Exception:
                pass
        
        # Stop Simple Rule
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
# Weather API
# =========================
from weather import weather_service

@app.get("/api/weather/current")
async def get_current_weather():
    """Get current weather conditions"""
    try:
        data = await weather_service.get_current_weather()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/weather/forecast")
async def get_weather_forecast():
    """Get weather forecast for next 24 hours"""
    try:
        data = await weather_service.get_forecast(hours=24)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/weather/solar")
async def get_solar_forecast():
    """Get solar-relevant weather forecast"""
    try:
        data = await weather_service.get_solar_forecast()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/weather/solar-hours")
async def get_weather_solar_hours(hours: int = 12):
    """Compacte zonne-verwachting per uur voor sidebar weergave."""
    try:
        h = max(1, min(24, int(hours)))
        data = await weather_service.get_solar_hours(hours=h)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

# =========================
# Frank Energie prices (display)
# =========================
from frank_energie import frank_client

@app.get("/api/frank/prices")
async def get_frank_prices(force: bool = False):
    """Frank Energie dynamic electricity prices (today + tomorrow if published)."""
    try:
        data = await frank_client.get_overview(force=force)
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Frank prices error: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/prices/compare")
async def get_prices_compare(force: bool = False):
    """Vergelijk uurtarieven: Zonneplan + NextEnergy (naast Frank).

    Zonneplan: publieke scrape. NextEnergy: Enever (ENEVER_TOKEN in .env).
    Alleen tonen — stuurt de batterij niet.
    """
    try:
        from price_providers import price_providers
        data = await price_providers.get_compare(force=force)
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Price compare error: {e}")
        return {"success": False, "error": str(e)}

# =========================
# WhatsApp Test Endpoint
# =========================
@app.post("/api/whatsapp/test")
async def test_whatsapp(contact: str = "jos"):
    """Send test WhatsApp message"""
    try:
        from whatsapp_notifier import whatsapp
        
        message = f"🧪 Test bericht van myEnergy systeem!\n\nVerstuurd om {datetime.now().strftime('%H:%M:%S')}\n\n_Dit is een test_"
        
        success = await whatsapp.send_message(contact, message)
        
        return {
            "success": success,
            "message": "Test message sent!" if success else "Failed to send",
            "contact": contact
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/whatsapp/tip/now")
async def send_tip_now():
    """Manually trigger energy tip (for testing)"""
    try:
        from whatsapp_notifier import whatsapp
        
        # Get weather + FORECAST
        weather_data = await weather_service.get_current_weather()
        clouds = weather_data.get("clouds", 100)
        temp = weather_data.get("temperature", 0)
        
        # Get hourly forecast for next 6 hours
        forecast_data = await weather_service.get_forecast(hours=6)
        forecast_6h = forecast_data.get("forecasts", [])[:6] if forecast_data else []
        
        # Calculate average clouds next 6 hours
        future_clouds = [f.get("clouds", 100) for f in forecast_6h] if forecast_6h else [clouds]
        avg_future_clouds = sum(future_clouds) / len(future_clouds) if future_clouds else clouds
        
        # Use same data as dashboard - call /api/status
        status = await get_status()
        
        pv_w = status.get("pv_generation_w", 0) or 0
        grid_w = status.get("grid_w", 0) or 0
        eddi_w = status.get("eddi_power_w", 0) or 0
        zappi_w = status.get("zappi_power_w", 0) or 0
        house_w = status.get("house_consumption_w", 0) or 0
        
        # Get battery SOC from simple rule status (has all batteries)
        avg_soc = 50  # default
        total_batt_power = status.get("marstek_power_w", 0) or 0
        
        # Try to get more accurate SOC from simple rule
        try:
            sr_status = simple_rule.last
            per_battery = sr_status.get("per_battery", {})
            if per_battery:
                socs = [b.get("soc", 50) for b in per_battery.values() if "soc" in b]
                if socs:
                    avg_soc = sum(socs) / len(socs)
        except:
            pass
        
        # Calculate overschot (excluding Eddi AND Zappi)
        export_w = max(0, -grid_w) if grid_w else 0
        overschot = export_w - eddi_w - zappi_w if export_w > 0 else 0
        
        energy_data = {
            "clouds": clouds,
            "forecast_clouds": int(avg_future_clouds),  # Avg clouds next 6h
            "temperature": temp,
            "pv_now_w": pv_w,
            "grid_w": grid_w,
            "battery_soc": int(avg_soc),
            "battery_power": int(total_batt_power),
            "overschot_w": int(overschot),
            "eddi_w": eddi_w,
            "zappi_w": zappi_w,
            "house_w": house_w,
            "hour": datetime.now().hour  # Current hour
        }
        
        await whatsapp.send_energy_tip(energy_data)
        
        return {"success": True, "message": "Energy tip sent!", "data": energy_data}
    
    except Exception as e:
        logger.error(f"Failed to send manual tip: {e}")
        return {"success": False, "error": str(e)}

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

@app.get("/api/energy/today")
async def energy_today():
    """Get today's energy totals (kWh) + Frank-based costs."""
    data = energy_tracker.get_today()
    # Schatting voor import vóór Frank-tracking (zelfde dag, vroege uren)
    if (data.get("import_cost_eur") or 0) <= 0 and (data.get("import_kwh") or 0) > 0:
        try:
            from frank_energie import frank_client as _frank_e
            ov = await _frank_e.get_overview()
            prices = [p["price_eur_kwh"] for p in ((ov.get("today") or {}).get("prices") or [])]
            if prices:
                avg = sum(prices) / len(prices)
                imp = float(data.get("import_kwh") or 0)
                gen = float(data.get("pv_kwh") or 0) + float(data.get("batt_discharge_kwh") or 0)
                data["import_cost_eur"] = round(imp * avg, 2)
                data["saved_cost_eur"] = round(gen * avg, 2)
                data["cost_method"] = "frank_avg_estimate"
        except Exception:
            pass
    return data

@app.get("/api/energy/history")
async def energy_history(days: int = 7):
    """Get energy history for last N days."""
    return energy_tracker.get_history(min(days, 365))

@app.get("/api/energy/csv")
async def energy_csv(days: int = 90):
    """Export energy history as CSV."""
    from starlette.responses import Response
    history = energy_tracker.get_history(min(days, 365))
    lines = ["Datum,PV kWh,Export kWh,Import kWh,Huis kWh,Eddi kWh,Zappi kWh,Batt Laden kWh,Batt Ontladen kWh,Zelfverbruik %,Eigen Opwek kWh,Kosten EUR,Bespaard EUR"]
    for d in history:
        gen = (d.get("pv_kwh", 0) or 0) + (d.get("batt_discharge_kwh", 0) or 0)
        imp = d.get("import_kwh", 0) or 0
        lines.append(",".join([
            d["date"],
            str(d.get("pv_kwh", 0)),
            str(d.get("export_kwh", 0)),
            str(d.get("import_kwh", 0)),
            str(d.get("house_kwh", 0)),
            str(d.get("eddi_kwh", 0)),
            str(d.get("zappi_kwh", 0)),
            str(d.get("batt_charge_kwh", 0)),
            str(d.get("batt_discharge_kwh", 0)),
            str(d.get("self_consumption_pct", 0)),
            str(round(gen, 2)),
            str(round(imp * 0.23, 2)),
            str(round(gen * 0.23, 2)),
        ]))
    csv_text = "\n".join(lines)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=energie_overzicht_{days}d.csv"}
    )

@app.get("/rapport")
async def energy_report_page():
    """Serve the energy report page."""
    try:
        with open("rapport.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: {e}</h1>", status_code=500)

@app.get("/app")
async def app_wrapper_page():
    """Serve the wrapper page with both Flow and Dashboard in iframes."""
    try:
        with open("app.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<h1>Error: {e}</h1>", status_code=500)

@app.get("/flow.html")
async def flow_visualization_page():
    """Serve the energy flow visualization page."""
    try:
        with open("flow.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"Flow page not available: {e}", status_code=500)

@app.get("/flow2.html")
async def flow2_visualization_page():
    """Serve the new energy flow visualization page."""
    try:
        with open("flow2.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"Flow2 page not available: {e}", status_code=500)

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
            # Wrap in timeout to prevent hanging if Modbus doesn't respond
            battery_data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, venus_modbus.read_battery_data),
                timeout=5.0
            )
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
        # Add timeout to prevent hanging on Modbus issues
        result = await asyncio.wait_for(manager.read_status(bid), timeout=5.0)
        return result
    except asyncio.TimeoutError:
        return {
            "success": False, 
            "error": "Battery timeout (Modbus not responding)",
            "battery_id": bid,
            "soc_percent": {"value": None, "unit": "%"},
            "battery_voltage": {"value": None, "unit": "V"},
            "battery_current": {"value": None, "unit": "A"},
            "battery_power": {"value": None, "unit": "W"}
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/battery2/status")
async def get_battery2_status():
    """Get real-time battery 2 status via Modbus (WiFi converter)."""
    try:
        # Wrap sync Modbus call with timeout to prevent hanging
        async with modbus_lock2:
            battery_data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, venus_modbus2.read_battery_data),
                timeout=5.0
            )
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
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "Battery timeout (Modbus not responding after 5s)",
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
        
        # Reload limits in SimpleRule so it uses new config immediately
        simple_rule.reload_battery_limits()
        
        if auto_charge:
            result = venus_modbus.check_minimum_soc(min_soc)
        else:
            # Just check, don't take action
            battery_data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, venus_modbus.read_battery_data),
                timeout=5.0
            )
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
        
        # Reload limits in SimpleRule so it uses new config immediately
        simple_rule.reload_battery_limits()

        vm = _get_modbus_for(bid)
        if auto_charge:
            # enforce and/or start emergency charge if needed
            # BELANGRIJK: Geef Simple Rule status mee - emergency charge alleen als Simple Rule UIT staat!
            result = vm.check_minimum_soc(min_soc, simple_rule_enabled=simple_rule.enabled)
            ok = bool(result.get("ok", False))
            return {"success": ok, **result, "id": bid}
        else:
            # passive check
            bd = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, vm.read_battery_data),
                timeout=5.0
            )
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
                bd = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, venus_modbus.read_battery_data),
                    timeout=5.0
                )
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
            data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, venus_modbus.read_battery_data),
                timeout=5.0
            )
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

@app.get("/api/p1/test")
async def test_p1_connection():
    """Test P1 meter connection"""
    if not p1_reader:
        return {"success": False, "error": "P1_METER_IP not configured"}
    
    try:
        data = await p1_reader.read_data()
        if data:
            return {
                "success": True,
                "ip": P1_METER_IP,
                "data": data,
                "has_phase_data": "active_power_l1_w" in data
            }
        else:
            return {
                "success": False,
                "ip": P1_METER_IP,
                "error": "No data received - Is Local API enabled?"
            }
    except Exception as e:
        return {"success": False, "ip": P1_METER_IP, "error": str(e)}

@app.get("/api/myenergi/phases")
async def get_phase_data():
    """Get 3-phase power data from Zappi Grid CT or Harvi CT clamps.
    Returns power per phase (L1, L2, L3) and total.
    """
    try:
        async with myenergi_lock:
            data = await myenergi.status_all()
        
        raw = data.get("raw", [])
        phases = {
            "l1_w": None,
            "l2_w": None, 
            "l3_w": None,
            "total_w": 0,
            "source": None
        }
        
        # Priority 1: Check Zappi for grid CT clamps (ectp4/5/6)
        for section in raw if isinstance(raw, list) else []:
            if isinstance(section, dict) and "zappi" in section:
                zappi_list = section.get("zappi") or []
                for zappi in zappi_list:
                    # ACTUAL MAPPING (verified with P1 meter):
                    # ectp4 = Fase B, ectp5 = Fase A, ectp6 = Fase C
                    ectp4 = zappi.get("ectp4")  # Fase B
                    ectp5 = zappi.get("ectp5")  # Fase A
                    ectp6 = zappi.get("ectp6")  # Fase C
                    
                    if ectp4 is not None or ectp5 is not None or ectp6 is not None:
                        # Correct mapping: ectp4(B)→L1, ectp5(A)→L2, ectp6(C)→L3
                        phases["l1_w"] = int(ectp4) if ectp4 is not None else 0  # Fase B
                        phases["l2_w"] = int(ectp5) if ectp5 is not None else 0  # Fase A
                        phases["l3_w"] = int(ectp6) if ectp6 is not None else 0  # Fase C
                        phases["source"] = f"Zappi Grid CT"
                        break
        
        # Priority 2: Find Harvi with CT clamps (fallback)
        if phases["source"] is None:
            for section in raw if isinstance(raw, list) else []:
                if isinstance(section, dict) and "harvi" in section:
                    harvi_list = section.get("harvi") or []
                    for harvi in harvi_list:
                        # Check which CT types are configured (Generation/Grid/etc)
                        ct1_type = harvi.get("ectt1")  # CT type for clamp 1
                        ct2_type = harvi.get("ectt2")
                        ct3_type = harvi.get("ectt3")
                        
                        # Read power values (positive or negative depending on direction)
                        ectp1 = harvi.get("ectp1")  # Phase L1
                        ectp2 = harvi.get("ectp2")  # Phase L2  
                        ectp3 = harvi.get("ectp3")  # Phase L3
                        
                        if ectp1 is not None:
                            phases["l1_w"] = int(ectp1)
                            phases["l1_type"] = ct1_type
                        if ectp2 is not None:
                            phases["l2_w"] = int(ectp2)
                            phases["l2_type"] = ct2_type
                        if ectp3 is not None:
                            phases["l3_w"] = int(ectp3)
                            phases["l3_type"] = ct3_type
                        
                        phases["source"] = f"Harvi SN: {harvi.get('sno', 'unknown')}"
                        break
        
        # Calculate total (sum of all phases that have data)
        total = 0
        for phase in [phases.get("l1_w"), phases.get("l2_w"), phases.get("l3_w")]:
            if phase is not None:
                total += phase
        phases["total_w"] = total
        
        # Calculate balance (how evenly distributed)
        active_phases = [p for p in [phases.get("l1_w"), phases.get("l2_w"), phases.get("l3_w")] if p is not None]
        if len(active_phases) > 1:
            avg = sum(active_phases) / len(active_phases)
            max_diff = max(abs(p - avg) for p in active_phases)
            phases["balance_percent"] = round(100 - (max_diff / (abs(avg) + 1) * 100), 1) if avg != 0 else 100
        else:
            phases["balance_percent"] = None
        
        return {"success": True, "phases": phases}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

# =========================
# Phase Monitor Endpoints (3x25A Check)
# =========================

@app.post("/api/phase_monitor/start")
async def start_phase_monitor():
    """Start de fase monitor"""
    try:
        await phase_monitor.start()
        return {"success": True, "message": "Phase monitor gestart"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/stop")
async def stop_phase_monitor():
    """Stop de fase monitor"""
    try:
        await phase_monitor.stop()
        return {"success": True, "message": "Phase monitor gestopt"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/status")
async def get_phase_monitor_status():
    """Haal huidige status en statistieken op"""
    try:
        stats = phase_monitor.get_stats()
        return {"success": True, **stats}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/violations")
async def get_phase_violations(hours: int = Query(24)):
    """Haal overschrijdingen op van laatste X uur"""
    try:
        violations = phase_monitor.get_violations(hours)
        return {
            "success": True,
            "hours": hours,
            "count": len(violations),
            "violations": violations
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/violations/{timestamp}/dismiss")
async def dismiss_phase_violation(timestamp: str, reason: str = Body(..., embed=True)):
    """Markeer een violation als dismissed met reden
    
    Deze violation blijft zichtbaar maar telt NIET mee voor 3x25A analyse.
    Gebruik voor: batterij laden, test situaties, bewuste overschrijdingen.
    
    Args:
        timestamp: ISO timestamp van de violation
        reason: Reden voor dismiss (bijv. "Batterij laden vanaf grid")
    
    Example:
        POST /api/phase_monitor/violations/2025-10-12T22:00:00/dismiss
        Body: {"reason": "Batterij laden vanaf grid"}
    """
    try:
        result = phase_monitor.dismiss_violation(timestamp, reason)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/violations/{timestamp}/undismiss")
async def undismiss_phase_violation(timestamp: str):
    """Verwijder dismiss markering van een violation
    
    Args:
        timestamp: ISO timestamp van de violation
    
    Example:
        POST /api/phase_monitor/violations/2025-10-12T22:00:00/undismiss
    """
    try:
        result = phase_monitor.undismiss_violation(timestamp)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/reset_peaks")
async def reset_phase_monitor_peaks():
    """Reset alleen de max waarden per fase (niet de metingen)
    
    Dit reset:
    - Max Fase A/B/C waarden
    - Peak history
    
    Behoudt:
    - Alle metingen
    - Violations log
    
    Example:
        POST /api/phase_monitor/reset_peaks
    """
    try:
        result = phase_monitor.reset_peak_values()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/reset")
async def reset_phase_monitor_stats(keep_violations: bool = True):
    """Reset alle phase monitor statistieken
    
    Args:
        keep_violations: Behoud violations_log (default True) of ook resetten
    
    Returns:
        Aantal verwijderde entries
    
    Example:
        POST /api/phase_monitor/reset
        Body: {"keep_violations": true}
        
        Of zonder body (gebruikt default True):
        POST /api/phase_monitor/reset
    """
    try:
        result = phase_monitor.reset_stats(keep_violations=keep_violations)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/phase_monitor/violations/bulk_dismiss")
async def bulk_dismiss_violations(
    start_time: str = Body(None),
    end_time: str = Body(None),
    max_overshoot_w: int = Body(None),
    phase: str = Body(None),
    reason: str = Body("Bulk dismiss")
):
    """Dismiss meerdere violations in één keer
    
    Args:
        start_time: ISO timestamp start (optioneel)
        end_time: ISO timestamp einde (optioneel)
        max_overshoot_w: Max overschrijding in Watt (bijv. 200 = <200W over limiet)
        phase: Filter op fase (L1/L2/L3) of None voor alle
        reason: Reden voor dismiss
    
    Examples:
        # Dismiss alles tussen 22:12-22:14
        POST /api/phase_monitor/violations/bulk_dismiss
        Body: {"start_time": "2025-10-12T22:12:00", "end_time": "2025-10-12T22:14:00", "reason": "Batterij laden test"}
        
        # Dismiss alle kleine overschrijdingen (<200W)
        POST /api/phase_monitor/violations/bulk_dismiss  
        Body: {"max_overshoot_w": 200, "reason": "Kleine pieken, niet significant"}
        
        # Dismiss alle L3 violations vandaag
        POST /api/phase_monitor/violations/bulk_dismiss
        Body: {"phase": "L3", "start_time": "2025-10-12T00:00:00", "reason": "L3 balancering issue"}
    """
    try:
        result = phase_monitor.bulk_dismiss_violations(
            start_time=start_time,
            end_time=end_time,
            max_overshoot_w=max_overshoot_w,
            phase=phase,
            reason=reason
        )
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/data")
async def get_phase_data_history(count: int = Query(100)):
    """Haal recente fase data op"""
    try:
        data = phase_monitor.get_recent_data(count)
        return {
            "success": True,
            "count": len(data),
            "data": data
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/analysis")
async def get_feasibility_analysis():
    """Analyseer of 3x25A haalbaar is"""
    try:
        analysis = phase_monitor.analyze_feasibility()
        return {
            "success": True,
            **analysis
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/patterns")
async def get_peak_patterns():
    """Analyseer patronen in piekbelasting: uren, weekdagen, periodes
    
    BELANGRIJK: Gebruikt ALLEEN metingen ZONDER Zappi laden!
    Reden: Zappi heeft load balancing en zou zich aanpassen aan 3x25A.
    Dit geeft het échte huishoudelijke verbruik zonder vertekening.
    
    Returns:
        - data_filter: Info over gefilterde data (hoeveel Zappi metingen uitgesloten)
        - summary: Snelle overview met hoogste uren en dagen
        - by_hour: Statistieken per uur van de dag (0-23)
        - by_weekday: Statistieken per dag van de week
        - by_period: Statistieken per dagdeel (nacht/ochtend/middag/avond)
        - risky_hours: Uren waar gemiddeld >80% van limiet wordt gebruikt
        - recommendations: Concrete aanbevelingen op basis van patronen
    """
    try:
        patterns = phase_monitor.analyze_peak_patterns()
        return {
            "success": True,
            **patterns
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/peak_history")
async def get_peak_history(phase: str = Query(None)):
    """Haal peak history op - alle keren dat een nieuwe max werd bereikt"""
    try:
        history = phase_monitor.get_peak_history(phase)
        return {
            "success": True,
            "history": history
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/top_peaks")
async def get_top_peaks(limit: int = Query(50), include_below_limit: bool = Query(True)):
    """Haal TOP N hoogste pieken op (inclusief die ONDER de limiet blijven)
    
    Args:
        limit: Aantal top pieken per fase (default 50, max 200)
        include_below_limit: Ook metingen onder limiet tonen (default True)
    
    Returns:
        - overall_top: Top pieken over alle fases
        - l1_top, l2_top, l3_top: Top per specifieke fase
        - near_misses: Hoge waarden die net onder limiet blijven (80-100%)
        - Elk item: value, distance_to_limit, timestamp, zappi status, alle fase waarden
    """
    try:
        # Limiteer tot max 200 voor performance
        limit = min(limit, 200)
        
        top = phase_monitor.get_top_peaks(limit, include_below_limit)
        return {
            "success": True,
            **top
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/phase_monitor/top_violations")
async def get_top_violations(limit: int = Query(20)):
    """Haal TOP N ergste fase overschrijdingen op met alle details
    
    Args:
        limit: Aantal top violations per fase (default 20, max 100)
    
    Returns:
        - overall_top: Top violations over alle fases
        - l1_top, l2_top, l3_top: Top per specifieke fase
        - Elk item bevat: value, overshoot, timestamp, zappi status, alle fase waarden
    """
    try:
        # Limiteer tot max 100 voor performance
        limit = min(limit, 100)
        
        top = phase_monitor.get_top_violations(limit)
        return {
            "success": True,
            **top
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
            test_data = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, venus_modbus.read_battery_data),
                timeout=5.0
            )
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
        """Execute Eddi Priority rule with temperature checking and anti-feed mode."""
        try:
            # Get current system values
            grid_w = myenergi_data.get("grid_w", 0)
            eddi_w = myenergi_data.get("eddi_w", 0)
            pv_w = myenergi_data.get("pv_generation_w", 0)
            
            # Get tank temperature (with override support)
            tank_temp = await get_tank_temperature_with_override(rule_params)
            target_temp = rule_params.get("tank_temp_target", 60)
            
            # Anti-feed mode parameters
            pv_threshold = rule_params.get("pv_threshold_w", 50)  # Min PV to consider "sun is shining"
            import_threshold = rule_params.get("import_threshold_w", 100)  # Min grid import to trigger anti-feed
            
            logger.info(f"🔥 EDDI RULE: Grid={grid_w}W, Eddi={eddi_w}W, PV={pv_w}W, Tank={tank_temp}°C (target={target_temp}°C)")
            
            # Check if tank is warm enough
            if tank_temp < target_temp:
                logger.info(f"🔥 EDDI RULE: Tank too cold ({tank_temp}°C < {target_temp}°C) - Eddi has priority")
                # Set battery to minimal power or stop charging
                await self.set_battery_minimal_power(rule)
                return
            
            # Check for anti-feed mode condition: no PV + importing from grid
            if pv_w < pv_threshold and grid_w > import_threshold:
                logger.info(f"☀️ EDDI RULE: No PV ({pv_w}W) + importing ({grid_w}W) - Activating ANTI-FEED mode")
                await self.set_battery_anti_feed(rule)
                return
            
            # Tank is warm enough and PV available, apply normal Eddi priority logic
            export_w = max(0, -grid_w)  # Negative grid = export
            buffer_w = rule_params.get("eddi_buffer_w", 200)
            threshold_w = rule_params.get("export_threshold_w", 100)
            
            available_for_battery = export_w - eddi_w - buffer_w
            
            logger.info(f"🔥 EDDI RULE: Export={export_w}W, Available for battery={available_for_battery}W")
            
            if available_for_battery > threshold_w:
                max_battery_w = rule_params.get("max_battery_power_w", 1500)
                target_power = min(available_for_battery, max_battery_w)
                logger.info(f"🔥 EDDI RULE: Setting battery to {target_power}W (CHARGING)")
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
    
    async def set_battery_anti_feed(self, rule):
        """Set battery to anti-feed mode (discharge to supply house)."""
        try:
            batteries = rule.get("batteries", {})
            for battery_id, enabled in batteries.items():
                if enabled and battery_id == "venus_e_78":
                    logger.info(f"⚡ Setting {battery_id} to ANTI-FEED mode")
                    # Get the VenusE client and set work mode to Anti-Feed (mode=1)
                    venus_e_client = VenusEModbusClient(
                        host=os.getenv("MARSTEK_MODBUS_HOST", "192.168.0.198"),
                        port=int(os.getenv("MARSTEK_MODBUS_PORT", "502"))
                    )
                    result = venus_e_client.set_work_mode(1)  # 1 = Anti-Feed mode
                    logger.info(f"⚡ Battery anti-feed result: {result}")
        except Exception as e:
            logger.error(f"⚡ Battery anti-feed error: {e}")
    
    async def set_battery_power(self, rule, power_w):
        """Set battery to specific power (charge mode with Eddi priority)."""
        try:
            batteries = rule.get("batteries", {})
            for battery_id, enabled in batteries.items():
                if enabled and battery_id == "venus_e_78":
                    # First, ensure we're back in Manual mode (mode=0) from Anti-Feed
                    # This allows us to control charge/discharge manually
                    logger.info(f"🔋 Setting {battery_id} to Manual mode for charging")
                    venus_e_client = VenusEModbusClient(
                        host=os.getenv("MARSTEK_MODBUS_HOST", "192.168.0.198"),
                        port=int(os.getenv("MARSTEK_MODBUS_PORT", "502"))
                    )
                    mode_result = venus_e_client.set_work_mode(0)  # 0 = Manual mode
                    logger.info(f"🔋 Mode switch result: {mode_result}")
                    
                    # Now set the charging power
                    logger.info(f"🔋 Setting {battery_id} to {power_w}W")
                    result = await set_battery_power("venus_e_78", power_w)
                    logger.info(f"🔋 Battery power result: {result}")
        except Exception as e:
            logger.error(f"🔋 Battery power error: {e}")

# Replace the rules engine
rules_engine = EnhancedSimpleRulesEngine()

