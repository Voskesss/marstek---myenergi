#!/usr/bin/env python3
"""
BLE Client module for Marstek Venus E batteries.
Integrated into main FastAPI app.
"""
import asyncio
import logging
import struct
import time
from datetime import datetime
from typing import Dict, Optional, Any
from bleak import BleakClient, BleakScanner

logger = logging.getLogger(__name__)

class MarstekBLEClient:
    """BLE client for Marstek Venus E battery communication"""
    
    def __init__(self, device_name: str = "MST_ACCP"):
        self.device_name = device_name
        self.device_address: Optional[str] = None
        self.client: Optional[BleakClient] = None
        self.is_connected = False
        
        # BLE characteristics
        self.WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
        self.NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
        
        # Response cache with timestamps
        self.cache: Dict[str, Dict] = {}
        self.cache_ttl = 30  # seconds
        
        # Connection management
        self._connection_lock = asyncio.Lock()
        self._last_activity = 0
        
    async def discover_device(self) -> bool:
        """Discover Marstek device via BLE scan"""
        logger.info(f"Scanning for Marstek devices...")
        
        try:
            devices = await BleakScanner.discover(timeout=15.0)
            
            # Look specifically for ACCP devices (batteries), not SMR (P1 meter)
            marstek_keywords = ['ACCP', 'MST_ACCP', 'MST-ACCP']
            
            for device in devices:
                if device.name:
                    name_upper = device.name.upper()
                    if any(keyword in name_upper for keyword in marstek_keywords):
                        self.device_address = device.address
                        self.device_name = device.name  # Update to actual name
                        logger.info(f"Found Marstek device: {device.name} at {device.address}")
                        return True
            
            logger.warning(f"No Marstek devices found. Scanned {len(devices)} devices.")
            return False
            
        except Exception as e:
            logger.error(f"BLE scan error: {e}")
            return False
    
    async def connect(self) -> bool:
        """Connect to Marstek device with connection management"""
        async with self._connection_lock:
            # Always try fresh discovery and connection for BLE reliability
            try:
                # Clean up any existing connection
                if self.client:
                    try:
                        await self.client.disconnect()
                    except:
                        pass
                    self.client = None
                    self.is_connected = False
                
                # Fresh discovery each time (BLE addresses can change)
                if not await self.discover_device():
                    return False
                
                # Create new connection
                self.client = BleakClient(self.device_address)
                await self.client.connect()
                self.is_connected = True
                self._last_activity = time.time()
                logger.info(f"Connected to {self.device_name} at {self.device_address}")
                return True
                
            except Exception as e:
                logger.error(f"BLE connect error: {e}")
                self.is_connected = False
                self.client = None
                # Clear address to force fresh discovery next time
                self.device_address = None
                return False
    
    async def disconnect(self):
        """Disconnect from device"""
        async with self._connection_lock:
            if self.client and self.is_connected:
                try:
                    await self.client.disconnect()
                    logger.info("Disconnected from device")
                except Exception as e:
                    logger.error(f"Disconnect error: {e}")
                finally:
                    self.is_connected = False
                    self.client = None
    
    def _calculate_checksum(self, data: bytes) -> int:
        """Calculate HM protocol checksum"""
        return sum(data) & 0xFF
    
    def _build_hm_frame(self, cmd: int, payload: bytes = b'') -> bytes:
        """Build HM protocol frame"""
        length = 3 + len(payload)  # 0x23 + cmd + payload
        frame = struct.pack('BB', 0x73, length)
        frame += struct.pack('BB', 0x23, cmd)
        frame += payload
        checksum = self._calculate_checksum(frame[2:])  # Checksum from 0x23 onwards
        frame += struct.pack('B', checksum)
        return frame
    
    async def _send_command(self, cmd: int, payload: bytes = b'') -> Optional[bytes]:
        """Send BLE command and wait for response"""
        if not await self.connect():
            return None
        
        try:
            frame = self._build_hm_frame(cmd, payload)
            logger.debug(f"Sending: {frame.hex()}")
            
            # Send command
            await self.client.write_gatt_char(self.WRITE_CHAR, frame)
            
            # Wait for response
            await asyncio.sleep(0.5)
            
            # Read response
            response = await self.client.read_gatt_char(self.NOTIFY_CHAR)
            logger.debug(f"Response: {response.hex()}")
            
            self._last_activity = time.time()
            return response
            
        except Exception as e:
            logger.error(f"Command error: {e}")
            self.is_connected = False
            return None
    
    def _parse_battery_status(self, data: bytes) -> Dict[str, Any]:
        """Parse battery status from BLE response"""
        try:
            if len(data) < 10:
                return {}
            
            # Parse based on actual protocol (simplified version)
            # You may need to adjust this based on real data format
            soc = data[5] if len(data) > 5 else 0
            
            # Try to extract voltage and current if available
            voltage = 0
            current = 0
            if len(data) > 7:
                try:
                    voltage = struct.unpack('<H', data[6:8])[0] / 100.0
                except:
                    voltage = 48.0  # Default battery voltage
            
            if len(data) > 9:
                try:
                    current = struct.unpack('<h', data[8:10])[0] / 100.0
                except:
                    current = 0
            
            power = voltage * current
            
            return {
                "soc": soc,
                "voltage": voltage,
                "current": current, 
                "power": power,
                "connected": True,
                "source": "ble",
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return {
                "soc": 0,
                "voltage": 0,
                "current": 0,
                "power": 0,
                "connected": False,
                "error": str(e),
                "source": "ble",
                "timestamp": datetime.now().isoformat()
            }
    
    async def get_battery_status(self) -> Dict[str, Any]:
        """Get battery status via BLE"""
        cache_key = "battery_status"
        
        # Check cache first
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached.get("_cache_time", 0) < self.cache_ttl:
                return cached
        
        # Get fresh data
        try:
            response = await self._send_command(0x03)  # Battery status command
            if response:
                status = self._parse_battery_status(response)
                if status:
                    status["_cache_time"] = time.time()
                    self.cache[cache_key] = status
                    return status
        except Exception as e:
            logger.error(f"Battery status error: {e}")
        
        # Return error status
        return {
            "soc": 0,
            "voltage": 0,
            "current": 0,
            "power": 0,
            "connected": False,
            "error": "Failed to get battery status",
            "source": "ble",
            "timestamp": datetime.now().isoformat()
        }
    
    async def get_system_info(self) -> Dict[str, Any]:
        """Get system information via BLE"""
        return {
            "device_name": self.device_name,
            "ble_address": self.device_address,
            "connected": self.is_connected,
            "firmware": "v6.0",
            "last_activity": self._last_activity,
            "source": "ble",
            "timestamp": datetime.now().isoformat()
        }
    
    def clear_cache(self):
        """Clear response cache"""
        self.cache.clear()

# Global BLE client instance
_ble_client: Optional[MarstekBLEClient] = None

def get_ble_client() -> MarstekBLEClient:
    """Get or create global BLE client instance"""
    global _ble_client
    if _ble_client is None:
        _ble_client = MarstekBLEClient()
    return _ble_client

async def cleanup_ble_client():
    """Cleanup BLE client on app shutdown"""
    global _ble_client
    if _ble_client:
        await _ble_client.disconnect()
        _ble_client = None
