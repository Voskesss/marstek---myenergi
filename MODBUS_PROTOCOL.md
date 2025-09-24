# Marstek Venus E Modbus Protocol

This document contains the working Modbus communication settings for the Marstek Venus E battery, discovered through community information and testing.

## Connection Settings

- **Protocol:** Modbus TCP
- **IP Address:** IP of the RS485-to-WiFi converter (e.g., `192.168.68.92`)
- **Port:** `502`

## Serial (RS485) Settings

These settings must be configured in the RS485-to-WiFi converter's web interface.

- **Baud Rate:** `115200`
- **Data Bits:** `8`
- **Parity:** `None`
- **Stop Bits:** `1`

## Modbus Unit ID

- **Unit ID / Slave ID:** `1`

## Key Registers (Holding Registers)

### Manual Control

To manually control the battery, a specific sequence must be followed.

1.  **Enable Control Mode:**
    - Write `21930` (Hex: `0x55AA`) to register `42000`.

2.  **Set Power and Mode:**
    - **Charge Power:** Write desired power in Watts to register `42020`.
    - **Discharge Power:** Write desired power in Watts to register `42021`.
    - **Set Mode:** Write the desired mode to register `42010`:
        - `1`: Force Charge
        - `2`: Force Discharge
        - `0`: Stop forced operation

3.  **Disable Control Mode:**
    - To return the battery to its normal, self-initiated mode, write `21947` (Hex: `0x55BB`) to register `42000`.

### Reading Data

- **State of Charge (SoC %):** Register `32104` (Value is direct percentage, e.g., 630 = 63.0%)
- **Battery Voltage:** Register `32100`
- **Battery Current:** Register `32101`
- **Work Mode:** Register `35100` (Enum, see `venus_e_register_map.py`)
