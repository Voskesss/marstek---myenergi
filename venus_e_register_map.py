#!/usr/bin/env python3
"""
Venus E v2 Complete Register Mapping
Based on Home Assistant integration and our Modbus scan
"""

# Complete Venus E v2 Register Map
VENUS_E_REGISTERS = {
    # Preferred registers (v2 firmware)
    32104: {
        "name": "soc_percent",
        "description": "Battery State of Charge",
        "unit": "%",
        "scale": 1.0,
        "type": "sensor"
    },
    32100: {
        "name": "battery_voltage",
        "description": "Battery Voltage",
        "unit": "V",
        "scale": 0.1,  # raw 571 -> 57.1 V
        "type": "sensor"
    },
    32101: {
        "name": "battery_current",
        "description": "Battery Current",
        "unit": "A",
        "scale": 0.01,
        "signed": True,
        "type": "sensor"
    },
    32102: {
        "name": "battery_power",
        "description": "Battery Power",
        "unit": "W",
        "scale": 1,
        "signed": True,
        "type": "sensor"
    },
    35100: {
        "name": "work_mode",
        "description": "Current Work Mode",
        "unit": "",
        "scale": 1,
        "type": "sensor",
        "values": {
            0: "Standby",
            1: "Standby",
            2: "Charging",
            3: "Discharging",
            4: "Fault",
            5: "Idle",
            6: "Self-Regulating"
        }
    },

    # Legacy registers (v1 firmware) — kept for reference
    30000: {"name": "legacy_soc_percent", "description": "Legacy SoC", "unit": "%", "scale": 0.5, "type": "sensor"},
    30001: {"name": "legacy_battery_voltage", "description": "Legacy Voltage", "unit": "V", "scale": 0.01, "type": "sensor"},
    30002: {"name": "legacy_battery_current", "description": "Legacy Current", "unit": "A", "scale": 0.01, "signed": True, "type": "sensor"},
    30003: {"name": "legacy_battery_power", "description": "Legacy Power", "unit": "W", "scale": 1, "signed": True, "type": "sensor"},
    30005: {"name": "legacy_work_mode", "description": "Legacy Work Mode", "unit": "", "scale": 1, "type": "sensor"},
    30010: {"name": "internal_temp", "description": "Internal Temperature", "unit": "°C", "scale": 0.1, "type": "sensor"},
}

# Control Registers (Read/Write) - 42000 range  
VENUS_E_CONTROLS = {
    42000: {
        "name": "rs485_control_enable",
        "description": "RS485 Control Mode Enable",
        "unit": "",
        "scale": 1,
        "type": "control",
        # Some firmwares use simple 0/1, others expect magic tokens 0x55AA/0x55BB
        "values": {0: "Disable", 1: "Enable", 21930: "Enable", 21947: "Disable"}
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
    },
    # Alternative/extended control set used by some firmwares
    42010: {
        "name": "control_mode_command",
        "description": "Control Command (0=Stop,1=Charge,2=Discharge)",
        "unit": "",
        "scale": 1,
        "type": "control",
        "values": {0: "Stop", 1: "Force Charge", 2: "Force Discharge"}
    },
    42020: {
        "name": "charge_setpoint_power",
        "description": "Charge Setpoint Power",
        "unit": "W",
        "scale": 1,
        "type": "control",
        "min": 0,
        "max": 2500
    },
    42021: {
        "name": "discharge_setpoint_power",
        "description": "Discharge Setpoint Power",
        "unit": "W",
        "scale": 1,
        "type": "control",
        "min": 0,
        "max": 2500
    }
}

def get_register_info(address: int) -> dict:
    """Get register information by address"""
    if address in VENUS_E_REGISTERS:
        return VENUS_E_REGISTERS[address]
    elif address in VENUS_E_CONTROLS:
        return VENUS_E_CONTROLS[address]
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
    
    # Format with max 1 decimal place
    if "values" in reg_info and raw_value in reg_info["values"]:
        # Use enumerated value
        formatted = reg_info["values"][raw_value]
    elif reg_info["unit"]:
        # Format number with max 1 decimal
        if isinstance(scaled_value, float):
            formatted = f"{scaled_value:.1f} {reg_info['unit']}"
        else:
            formatted = f"{scaled_value} {reg_info['unit']}"
    else:
        # No unit, just format number
        if isinstance(scaled_value, float):
            formatted = f"{scaled_value:.1f}"
        else:
            formatted = str(scaled_value)
    
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
