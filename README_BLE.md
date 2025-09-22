# BLE Integration voor Marstek Venus E

## Overzicht

De myenergi-marstek app heeft nu **geïntegreerde BLE ondersteuning** als fallback voor wanneer de netwerk Local API niet werkt.

## Wat is er gemaakt

### 1. Geïntegreerde BLE Client (`ble_client.py`)
- Communiceert direct via BLE met Marstek batterij
- Automatische device discovery en connection management
- Response caching voor betere performance
- Foutafhandeling en reconnection logic

### 2. Updated Main App (`app.py`)
- `MARSTEK_USE_BLE=true` → gebruikt BLE i.p.v. netwerk API
- Automatische fallback naar BLE als netwerk faalt
- Nieuwe BLE endpoints: `/api/ble/status`, `/api/ble/info`
- Startup/shutdown lifecycle management

### 3. Test Scripts
- `test_integrated.py` - Test BLE functionaliteit
- `start_integrated.sh` - Eenvoudige startup

## Setup

### 1. Bluetooth aanzetten
```bash
# macOS: Ga naar System Preferences > Bluetooth en zet aan
# Of via command line:
sudo blueutil -p 1
```

### 2. Installeer BLE ondersteuning
```bash
cd /Users/josklijnhout/myenergy-marstek
source .venv/bin/activate
pip install bleak
```

### 3. Test BLE verbinding
```bash
python test_integrated.py
```

### 4. Start app met BLE
```bash
# Optie 1: Via script
./start_integrated.sh

# Optie 2: Handmatig
export MARSTEK_USE_BLE=true
uvicorn app:app --port 8000
```

## Gebruik

### Endpoints

**BLE Status:**
```bash
curl http://localhost:8000/api/ble/status
```

**BLE Info:**
```bash
curl http://localhost:8000/api/ble/info
```

**Handmatige BLE connectie:**
```bash
curl -X POST http://localhost:8000/api/ble/connect
```

**Hoofdstatus (gebruikt BLE als MARSTEK_USE_BLE=true):**
```bash
curl http://localhost:8000/api/status
```

### Web Interface

- **Hoofdapp:** http://localhost:8000
- **BLE UI:** http://localhost:8000/ble/ (bestaande BLE interface)
- **Status:** http://localhost:8000/api/status

## Configuratie

### Environment Variables

```bash
# BLE mode aanzetten
export MARSTEK_USE_BLE=true

# Andere instellingen blijven hetzelfde
export MYENERGI_BASE_URL="https://s18.myenergi.net"
export MYENERGI_HUB_SERIAL="Z12345678"
export MYENERGI_API_KEY="your_api_key"
```

### .env File

```
MARSTEK_USE_BLE=true
MYENERGI_BASE_URL=https://s18.myenergi.net
MYENERGI_HUB_SERIAL=Z12345678
MYENERGI_API_KEY=your_api_key
```

## Voordelen

✅ **Eén proces** - Geen aparte BLE bridge service nodig
✅ **Automatische fallback** - Werkt als netwerk API faalt  
✅ **Bestaande functionaliteit** - Alle myenergi integratie blijft werken
✅ **Firmware v6.0 compatible** - Werkt met jouw huidige firmware
✅ **Uitbreidbaar** - Later meerdere batterijen via meerdere instances

## Troubleshooting

### "Bluetooth device is turned off"
```bash
# macOS Bluetooth aanzetten
sudo blueutil -p 1
# Of via System Preferences > Bluetooth
```

### "Device MST_ACCP_3159 not found"
- Zorg dat batterij aan staat en binnen 10m bereik
- Check of batterij niet verbonden is met andere device
- Probeer batterij herstart

### "BLE not available"
```bash
pip install bleak
```

### Permissions op macOS
- Ga naar System Preferences > Security & Privacy > Privacy
- Geef Terminal/IDE toegang tot Bluetooth

## Architectuur

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   myenergi      │    │   FastAPI App    │    │   Marstek       │
│   (Cloud/Local) │◄──►│                  │◄──►│   (BLE)         │
│                 │    │  ble_client.py   │    │   Venus E       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │
                              ▼
                       ┌──────────────────┐
                       │   Control Logic  │
                       │   (Automatic)    │
                       └──────────────────┘
```

## Volgende Stappen

1. **Test met Bluetooth aan** - Zet Bluetooth aan en test opnieuw
2. **Productie deployment** - Start met `./start_integrated.sh`
3. **Monitoring** - Check `/api/ble/status` voor BLE verbinding
4. **Meerdere batterijen** - Later uitbreiden met meerdere BLE clients

## Fallback Strategie

Als Marstek netwerk API alsnog werkt (na firmware update):
1. Zet `MARSTEK_USE_BLE=false`
2. Configureer `MARSTEK_BASE_URL=http://192.168.68.66:30000`
3. App gebruikt automatisch netwerk API i.p.v. BLE

Zo heb je beide opties beschikbaar! 🎉
