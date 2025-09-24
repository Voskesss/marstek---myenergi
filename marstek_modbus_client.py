#!/usr/bin/env python3
"""
Marstek Venus E v2 Modbus Client
Communicates with Venus E batteries via RS-485 Modbus TCP
"""

import asyncio
import time
from typing import Dict, Any, Optional, List
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

class MarstekModbusClient:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.client = None
        self.connected = False
    
    def connect(self) -> bool:
        """Connect to Modbus TCP server"""
        try:
            self.client = ModbusTcpClient(self.host, port=self.port)
            self.connected = self.client.connect()
            if self.connected:
                print(f"✅ Connected to Modbus {self.host}:{self.port}")
            else:
                print(f"❌ Failed to connect to {self.host}:{self.port}")
            return self.connected
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from Modbus server"""
        if self.client:
            self.client.close()
            self.connected = False
            print("🔌 Disconnected from Modbus")
    
    def read_registers(self, address: int, count: int) -> Optional[List[int]]:
        """Read holding registers"""
        if not self.connected:
            return None
        
        try:
            result = self.client.read_holding_registers(address, count, unit=self.unit_id)
            if result.isError():
                print(f"❌ Modbus read error: {result}")
                return None
            return result.registers
        except Exception as e:
            print(f"❌ Read error: {e}")
            return None
    
    def write_register(self, address: int, value: int) -> bool:
        """Write single holding register"""
        if not self.connected:
            return False
        
        try:
            result = self.client.write_register(address, value, unit=self.unit_id)
            if result.isError():
                print(f"❌ Modbus write error: {result}")
                return False
            print(f"✅ Written register {address} = {value}")
            return True
        except Exception as e:
            print(f"❌ Write error: {e}")
            return False
    
    # Battery-specific methods (registers to be discovered)
    def get_battery_status(self) -> Optional[Dict[str, Any]]:
        """Get battery status - register addresses TBD"""
        # Common Modbus addresses for battery systems:
        # SoC: usually around 1000-1010
        # Voltage: 1020-1030  
        # Current: 1040-1050
        # Power: 1060-1070
        # Temperature: 1080-1090
        
        registers = self.read_registers(1000, 20)  # Read 20 registers from 1000
        if registers:
            return {
                "soc": registers[0] / 10.0 if len(registers) > 0 else None,  # Usually scaled
                "voltage": registers[5] / 100.0 if len(registers) > 5 else None,
                "current": registers[10] / 100.0 if len(registers) > 10 else None,
                "power": registers[15] if len(registers) > 15 else None,
                "raw_registers": registers
            }
        return None
    
    def set_charge_mode(self, mode: str) -> bool:
        """Set battery charge mode"""
        # Mode mapping (to be discovered):
        # 0 = Auto, 1 = Force Charge, 2 = Force Discharge, 3 = Idle
        mode_map = {
            "auto": 0,
            "charge": 1, 
            "discharge": 2,
            "idle": 3
        }
        
        if mode.lower() not in mode_map:
            print(f"❌ Invalid mode: {mode}")
            return False
        
        # Control register usually around 2000-2010
        return self.write_register(2000, mode_map[mode.lower()])
    
    def set_charge_power(self, power_w: int) -> bool:
        """Set charge/discharge power in watts"""
        # Power register usually around 2010-2020
        return self.write_register(2010, power_w)
    
    def discover_registers(self) -> Dict[str, Any]:
        """Discover available registers by scanning common ranges"""
        print("🔍 Discovering Modbus registers...")
        
        discovered = {}
        
        # Scan common register ranges
        ranges = [
            (1000, 50, "status"),      # Status registers
            (2000, 20, "control"),     # Control registers  
            (3000, 20, "config"),      # Configuration
            (4000, 20, "alarms")       # Alarms/errors
        ]
        
        for start, count, name in ranges:
            print(f"📡 Scanning {name} registers {start}-{start+count-1}")
            registers = self.read_registers(start, count)
            if registers:
                # Filter out zero/invalid values
                valid_regs = {i: val for i, val in enumerate(registers) if val != 0}
                if valid_regs:
                    discovered[name] = {
                        "start_address": start,
                        "registers": valid_regs
                    }
                    print(f"✅ Found {len(valid_regs)} non-zero {name} registers")
        
        return discovered

# Test function
async def test_modbus_client():
    """Test Modbus client with Venus E battery"""
    print("🔋 Testing Marstek Venus E Modbus Client")
    print("=" * 50)
    
    # Replace with your converter IP
    client = MarstekModbusClient("192.168.68.100", 502, 1)
    
    if not client.connect():
        print("❌ Cannot connect - check hardware setup")
        return
    
    try:
        # Discover registers first
        discovered = client.discover_registers()
        print(f"\n📊 Discovered registers: {discovered}")
        
        # Try to read battery status
        status = client.get_battery_status()
        if status:
            print(f"\n🔋 Battery Status: {status}")
        
        # Test control commands
        print(f"\n🎮 Testing control commands...")
        client.set_charge_mode("auto")
        time.sleep(1)
        client.set_charge_power(1000)
        
    finally:
        client.disconnect()

if __name__ == "__main__":
    asyncio.run(test_modbus_client())
