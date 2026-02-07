#!/usr/bin/env python3
"""Debug SOC reading for battery 74"""

from pymodbus.client import ModbusTcpClient
import json

# Battery 74
HOST = "192.168.68.74"
PORT = 502

print(f"🔍 Connecting to battery at {HOST}:{PORT}")
client = ModbusTcpClient(HOST, port=PORT, timeout=3)

if not client.connect():
    print(f"❌ Failed to connect")
    exit(1)

print("✅ Connected!")

# Try different slave/unit IDs and SOC registers
test_configs = [
    {"reg": 32104, "slave": 1, "desc": "SOC @ 32104, slave 1"},
    {"reg": 32104, "slave": 2, "desc": "SOC @ 32104, slave 2"},
    {"reg": 32104, "slave": 3, "desc": "SOC @ 32104, slave 3"},
    {"reg": 1001, "slave": 1, "desc": "SOC @ 1001, slave 1"},
    {"reg": 0, "slave": 1, "desc": "SOC @ 0, slave 1"},
]

print("\n" + "="*60)
for cfg in test_configs:
    try:
        result = client.read_holding_registers(
            address=cfg["reg"], 
            count=1, 
            slave=cfg["slave"]
        )
        
        if hasattr(result, 'registers') and not result.isError():
            raw = result.registers[0]
            print(f"\n✅ {cfg['desc']}")
            print(f"   Raw value: {raw}")
            print(f"   As %: {raw}%")
            print(f"   Divided by 10: {raw/10}%")
            print(f"   Divided by 100: {raw/100}%")
        else:
            print(f"\n❌ {cfg['desc']} - Error or no data")
    except Exception as e:
        print(f"\n❌ {cfg['desc']} - Exception: {e}")

print("\n" + "="*60)
client.close()
print("\n🔌 Disconnected")
