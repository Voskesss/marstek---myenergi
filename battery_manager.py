#!/usr/bin/env python3
"""
Minimal BatteryManager to support multiple batteries via a uniform API.
This first iteration simply wraps the two existing Modbus clients from app.py
using dependency injection at init time.

Usage:
  from battery_manager import BatteryManager
  mgr = BatteryManager({
      'b1': {'client': venus_modbus,  'lock': modbus_lock},
      'b2': {'client': venus_modbus2, 'lock': modbus_lock2},
  })
  status = await mgr.read_status('b2')
"""

from __future__ import annotations
from typing import Dict, Any
from datetime import datetime


class BatteryManager:
    def __init__(self, registry: Dict[str, Dict[str, Any]]):
        # registry: id -> { 'client': ModbusClientWrapper, 'lock': asyncio.Lock }
        self.registry = registry or {}

    def has(self, bid: str) -> bool:
        return bid in self.registry

    async def read_status(self, bid: str) -> Dict[str, Any]:
        if bid not in self.registry:
            return {"success": False, "error": f"unknown battery id: {bid}"}

        entry = self.registry[bid]
        client = entry.get('client')
        lock = entry.get('lock')
        if client is None or lock is None:
            return {"success": False, "error": "client/lock missing"}

        # Serialize access per device
        async with lock:
            data = client.read_battery_data()
            try:
                client.disconnect()
            except Exception:
                pass

        if not data:
            return {"success": False, "error": "no data"}

        # Derived
        try:
            soc = float(data.get("soc_percent", {}).get("value"))
        except Exception:
            soc = None
        try:
            v = float(data.get("battery_voltage", {}).get("value", 0.0))
        except Exception:
            v = 0.0
        try:
            i = float(data.get("battery_current", {}).get("value", 0.0))
        except Exception:
            i = 0.0
        calc_power_w = v * i
        raw_bp = data.get("battery_power", {})
        power_w = raw_bp.get("value") if isinstance(raw_bp, dict) else None
        if not isinstance(power_w, (int, float)):
            power_w = calc_power_w
        work_mode_raw = data.get("work_mode", {}).get("raw")
        mode_map = {0: "Standby", 1: "Charging", 2: "Discharging", 3: "Backup", 4: "Fault", 5: "Idle", 6: "Self-Regulating"}
        mode = mode_map.get(work_mode_raw)
        if not mode:
            mode = "Idle" if abs(calc_power_w) < 20 else ("Charging" if calc_power_w > 0 else "Discharging")

        # Estimate remaining energy if soc is known
        FULL_KWH = 5.12  # align with app.py BATTERY_FULL_KWH
        remaining_kwh = (FULL_KWH * (soc/100.0)) if soc is not None else None

        return {
            "success": True,
            "data": data,
            "derived": {
                "soc_percent": soc,
                "power_w": power_w,
                "calc_power_w": calc_power_w,
                "mode": mode,
                "remaining_kwh": remaining_kwh,
            },
            "timestamp": datetime.now().isoformat(),
        }
