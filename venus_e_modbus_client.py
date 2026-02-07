#!/usr/bin/env python3
"""
Venus E Modbus Client - Working version
Connects to Venus E battery via USR-DR164 converter
"""

from pymodbus.client import ModbusTcpClient
import time
import json

class VenusEModbusClient:
    def __init__(self, host='192.168.68.92', port=502):
        self.host = host
        self.port = port
        self.client = None
        self.connected = False
    
    def connect(self):
        """Connect to Modbus TCP server"""
        try:
            self.client = ModbusTcpClient(self.host, port=self.port)
            self.connected = self.client.connect()
            if self.connected:
                print(f"✅ Connected to Venus E at {self.host}:{self.port}")
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
            print("🔌 Disconnected from Venus E")
    
    def read_register(self, address, count=1, unit=1):
        """Read holding registers with connection recovery"""
        if not self.connected:
            if not self.connect():
                return None
        
        try:
            result = self.client.read_holding_registers(address=address, count=count, slave=unit)
            if result.isError():
                return None
            return result.registers
        except Exception as e:
            # Handle broken pipe and connection errors
            if "Broken pipe" in str(e) or "Connection" in str(e) or "Bad file descriptor" in str(e) or "timed out" in str(e):
                print(f"⚠️ Connection lost to {self.host}:{self.port} - attempting reconnect")
                self.disconnect()
                time.sleep(0.5)  # Brief pause before reconnect
                # Try to reconnect once
                if self.connect():
                    try:
                        result = self.client.read_holding_registers(address=address, count=count, slave=unit)
                        if not result.isError():
                            return result.registers
                    except:
                        pass
            
            print(f"❌ Read error at {address}: {e}")
            return None
    
    def write_register(self, address, value, unit=1):
        """Write single holding register with connection recovery"""
        if not self.connected:
            if not self.connect():
                return False
        
        try:
            result = self.client.write_register(address=address, value=value, slave=unit)
            if not result.isError():
                return True
            # If error, try reconnect
            print(f"⚠️ Write error at {address}, attempting reconnect")
            self.disconnect()
            if self.connect():
                result = self.client.write_register(address=address, value=value, slave=unit)
                return not result.isError()
            return False
        except Exception as e:
            # Handle broken pipe and connection errors
            if "Broken pipe" in str(e) or "Connection" in str(e) or "Bad file descriptor" in str(e):
                print(f"⚠️ Connection lost during write to {self.host}:{self.port} - attempting reconnect")
                self.disconnect()
                # Try to reconnect once
                if self.connect():
                    try:
                        result = self.client.write_register(address=address, value=value, slave=unit)
                        if not result.isError():
                            return True
                    except:
                        pass
            
            print(f"❌ Write error at {address}: {e}")
            return False
    
    def discover_registers(self):
        """Discover Venus E battery registers"""
        print("🔍 Discovering Venus E registers...")
        
        if not self.connected:
            print("❌ Not connected")
            return {}
        
        discovered = {}
        
        # Test common battery register ranges
        test_ranges = [
            (1000, 20, "Battery Status"),
            (1100, 20, "Battery Info"),
            (2000, 10, "Control"),
            (3000, 10, "Configuration"),
            (4000, 10, "Alarms"),
            (5000, 10, "Statistics")
        ]
        
        for start, count, name in test_ranges:
            print(f"📡 Testing {name} registers ({start}-{start+count-1})")
            
            found_values = {}
            for addr in range(start, start + count):
                values = self.read_register(addr, 1, unit=1)
                if values and values[0] != 0:
                    found_values[addr] = values[0]
                    print(f"   ✅ Register {addr}: {values[0]} (0x{values[0]:04X})")
            
            if found_values:
                discovered[name] = found_values
        
        return discovered
    
    def get_battery_status(self):
        """Get current battery status"""
        if not self.connected:
            return None
        
        # Common Venus E register addresses (estimated)
        status = {}
        
        register_map = {
            1001: ("soc_percent", "State of Charge %"),
            1002: ("voltage_v", "Battery Voltage V"),
            1003: ("current_a", "Battery Current A"),
            1004: ("power_w", "Battery Power W"),
            1005: ("temperature_c", "Temperature °C"),
            2001: ("charge_mode", "Charge Mode"),
            2002: ("max_charge_w", "Max Charge Power W"),
            2003: ("max_discharge_w", "Max Discharge Power W")
        }
        
        for addr, (key, desc) in register_map.items():
            values = self.read_register(addr, 1, unit=1)
            if values:
                status[key] = {
                    "value": values[0],
                    "description": desc,
                    "register": addr
                }
        
        return status
    
    def set_charge_mode(self, mode):
        """Set battery charge mode"""
        # Mode values (estimated for Venus E)
        modes = {
            "auto": 0,
            "force_charge": 1,
            "force_discharge": 2,
            "idle": 3
        }
        
        if mode.lower() not in modes:
            print(f"❌ Invalid mode: {mode}")
            return False
        
        mode_value = modes[mode.lower()]
        success = self.write_register(2001, mode_value, unit=1)
        
        if success:
            print(f"✅ Set charge mode to: {mode}")
        else:
            print(f"❌ Failed to set charge mode")
        
        return success

def main():
    """Test Venus E Modbus connection"""
    print("🚀 Venus E Modbus Client Test")
    print("=" * 40)
    
    # Create client
    venus = VenusEModbusClient('192.168.68.92', 502)
    
    # Connect
    if not venus.connect():
        print("❌ Cannot connect to Venus E")
        return
    
    try:
        # Discover registers
        discovered = venus.discover_registers()
        
        if discovered:
            print(f"\n🎉 Found battery data in {len(discovered)} register ranges!")
            
            # Get battery status
            print("\n📊 Current Battery Status:")
            status = venus.get_battery_status()
            
            if status:
                for key, data in status.items():
                    if data["value"] != 0:
                        print(f"   {data['description']}: {data['value']}")
            else:
                print("   ⚠️ No status data available")
        else:
            print("\n⚠️ No battery registers found")
            print("   Battery may be sleeping or using different addresses")
    
    finally:
        venus.disconnect()

if __name__ == "__main__":
    main()
