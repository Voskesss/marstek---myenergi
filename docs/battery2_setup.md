# Battery 2 (WiFi Converter) - Quick Setup & Testing

This document describes how Battery 2 is integrated and how to test it.

## Topology
- Device: WiFi RS485 Modbus gateway connected to Battery 2
- IP: 192.168.68.74
- Port: 502 (TCP Server → Route: UART)
- Modbus Unit ID: 1 (assumed)
- Serial (RS485): 115200 baud, 8 data bits, 1 stop bit, Parity None, Half Duplex
- RS485 wiring: A(+) → A(+), B(-) → B(-), termination ON at both ends

## Backend (FastAPI)
- File: `app.py`
- Second Modbus client:
  - `venus_modbus2 = VenusEModbusClient(host=os.getenv('VENUS_MODBUS_HOST2', '192.168.68.74'))`
  - Lock: `modbus_lock2`
- Endpoint:
  - `GET /api/battery2/status`
    - Same payload structure as `/api/battery/status` for Battery 1
    - Derived fields: `soc_percent`, `power_w`, `mode`, `remaining_kwh`, etc.

## Dashboard
- File: `dashboard.html`
- Card: "Batterij 2 (WiFi Converter)" with read-only metrics
  - Fields: SoC, Remaining kWh, Power, Actie, IP, Port, Last Update
  - Next step: add JS updater to fetch `/api/battery2/status` every 5s

## Quick Tests
1) Start server
```
./start_production.sh
```
2) Health
```
curl http://localhost:8000/health
```
3) Battery 2 status
```
curl http://localhost:8000/api/battery2/status | jq '.'
```

## Troubleshooting
- If `success: false` or no data:
  - Verify RS485 A/B wiring and termination
  - Check serial settings (115200, 8N1, None)
  - Confirm Modbus Unit ID (usually 1)
  - Try direct Modbus TCP probe:
```
.venv/bin/python test_wifi_converter.py --ip 192.168.68.74 | jq '.'
```

## Notes
- Battery 1 remains on `192.168.68.92` (`venus_modbus`)
- Battery 2 uses `venus_modbus2` to avoid cross-talk and to keep locking simple
- Next phase: refactor to multi-battery architecture (registry + BatteryManager)
