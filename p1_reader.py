#!/usr/bin/env python3
"""
HomeWizard P1 Meter Reader
===========================

Leest fase data van P1 meter via HomeWizard compatibele API.
Gebruikt voor accurate 3-fase monitoring.
"""

import httpx
import logging
from typing import Optional, Dict

logger = logging.getLogger("p1-reader")

class P1Reader:
    """Read P1 meter data via HomeWizard API"""
    
    def __init__(self, ip_address: str, use_https: bool = False):
        self.ip = ip_address
        self.protocol = "https" if use_https else "http"
        self.base_url = f"{self.protocol}://{ip_address}"
        self.client = httpx.AsyncClient(verify=False, timeout=3.0)
    
    async def read_data(self) -> Optional[Dict]:
        """
        Read P1 meter data.
        
        Returns:
        {
            "active_power_w": 3800,           # Total power (positive = import)
            "active_power_l1_w": 1200,        # Phase L1 power
            "active_power_l2_w": 1300,        # Phase L2 power  
            "active_power_l3_w": 1300,        # Phase L3 power
            "active_voltage_l1_v": 230.1,     # Phase L1 voltage
            "active_voltage_l2_v": 229.8,
            "active_voltage_l3_v": 230.5,
            "active_current_l1_a": 5.2,       # Phase L1 current
            "active_current_l2_a": 5.6,
            "active_current_l3_a": 5.6,
            "total_power_import_t1_kwh": 12345.678,
            "total_power_export_t1_kwh": 5432.123,
            "gas_timestamp": "251010203000W",
            "gas_total_m3": 1234.567
        }
        
        Note: Fields that are not available (e.g. on single-phase meters) 
              will be missing from the response.
        """
        try:
            # Try API v1 (no token required)
            response = await self.client.get(f"{self.base_url}/api/v1/data")
            
            if response.status_code == 200:
                data = response.json()
                logger.debug(f"P1 data: {data}")
                return data
            else:
                logger.warning(f"P1 meter returned status {response.status_code}")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"P1 meter at {self.ip} timeout")
            return None
        except httpx.ConnectError:
            logger.debug(f"P1 meter at {self.ip} connection failed (Local API disabled?)")
            return None
        except Exception as e:
            logger.error(f"P1 meter read error: {e}")
            return None
    
    async def get_phase_data(self) -> Optional[Dict]:
        """
        Get phase-specific power data.
        
        Returns:
        {
            "l1_w": 1200,
            "l2_w": 1300,
            "l3_w": 1300,
            "total_w": 3800,
            "l1_v": 230.1,
            "l2_v": 229.8,
            "l3_v": 230.5,
            "l1_a": 5.2,
            "l2_a": 5.6,
            "l3_a": 5.6
        }
        
        Returns None if data unavailable or single-phase meter.
        """
        data = await self.read_data()
        if not data:
            return None
        
        # Check if phase data exists (3-phase meter)
        if "active_power_l1_w" not in data:
            logger.info("Single-phase P1 meter detected (no per-phase data)")
            return None
        
        return {
            "l1_w": data.get("active_power_l1_w"),
            "l2_w": data.get("active_power_l2_w"),
            "l3_w": data.get("active_power_l3_w"),
            "total_w": data.get("active_power_w"),
            "l1_v": data.get("active_voltage_l1_v"),
            "l2_v": data.get("active_voltage_l2_v"),
            "l3_v": data.get("active_voltage_l3_v"),
            "l1_a": data.get("active_current_l1_a"),
            "l2_a": data.get("active_current_l2_a"),
            "l3_a": data.get("active_current_l3_a")
        }
    
    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
