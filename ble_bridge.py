#!/usr/bin/env python3
"""
BLE Bridge Service for Marstek Venus E batteries.
Provides HTTP API endpoints backed by BLE communication.
Fallback solution when network Local API is not available.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Any
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from bleak import BleakClient, BleakScanner
import struct

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MarstekBLEClient:
    """BLE client for Marstek Venus E battery communication"""
    
    def __init__(self, device_name: str = "MST_ACCP_3159"):
        self.device_name = device_name
        self.device_address: Optional[str] = None
        self.client: Optional[BleakClient] = None
        self.is_connected = False
        
        # BLE characteristics (from our working implementation)
        self.WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
        self.NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
        
        # Response cache with timestamps
        self.cache: Dict[str, Dict] = {}
        self.cache_ttl = 30  # seconds
        
    async def discover_device(self) -> bool:
        """Discover Marstek device via BLE scan"""
        logger.info(f"Scanning for {self.device_name}...")
        
        try:
            devices = await BleakScanner.discover(timeout=10.0)
            for device in devices:
                if device.name and self.device_name in device.name:
                    self.device_address = device.address
                    logger.info(f"Found device: {device.name} at {device.address}")
                    return True
            
            logger.warning(f"Device {self.device_name} not found")
            return False
            
        except Exception as e:
            logger.error(f"BLE scan error: {e}")
            return False
    
    async def connect(self) -> bool:
        """Connect to Marstek device"""
        if not self.device_address:
            if not await self.discover_device():
                return False
        
        try:
            self.client = BleakClient(self.device_address)
            await self.client.connect()
            self.is_connected = True
            logger.info(f"Connected to {self.device_address}")
            return True
            
        except Exception as e:
            logger.error(f"BLE connect error: {e}")
            self.is_connected = False
            return False
    
    async def disconnect(self):
        """Disconnect from device"""
        if self.client and self.is_connected:
            try:
                await self.client.disconnect()
                self.is_connected = False
                logger.info("Disconnected from device")
            except Exception as e:
                logger.error(f"Disconnect error: {e}")
    
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
        if not self.is_connected:
            if not await self.connect():
                return None
        
        try:
            frame = self._build_hm_frame(cmd, payload)
            logger.debug(f"Sending: {frame.hex()}")
            
            # Send command
            await self.client.write_gatt_char(self.WRITE_CHAR, frame)
            
            # Wait for response (simplified - in real implementation we'd use notifications)
            await asyncio.sleep(0.5)
            
            # Read response
            response = await self.client.read_gatt_char(self.NOTIFY_CHAR)
            logger.debug(f"Response: {response.hex()}")
            
            return response
            
        except Exception as e:
            logger.error(f"Command error: {e}")
            self.is_connected = False
            return None
    
    def _parse_battery_status(self, data: bytes) -> Dict[str, Any]:
        """Parse battery status from BLE response"""
        # Simplified parser - adapt based on actual protocol
        try:
            if len(data) < 10:
                return {}
            
            # Example parsing (adjust based on actual data format)
            soc = data[5] if len(data) > 5 else 0
            voltage = struct.unpack('<H', data[6:8])[0] / 100.0 if len(data) > 7 else 0
            current = struct.unpack('<h', data[8:10])[0] / 100.0 if len(data) > 9 else 0
            power = voltage * current
            
            return {
                "soc": soc,
                "voltage": voltage,
                "current": current, 
                "power": power,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return {}
    
    async def get_battery_status(self) -> Dict[str, Any]:
        """Get battery status via BLE"""
        cache_key = "battery_status"
        
        # Check cache
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached.get("_cache_time", 0) < self.cache_ttl:
                return cached
        
        # Get fresh data
        response = await self._send_command(0x03)  # Battery status command
        if response:
            status = self._parse_battery_status(response)
            if status:
                status["_cache_time"] = time.time()
                self.cache[cache_key] = status
                return status
        
        return {"error": "Failed to get battery status", "timestamp": datetime.now().isoformat()}
    
    async def get_system_info(self) -> Dict[str, Any]:
        """Get system information via BLE"""
        cache_key = "system_info"
        
        # Check cache
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached.get("_cache_time", 0) < self.cache_ttl * 2:  # Longer cache for system info
                return cached
        
        # Get fresh data
        response = await self._send_command(0x01)  # System info command
        if response:
            info = {
                "device_name": self.device_name,
                "ble_address": self.device_address,
                "connected": self.is_connected,
                "firmware": "v6.0",  # From your screenshot
                "timestamp": datetime.now().isoformat(),
                "_cache_time": time.time()
            }
            self.cache[cache_key] = info
            return info
        
        return {"error": "Failed to get system info", "timestamp": datetime.now().isoformat()}

# Global BLE client instance
ble_client = MarstekBLEClient()

# FastAPI app
app = FastAPI(
    title="Marstek BLE Bridge",
    description="HTTP API bridge for Marstek Venus E battery via BLE",
    version="1.0.0"
)

@app.on_event("startup")
async def startup_event():
    """Initialize BLE connection on startup"""
    logger.info("Starting Marstek BLE Bridge...")
    await ble_client.discover_device()

@app.on_event("shutdown") 
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down BLE Bridge...")
    await ble_client.disconnect()

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "service": "Marstek BLE Bridge",
        "status": "running",
        "connected": ble_client.is_connected,
        "device": ble_client.device_name,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/battery/status")
async def get_battery_status():
    """Get battery status (SoC, power, etc.)"""
    try:
        status = await ble_client.get_battery_status()
        return JSONResponse(content=status)
    except Exception as e:
        logger.error(f"Battery status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/system/info")
async def get_system_info():
    """Get system information"""
    try:
        info = await ble_client.get_system_info()
        return JSONResponse(content=info)
    except Exception as e:
        logger.error(f"System info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/battery/connect")
async def connect_battery():
    """Manually trigger BLE connection"""
    try:
        success = await ble_client.connect()
        return {"success": success, "connected": ble_client.is_connected}
    except Exception as e:
        logger.error(f"Connect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/battery/disconnect")
async def disconnect_battery():
    """Manually disconnect BLE"""
    try:
        await ble_client.disconnect()
        return {"success": True, "connected": ble_client.is_connected}
    except Exception as e:
        logger.error(f"Disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cache/clear")
async def clear_cache():
    """Clear response cache"""
    ble_client.cache.clear()
    return {"success": True, "message": "Cache cleared"}

if __name__ == "__main__":
    print("🔋 Starting Marstek BLE Bridge Service...")
    print("📡 Endpoints:")
    print("   GET  /                     - Health check")
    print("   GET  /api/battery/status   - Battery status (SoC, power)")
    print("   GET  /api/system/info      - System information")
    print("   POST /api/battery/connect  - Connect to battery")
    print("   POST /api/battery/disconnect - Disconnect")
    print("   GET  /api/cache/clear      - Clear cache")
    print()
    print("🌐 Starting server on http://localhost:8001")
    
    uvicorn.run(
        "ble_bridge:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
        log_level="info"
    )
