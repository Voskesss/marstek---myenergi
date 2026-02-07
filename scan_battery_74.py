#!/usr/bin/env python3
"""Scan battery 74 for SOC register"""
from pymodbus.client import ModbusTcpClient
import time

HOST = "192.168.68.74"
client = ModbusTcpClient(HOST, port=502, timeout=2)

if not client.connect():
    print("❌ Cannot connect")
    exit(1)

print(f"✅ Connected to {HOST}")
print("\n🔍 Scanning for SOC-like values (expecting ~23%)...\n")

# Common SOC register locations
test_regs = [
    0, 1, 2, 3, 4, 5,  # Very low addresses
    1000, 1001, 1002, 1003, 1004, 1005,  # Common range
    2300, 2301, 2302,  # Could be 23.00 encoded
    30000, 30001, 30002, 30003, 30004, 30005,  # Venus E range
    32100, 32101, 32102, 32103, 32104, 32105,  # Current range
    40000, 40001, 40002,  # Alternative range
]

found = []

for reg in test_regs:
    try:
        result = client.read_holding_registers(address=reg, count=1, slave=1)
        if hasattr(result, 'registers') and not result.isError():
            val = result.registers[0]
            # Look for values that could be SOC
            if val in [23, 230, 2300, 0x17]:  # 23 in different encodings
                found.append((reg, val))
                print(f"✅ Register {reg}: {val} (0x{val:04X}) - POSSIBLE MATCH!")
            elif 20 <= val <= 30:  # Close to 23
                found.append((reg, val))
                print(f"⭐ Register {reg}: {val} (0x{val:04X}) - CLOSE MATCH!")
            elif val > 0 and val < 10000:
                # Show other interesting values
                if val / 10 == 23 or val / 100 == 23:
                    print(f"🔸 Register {reg}: {val} (÷10={val/10}, ÷100={val/100})")
    except:
        pass
    time.sleep(0.05)

print(f"\n📊 Found {len(found)} potential SOC registers:")
for reg, val in found:
    print(f"   Register {reg} = {val}")

client.close()
