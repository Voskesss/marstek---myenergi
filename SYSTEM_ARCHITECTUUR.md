# MyEnergi-Marstek Systeem Architectuur

## 📋 Overzicht

Deze applicatie integreert MyEnergi apparaten (Eddi/Zappi) met Marstek batterijen voor intelligent energie management met Eddi prioriteit.

## 🏗️ Hoofdcomponenten

### 1. FastAPI Backend (`app.py`)
- **Poort**: 8000
- **Framework**: FastAPI (async Python)
- **Logging**: Rotating file handler (`logs/app.log`)

### 2. Dashboard (`dashboard.html`)
- **Type**: Single-page web interface
- **Update**: Elke 5 seconden (auto-refresh)
- **Features**: Real-time energie flow, batterij status, weer voorspelling

### 3. MyEnergi Client
- **Protocol**: HTTPS REST API
- **Authenticatie**: API key + Hub serial
- **Data**: Grid power, PV generatie, Eddi/Zappi status
- **Endpoint**: `https://s18.myenergi.net/`

### 4. Marstek Batterij Clients

#### 4.1 Modbus TCP (Primair)
**Batterij 1**: `venus_ev2_92`
- **Host**: 192.168.68.92 (via VENUS_MODBUS_HOST env var)
- **Port**: 502
- **Protocol**: Modbus TCP
- **Registers**: 
  - SOC: Input register 769
  - Work Mode: Holding register 42001
  - Power: Berekend via voltage × current

**Batterij 2**: `venus_ev2_74`
- **Host**: 192.168.68.74 (via VENUS_MODBUS_HOST2 env var)
- **Port**: 502
- **Zelfde protocol als Batterij 1**

#### 4.2 BLE (Momenteel ongebruikt)
- **Functie**: Fallback/debugging
- **Status**: Kan verwijderd worden
- **Code**: `ble_client.py`

**Class**: `VenusEModbusClient` (`app.py`, regel ~135)
```python
def connect():
    client = ModbusTcpClient(host, port=502, timeout=2)
    connected = client.connect()

def read_battery_data():
    # SOC
    soc = read_input_registers(769, 2)
    # Voltage/Current
    voltage = read_input_registers(259, 2)  
    current = read_input_registers(261, 2)
    
def set_work_mode(mode):
    # 0 = Manual, 1 = Anti-Feed, 2 = Backup, 3 = Auto
    write_register(42001, mode)
    
def set_control(action, power_w):
    # action: "charge", "discharge", "stop"
    # Schrijft naar holding registers
```

## 🔄 Simple Battery Rule Engine

### Doel
Batterijen automatisch laden uit PV-overschot met **Eddi prioriteit**, en automatisch leveren aan huis bij geen PV.

### Configuratie (regel ~1838)
```python
{
    "buffer_w": 200,              # Grid export target
    "export_margin_w": 100,       # Extra buffer
    "threshold_start_w": 150,     # Start laden bij >150W overschot
    "threshold_stop_w": 100,      # Stop laden bij <100W overschot
    "ramp_step_w": 150,           # Stap grootte per cycle
    "loop_interval_s": 5.0,       # Check elke 5 seconden
    "cooldown_s": 8,              # Cooldown na stop
    "max_batt_total_w": 5000,     # Max totaal vermogen
    "per_battery_max_w": 2500,    # Max per batterij
    "pv_threshold_w": 50,         # Onder 50W = "geen PV"
    "import_threshold_w": 100,    # Boven 100W = "importeren"
}
```

### Logica Flow

#### A. Anti-Feed Mode (Avond/Nacht)
**Conditie**: `PV < 50W` EN `Grid Import > 100W`

**Actie**:
```python
1. Detecteer: geen zonne-energie + importeren uit net
2. Voor elke batterij:
   - Check huidige mode in state tracker
   - Als mode != "anti-feed":
     - Zet work_mode = 1 (Anti-Feed) via Modbus
     - Update state: battery_modes[bid] = "anti-feed"
   - Anders: skip (al goed)
3. Continue naar volgende cycle (5s later)
```

**Resultaat**: Batterijen leveren automatisch aan huis (zero feed-in)

#### B. Manual Mode + Laden (Dag)
**Conditie**: `Grid < 0` (exporteren) OF `PV > 50W`

**Actie**:
```python
1. Bereken overschot: max(0, -grid_w)
2. EMA smoothing: ema_overschot = 0.3 * raw + 0.7 * previous
3. Bereken error: ema_overschot - target_export
4. Bepaal target power:
   - Als error > threshold_start_w EN niet in cooldown:
     - Ramp up: target += ramp_step_w
   - Als error < -threshold_stop_w:
     - Ramp down: target -= ramp_step_w
     - Bij target=0: activeer cooldown
5. Verdeel over batterijen (met per-battery cap)
6. Voor elke batterij met power > 0:
   - Check huidige mode in state tracker
   - Als mode != "manual":
     - Zet work_mode = 0 (Manual) via Modbus
     - Update state: battery_modes[bid] = "manual"
   - Stuur laadcommando via _set_battery_power()
```

**Eddi Prioriteit**: Eddi krijgt altijd eerst, batterijen krijgen alleen resterende overschot

### State Tracking
```python
simple_rule.battery_modes = {
    "venus_ev2_92": "manual",    # Wat regel denkt dat mode is
    "venus_ev2_74": "anti-feed"
}
```

**Doel**: Voorkom onnodige mode switches
**Limitatie**: Weet niet van handmatige wijzigingen via dashboard

### Loop Cycle (elke 5s)
```
1. Fetch MyEnergi data (grid_w, pv_w)
2. Check conditie: PV + Import?
   ├─ Ja → Anti-Feed logica
   └─ Nee → Check export?
       ├─ Ja → Manual + Laden logica  
       └─ Nee → Stop laden + cooldown
3. Sleep 5 seconden
4. Herhaal
```

## 🌐 API Endpoints

### MyEnergi Status
- `GET /api/status` - Volledige systeem status
- Data: PV, Grid, Eddi, Zappi, Huis verbruik

### Batterij Control
- `GET /api/batteries` - Lijst batterijen
- `GET /api/batteries/{id}/status` - Batterij status (SOC, power, etc)
- `GET /api/batteries/{id}/config` - Batterij configuratie
- `POST /api/batteries/{id}/control` - Handmatig laden/ontladen
  ```json
  {"action": "charge|discharge|stop", "power_w": 1000}
  ```
- `POST /api/batteries/{id}/mode` - Work mode zetten
  ```json
  {"mode": 0}  // 0=Manual, 1=Anti-Feed, 2=Backup, 3=Auto
  ```

### Simple Rule Control
- `GET /api/simple_rule/status` - Regel status + laatste actie
- `POST /api/simple_rule/enable` - Activeer regel
- `POST /api/simple_rule/disable` - Deactiveer regel

### Weather (Nieuw)
- `GET /api/weather/current` - Huidig weer
- `GET /api/weather/forecast` - 24u voorspelling
- `GET /api/weather/solar` - PV-relevante forecast

## 🔌 Netwerk Topologie

```
┌─────────────────┐
│   MyEnergi Hub  │ (Cloud)
│   (s18 server)  │
└────────┬────────┘
         │ HTTPS
         │
┌────────▼────────┐     Modbus TCP
│  FastAPI Server │◄───────────────────┐
│   (Pi/Mac)      │                    │
│   Port 8000     │                    │
└────────┬────────┘                    │
         │ HTTP                        │
         │                             │
┌────────▼────────┐     ┌──────────────▼───────┐
│   Dashboard     │     │ Marstek Batterijen   │
│  (Browser)      │     │ 192.168.68.92 :502   │
└─────────────────┘     │ 192.168.68.74 :502   │
                        └──────────────────────┘
```

## 📁 Belangrijke Bestanden

### Core
- `app.py` - Hoofdapplicatie (3943 regels)
- `dashboard.html` - Web interface
- `.env` - Configuratie (API keys, hosts)
- `battery_config.json` - Batterij instellingen

### Clients
- `myenergi_client.py` - MyEnergi API client
- `marstek_client.py` - Marstek REST client (legacy)
- `ble_client.py` - Bluetooth client (**kan weg**)

### Nieuwe Features
- `weather.py` - OpenWeatherMap One Call API 3.0
- `WEATHER_SETUP.md` - Weather configuratie

### Deployment
- `start_production.sh` - Start script
- `requirements.txt` - Python dependencies

## 🧪 Test Scenario's

### Avond (Anti-Feed Test)
```bash
# Verwacht gedrag:
# - PV: 0-50W
# - Grid: +1000W tot +5000W (importeren)
# - Batterijen: Anti-Feed mode → leveren aan huis
# - Logs: "☀️ SIMPLE RULE: No PV + importing - Setting ANTI-FEED mode"
```

### Ochtend (Switch naar Manual)
```bash
# Verwacht gedrag:
# - PV: 500W+ (zon komt op)
# - Grid: -500W (beginnen exporteren)
# - Batterijen: Switch naar Manual mode
# - Begin laden met beperkt vermogen
# - Logs: "⚡ Battery switched to manual" + "🔋 Setting {bid} to Manual mode"
```

### Middag (Volledig Laden)
```bash
# Verwacht gedrag:
# - PV: 10000W+
# - Grid: -5000W (veel export)
# - Eddi: 3000W (warmwater)
# - Batterijen: ~2000W laden (na Eddi)
# - Ramp up tot max 5000W totaal
```

## 🔍 Debugging

### Logs Bekijken
```bash
# Realtime follow
tail -f logs/app.log

# Filter specifieke events
tail -f logs/app.log | grep "SIMPLE RULE\|⚡\|🔋"

# Laatste errors
tail -100 logs/app.log | grep ERROR
```

### Batterij Status Checken
```bash
# Via API
curl http://localhost:8000/api/batteries/venus_ev2_92/status | jq .

# Work mode bekijken
curl http://localhost:8000/api/simple_rule/status | jq '.last.per_battery'
```

### Handmatig Testen
```bash
# Zet anti-feed mode
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": 1}'

# Zet manual mode
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": 0}'

# Start laden (manual mode vereist)
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/control \
  -H "Content-Type: application/json" \
  -d '{"action": "charge", "power_w": 1000}'
```

## ⚠️ Bekende Issues / Limitations

1. **State Tracking**: Weet niet van handmatige mode wijzigingen
   - **Impact**: Minimaal - probeert gewoon opnieuw (idempotent)
   - **Fix**: Later - echte batterij status uitlezen

2. **BLE Client**: Ongebruikt, kan weg
   - **Actie**: Verwijderen in toekomstige cleanup

3. **Grid Data**: Soms negatieve PV waarde 's avonds
   - **Oorzaak**: MyEnergi API quirk
   - **Impact**: Geen - threshold >50W compenseert

4. **Mode Flipperen**: Was probleem, nu opgelost met state tracking

## 📝 TODO / Toekomstige Verbeteringen

- [ ] Override knop in dashboard (handmatige controle)
- [ ] Echte batterij work mode uitlezen (niet alleen state)
- [ ] BLE client verwijderen
- [ ] Pi5 deployment setup
- [ ] Systemd service configuratie
- [ ] Log rotation verbeteren
- [ ] Unit tests toevoegen
- [ ] Notificaties (Telegram?) voor belangrijke events

## 🚀 Deployment (Pi5)

**TODO**: Volgt later
- Systemd service file
- Auto-start on boot
- Log rotation config
- Watchdog voor crashes

---
**Laatst bijgewerkt**: 2025-10-04
**Branch**: `feature/anti-feed-mode`
