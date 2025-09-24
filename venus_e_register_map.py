#!/usr/bin/env python3
"""
Venus E v2 Complete Register Mapping
Based on Home Assistant integration and our Modbus scan
"""

# Complete Venus E v2 Register Map
VENUS_E_REGISTERS = {
    # Status Registers (Read-only) - 30000 range
    30000: {
        "name": "soc_percent",
        "description": "Battery State of Charge",
        "unit": "%", 
        "scale": 0.68,  # 144 raw = 98% actual
        "type": "sensor"
    },
    30001: {
        "name": "battery_voltage", 
        "description": "Battery Voltage",
        "unit": "V",
        "scale": 0.01,  # 5375 = 53.75V
        "type": "sensor"
    },
    30002: {
        "name": "battery_current",
        "description": "Battery Current", 
        "unit": "A",
        "scale": 0.01,
        "signed": True,  # Can be negative (discharge)
        "type": "sensor"
    },
    30003: {
        "name": "battery_power",
        "description": "Battery Power",
        "unit": "W", 
        "scale": 1,
        "signed": True,
        "type": "sensor"
    },
    30004: {
        "name": "ac_power",
        "description": "AC Power",
        "unit": "W",
        "scale": 1,
        "type": "sensor"
    },
    30005: {
        "name": "work_mode",
        "description": "Current Work Mode",
        "unit": "",
        "scale": 1,
        "type": "sensor",
        "values": {
            0: "Standby",
            1: "Charge", 
            2: "Discharge",
            3: "Backup",
            4: "Fault"
        }
    },
    30006: {
        "name": "system_status",
        "description": "System Status",
        "unit": "",
        "scale": 1, 
        "type": "sensor"
    },
    30008: {
        "name": "cycle_count",
        "description": "Battery Cycle Count",
        "unit": "cycles",
        "scale": 1,
        "type": "sensor"
    },
    30009: {
        "name": "capacity_ah", 
        "description": "Battery Capacity",
        "unit": "Ah",
        "scale": 1,
        "type": "sensor"
    },
    30010: {
        "name": "internal_temp",
        "description": "Internal Temperature", 
        "unit": "°C",
        "scale": 0.1,  # 257 = 25.7°C
        "type": "sensor"
    },
    
    # Additional status registers (estimated addresses)
    30011: {
        "name": "ac_voltage",
        "description": "AC Voltage",
        "unit": "V",
        "scale": 0.1,
        "type": "sensor"
    },
    30012: {
        "name": "ac_current", 
        "description": "AC Current",
        "unit": "A",
        "scale": 0.01,
        "type": "sensor"
    },
    30013: {
        "name": "mosfet_temp_1",
        "description": "MOSFET Temperature 1",
        "unit": "°C", 
        "scale": 0.1,
        "type": "sensor"
    },
    30014: {
        "name": "mosfet_temp_2",
        "description": "MOSFET Temperature 2", 
        "unit": "°C",
        "scale": 0.1,
        "type": "sensor"
    },
    30015: {
        "name": "protection_status",
        "description": "Protection Status Flags",
        "unit": "",
        "scale": 1,
        "type": "sensor"
    }
}

# Control Registers (Read/Write) - 42000 range  
VENUS_E_CONTROLS = {
    42000: {
        "name": "rs485_control_enable",
        "description": "RS485 Control Mode Enable",
        "unit": "",
        "scale": 1,
        "type": "control",
        "values": {0: "Disable", 1: "Enable"}
    },
    42001: {
        "name": "user_work_mode",
        "description": "User Work Mode",
        "unit": "",
        "scale": 1, 
        "type": "control",
        "values": {
            0: "Auto",
            1: "Manual", 
            2: "Trade Mode",
            3: "Backup Mode"
        }
    },
    42002: {
        "name": "force_charge_power",
        "description": "Force Charge Power",
        "unit": "W",
        "scale": 1,
        "type": "control",
        "min": 0,
        "max": 2500
    },
    42003: {
        "name": "force_discharge_power", 
        "description": "Force Discharge Power",
        "unit": "W",
        "scale": 1,
        "type": "control", 
        "min": 0,
        "max": 2500
    },
    42004: {
        "name": "max_charge_power",
        "description": "Maximum Charge Power",
        "unit": "W",
        "scale": 1,
        "type": "control",
        "min": 0, 
        "max": 2500
    },
    42005: {
        "name": "max_discharge_power",
        "description": "Maximum Discharge Power", 
        "unit": "W",
        "scale": 1,
        "type": "control",
        "min": 0,
        "max": 2500
    },
    42006: {
        "name": "charge_percentage",
        "description": "Target Charge Percentage",
        "unit": "%",
        "scale": 1,
        "type": "control",
        "min": 0,
        "max": 100
    },
    42007: {
        "name": "backup_function",
        "description": "Backup Function Enable",
        "unit": "",
        "scale": 1,
        "type": "control", 
        "values": {0: "Disable", 1: "Enable"}
    },
    42008: {
        "name": "force_charge_discharge",
        "description": "Force Charge/Discharge Command",
        "unit": "",
        "scale": 1,
        "type": "control",
        "values": {
            0: "Stop",
            1: "Force Charge", 
            2: "Force Discharge"
        }
    }
}

def get_register_info(address: int) -> dict:
    """Get register information by address"""
    if address in VENUS_E_REGISTERS:
        return VENUS_E_REGISTERS[address]
    elif address in VENUS_E_CONTROLS:
        return VENUS_E_CONTROLS[address]
    else:
        return None

def get_all_sensors() -> dict:
    """Get all sensor registers"""
    return {addr: info for addr, info in VENUS_E_REGISTERS.items() if info["type"] == "sensor"}

def get_all_controls() -> dict:
    """Get all control registers"""
    return VENUS_E_CONTROLS

def format_value(address: int, raw_value: int) -> dict:
    """Format raw Modbus value according to register definition"""
    reg_info = get_register_info(address)
    
    if not reg_info:
        return {"value": raw_value, "formatted": str(raw_value)}
    
    # Handle signed values
    if reg_info.get("signed", False) and raw_value > 32767:
        raw_value = raw_value - 65536
    
    # Apply scaling
    scaled_value = raw_value * reg_info["scale"]
    
    # Format with unit
    if reg_info["unit"]:
        formatted = f"{scaled_value} {reg_info['unit']}"
    else:
        formatted = str(scaled_value)
    
    # Handle enumerated values
    if "values" in reg_info and raw_value in reg_info["values"]:
        formatted = reg_info["values"][raw_value]
    
    return {
        "value": scaled_value,
        "raw": raw_value,
        "formatted": formatted,
        "description": reg_info["description"],
        "unit": reg_info["unit"]
    }

if __name__ == "__main__":
    print("🔋 Venus E v2 Register Map")
    print("=" * 40)
    
    print("\\n📊 Sensor Registers:")
    for addr, info in get_all_sensors().items():
        print(f"  {addr}: {info['description']} ({info['unit']})")
    
    print("\\n🎮 Control Registers:")  
    for addr, info in get_all_controls().items():
        print(f"  {addr}: {info['description']} ({info['unit']})")
