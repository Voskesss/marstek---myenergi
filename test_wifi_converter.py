#!/usr/bin/env python3
"""
Quick WiFi Converter connectivity test to IP from battery_config.json.
- Tries TCP connect on port 502
- Scans a set of unit IDs (1..10, 0, 247)
- Reads a small set of holding and input registers
Prints JSON to stdout.
"""
import json
import time
import argparse
from typing import Any, Dict

from pymodbus.client import ModbusTcpClient

CONFIG_PATH = "battery_config.json"


def load_ip() -> str:
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        return cfg.get("wifi_converter", {}).get("ip_address", "192.168.68.74")
    except Exception:
        return "192.168.68.74"


def main():
    parser = argparse.ArgumentParser(description="Test WiFi Modbus converter")
    parser.add_argument("--ip", help="Override IP address", default=None)
    args = parser.parse_args()

    ip = args.ip or load_ip()
    port = 502
    out: Dict[str, Any] = {
        "ip": ip,
        "port": port,
        "ts": time.time(),
        "connect": None,
        "unit_scan": [],
        "sample_reads": {},
        "error": None,
    }

    try:
        client = ModbusTcpClient(ip, port=port, timeout=3)
        out["connect"] = bool(client.connect())
        if not out["connect"]:
            print(json.dumps(out))
            return

        unit_ids = [1,2,3,4,5,6,7,8,9,10, 0, 247]
        for unit_id in unit_ids:
            try:
                rr = client.read_holding_registers(0, 1, unit=unit_id)
                ok = (hasattr(rr, "isError") and not rr.isError())
                entry = {"unit_id": unit_id, "ok_hold_0": ok}

                try:
                    rr_in = client.read_input_registers(0, 1, unit=unit_id)
                    entry["ok_input_0"] = (hasattr(rr_in, "isError") and not rr_in.isError())
                except Exception:
                    entry["ok_input_0"] = False

                reads = {"holding": {}, "input": {}}
                for addr in [0, 1, 1000, 30000, 32100, 32101, 32102, 35100, 42000, 43000]:
                    try:
                        r2 = client.read_holding_registers(addr, 1, unit=unit_id)
                        if hasattr(r2, "isError") and not r2.isError():
                            reads["holding"][str(addr)] = r2.registers[0]
                    except Exception:
                        pass
                    try:
                        r3 = client.read_input_registers(addr, 1, unit=unit_id)
                        if hasattr(r3, "isError") and not r3.isError():
                            reads["input"][str(addr)] = r3.registers[0]
                    except Exception:
                        pass

                entry["has_data"] = bool(reads["holding"] or reads["input"])
                out["unit_scan"].append(entry)
                if entry["has_data"]:
                    out["sample_reads"][str(unit_id)] = reads
            except Exception:
                out["unit_scan"].append({"unit_id": unit_id, "ok_hold_0": False, "ok_input_0": False, "has_data": False})
        client.close()
    except Exception as e:
        out["error"] = str(e)

    print(json.dumps(out))


if __name__ == "__main__":
    main()
