# Volledige Functie Analyse - MyEnergi Marstek Applicatie

**Datum:** 8 oktober 2025, 22:49  
**Doel:** Complete review van alle functies en logica voor optimalisatie

---

## 🎯 CORE SYSTEEM ARCHITECTUUR

### Batterijen
- **Battery 1 (venus_ev2_92):** `192.168.68.92:502` (Modbus)
- **Battery 2 (venus_ev2_74):** `192.168.68.74:502` (Modbus/WiFi converter)

### Communicatie
- **MyEnergi:** Cloud API (Digest Auth) → Eddi, Zappi, Harvi data
- **Batterijen:** Modbus TCP → Venus E registers
- **Dashboard:** FastAPI backend → Real-time status

---

## ⚙️ 1. SIMPLE RULE ENGINE (Hoofdlogica)

**Status:** ✅ Auto-start bij app boot  
**Locatie:** `_simple_rule_loop()` regel ~1962-2236

### Functie
Export-driven batterij controle met dynamische power setpoints.

### Belangrijkste Logica

#### A. **Anti-Feed Mode** (Geen zon + importeren)
```python
Conditie: PV < 50W AND Grid Import > 100W
Actie: Switch batterijen naar Anti-Feed mode (work_mode = 1)
- Batterij ontlaadt automatisch om import te minimaliseren
- SOC check: Stopt als SOC <= min_soc_percent
```

**Issues:**
- ⚠️ Anti-Feed blijft soms actief na zonsopgang
- ⚠️ Mode wordt te vaak geschakeld (elke 5s)

#### B. **Surplus Charging** (Zon + export)
```python
Conditie: Grid Export > buffer_w + export_margin_w
Berekening:
  overschot = max(0, -grid_w)
  target_export = buffer_w (200W) + export_margin_w (100W) = 300W
  error = overschot - target_export
  
Ramping:
  Als error > 150W → ramp up (step 150W per 5s)
  Als error < -100W → ramp down (step 150W per 5s)
  Max: 5000W totaal, 2500W per batterij
```

**Issues:**
- ⚠️ Te snel ramping (150W elke 5s = 1800W/min)
- ⚠️ Geen hysteresis bij mode switching

#### C. **SOC Checking** (Min SOC bescherming)
```python
Per batterij check:
  Als current_soc <= min_soc_percent:
    - BLOCK anti-feed mode
    - Switch naar Manual mode (stop discharge)
```

**Issues:**
- ⚠️ Modbus timeout blokkeert hele loop
- ⚠️ Bij Modbus error stopt Simple Rule alle batterijen (te defensief)

### Config Parameters
```json
{
  "buffer_w": 200,              // Grid reserve buffer
  "export_margin_w": 100,       // Extra margin voor stabiliteit
  "threshold_start_w": 150,     // Start charging threshold
  "threshold_stop_w": 100,      // Stop charging threshold
  "ramp_step_w": 150,           // Power ramp step size
  "loop_interval_s": 5.0,       // Loop frequency
  "cooldown_s": 8,              // Cooldown na stop
  "max_batt_total_w": 5000,     // Max totaal vermogen
  "per_battery_max_w": 2500,    // Max per batterij
  "pv_threshold_w": 50,         // Min PV voor "zon"
  "import_threshold_w": 100     // Min import voor anti-feed
}
```

### Endpoints
- `POST /api/simple_rule/enable` - Start Simple Rule
- `POST /api/simple_rule/disable` - Stop Simple Rule
- `GET /api/simple_rule/status` - Status + laatste waarden

---

## 🛡️ 2. SOC SAFETY MONITOR (Altijd actief)

**Status:** ✅ Always running (onafhankelijk van Simple Rule)  
**Locatie:** `_soc_safety_monitor()` regel ~2283-2342

### Functie
Emergency stop als batterij onder min SOC ontlaadt.

### Logica
```python
Check elke 10 seconden:
  Voor elke batterij:
    Als current_soc <= min_soc AND battery_power < -50W:
      → Emergency stop: Switch naar Manual mode (0)
      → Log warning
```

**Status:** ✅ Werkt goed, maar heeft betere Modbus error handling nodig

---

## 🔋 3. MINIMUM SOC FUNCTIONALITEIT

**Locatie:** Meerdere endpoints + config file

### A. **Config Management**
**File:** `battery_config.json`
```json
{
  "venus_ev2_92": {
    "minimum_soc_percent": 19.0,
    "auto_charge_enabled": true
  },
  "venus_ev2_74": {
    "minimum_soc_percent": 19.0,
    "auto_charge_enabled": true
  }
}
```

### B. **Check & Enforce Endpoints**

#### Per Battery
`POST /api/batteries/{bid}/minimum_soc`
```python
Payload: { min_soc_percent: 19.0, auto_charge: true }

Als auto_charge = true:
  - Check current SOC
  - Als te laag: Start emergency charge (500W)
  - Hysteresis: Stop pas bij min_soc + 2%

Als auto_charge = false:
  - Alleen check, geen actie
```

**Status:** ✅ Werkt, maar wordt NIET gebruikt door Simple Rule

### C. **VenusEModbusClient.check_minimum_soc()**
**Locatie:** Regel ~363-414

```python
def check_minimum_soc(min_soc: float, hysteresis: float = 2.0):
    current_soc = read_battery_data()["soc_percent"]
    
    Als current_soc <= min_soc:
        → set_control("charge", 500W)  # Emergency charge
    
    Als current_soc >= min_soc + hysteresis:
        → set_control("stop")  # Stop emergency charge
    
    Als in hysteresis zone:
        → Geen actie (anti-toggle)
```

**Issues:**
- ⚠️ Wordt NIET gebruikt door Simple Rule (heeft eigen SOC check)
- ⚠️ Twee verschillende implementaties (verwarring)

---

## 📊 4. OUDE REGELLOGICA (Legacy, niet actief)

### A. **Control Loop** (Regel ~1714-1786)
**Status:** ❌ NIET ACTIEF (Simple Rule vervangt dit)

```python
Eddi Priority Logic:
  - Threshold mode: Export-based with hysteresis
  - Power mode: Eddi active → block battery
  - Temp mode: Tank temp < target → block battery
```

**Actie:** Kan verwijderd worden (dead code)

### B. **Eddi Priority Helpers**
**Locatie:** `should_block_battery_for_priority()` regel ~1011-1088

```python
Modes:
  - "threshold": Min export check (5000W) + hysteresis
  - "power": Eddi > 200W → block battery
  - "temp": Tank temp < target → block battery
```

**Status:** Legacy, maar functies worden nog gebruikt voor `/api/status` berekeningen

---

## 🌐 5. API ENDPOINTS OVERZICHT

### Status & Monitoring
| Endpoint | Functie | Status |
|----------|---------|--------|
| `GET /api/status` | Combined MyEnergi + Battery status | ✅ Werkt |
| `GET /api/health` | Simple Rule health check | ✅ Werkt |
| `GET /api/logs/tail` | Recent log entries | ✅ Werkt |
| `GET /dashboard` | Live dashboard HTML | ✅ Werkt |
| `GET /flow.html` | Energy flow visualization | ✅ Werkt |

### Battery Control (Per Battery)
| Endpoint | Functie | Status |
|----------|---------|--------|
| `GET /api/batteries` | List all batteries | ✅ Werkt |
| `GET /api/batteries/{bid}/status` | Battery status by ID | ✅ Werkt |
| `POST /api/batteries/{bid}/control` | Control battery (charge/discharge/stop) | ✅ Werkt |
| `POST /api/batteries/{bid}/mode` | Set work mode (0/1/2/3) | ✅ Werkt |
| `POST /api/batteries/{bid}/minimum_soc` | Set min SOC limit | ✅ Werkt |
| `GET /api/batteries/{bid}/config` | Get battery config | ✅ Werkt |

### Simple Rule Control
| Endpoint | Functie | Status |
|----------|---------|--------|
| `POST /api/simple_rule/enable` | Start Simple Rule | ✅ Werkt |
| `POST /api/simple_rule/disable` | Stop Simple Rule | ✅ Werkt |
| `GET /api/simple_rule/status` | Get Simple Rule status | ✅ Werkt |

### Weather & WhatsApp
| Endpoint | Functie | Status |
|----------|---------|--------|
| `GET /api/weather/current` | Current weather | ✅ Werkt |
| `GET /api/weather/forecast` | 24h forecast | ✅ Werkt |
| `POST /api/whatsapp/test` | Test WhatsApp | ✅ Werkt |
| `POST /api/whatsapp/tip/now` | Manual energy tip | ✅ Werkt (je cursor is hier) |

### Diagnostics
| Endpoint | Functie | Status |
|----------|---------|--------|
| `GET /api/battery/ping` | Modbus connectivity test | ✅ Werkt |
| `GET /api/battery/scan` | Scan Modbus registers | ✅ Werkt |
| `GET /api/battery/read_many` | Read multiple registers | ✅ Werkt |
| `POST /api/battery/diagnostics/work_mode` | Work mode diagnostics | ✅ Werkt |

---

## 📱 6. WHATSAPP NOTIFICATIES

**Status:** ✅ Auto-start bij app boot  
**Locatie:** `_whatsapp_tips_scheduler()` regel ~2349-2448

### Functie
Automatische energy tips op 09:00 en 13:00.

### Logica
```python
Scheduler:
  - Check elke minuut
  - Send tip om 09:00 en 13:00
  - Track sent_today (1x per tijdstip per dag)

Data gathering:
  - Weather (clouds, temp)
  - PV generation
  - Grid import/export
  - Battery SOC (avg van alle batterijen)
  - House consumption
  - Eddi power
  - Overschot berekening
```

**Status:** ✅ Werkt goed

---

## 🌤️ 7. WEATHER SERVICE

**File:** `weather.py`  
**Status:** ✅ Werkt

### Endpoints
- `GET /api/weather/current` - Huidige weersdata
- `GET /api/weather/forecast` - 24h forecast
- `GET /api/weather/solar` - Solar-specific forecast

**Gebruik:** WhatsApp tips, toekomstige smart charging

---

## 🔧 8. MODBUS CONTROL FUNCTIES

### A. **VenusEModbusClient.set_control()**
**Locatie:** Regel ~416-488

```python
Actions: "charge", "discharge", "stop"

Registers:
  - 42000: Control enable (0x55AA) / disable (0x55BB)
  - 42010: Mode (0=Stop, 1=Charge, 2=Discharge)
  - 42020: Charge power (W)
  - 42021: Discharge power (W)

Proces:
  1. Enable control (0x55AA)
  2. Set power + mode
  3. (Voor stop: Disable control 0x55BB)
```

**Status:** ✅ Werkt betrouwbaar

### B. **VenusEModbusClient.set_work_mode()**
**Locatie:** Regel ~286-361

```python
Modes:
  - 0: Manual (app controleert niet)
  - 1: Anti-Feed (batterij regelt zelf)
  - 2: Trade Mode
  - 3: Backup Mode

Register: 43000 (User Work Mode)

Proces:
  1. Enable RS485 control (42000 = 0x55AA)
  2. Write work mode (43000 = mode)
  3. Disable RS485 control (42000 = 0x55BB)
```

**Status:** ✅ Werkt, maar veel mode switches kunnen instabiliteit veroorzaken

---

## 📈 9. DATA EXTRACTION FUNCTIES

### MyEnergi Data Parsing

#### `extract_grid_export_w()` - Regel ~816-844
```python
Conventie: Positief = Export, Negatief = Import
Bronnen: Zappi.grd, Eddi.grd, top-level.pgrid
```

#### `extract_eddi_power_w()` - Regel ~846-868
```python
Bronnen: Eddi.ectp1 (heater power), Eddi.div (total diverter)
```

#### `extract_zappi_power_w()` - Regel ~870-892
```python
Bronnen: Zappi.div (charge power), Zappi.che (charge energy)
```

#### `extract_house_consumption_w()` - Regel ~894-950
```python
Formule:
  house = PV + Grid - Eddi - Zappi - Battery

Batterij conventie:
  +ve = laden (verbruikt)
  -ve = ontladen (levert)
```

**Status:** ✅ Werkt correct met batterij component

#### `extract_pv_generation_w()` - Regel ~952-978
```python
Bron: Harvi CT clamps met type "Generation"
```

#### `extract_eddi_temperatures()` - Regel ~980-1009
```python
Bron: Eddi.tp1, Eddi.tp2 (tank temps in °C)
```

### Simple Rule Data Extraction

#### `_extract_grid_from_raw()` - Regel ~1898-1926
```python
Prioriteit:
  1. Zappi CT ectp4+ectp5+ectp6 (meest accuraat)
  2. Eddi.grd
  3. Top-level.grd
```

**Status:** ✅ Gebruikt CT clamps voor betere precisie

---

## 🐛 10. BEKENDE ISSUES & PROBLEMEN

### Kritiek (Moet opgelost)
1. **Anti-Feed blijft te lang actief**
   - Symptom: Na zonsopgang blijft anti-feed mode actief
   - Oorzaak: Simple Rule switch terug naar Manual mode faalt soms
   - Fix: Betere mode state tracking + retry logic

2. **Modbus timeouts blokkeren Simple Rule**
   - Symptom: Bij batterij disconnect stopt Simple Rule volledig
   - Oorzaak: Blocking Modbus read in async loop
   - Fix: `asyncio.wait_for()` met timeout + continue bij error

3. **Te agressieve ramping**
   - Symptom: Batterij power schiet te snel omhoog
   - Oorzaak: 150W elke 5s = 1800W/min
   - Fix: Langzamere ramp (50W per 5s) of langere interval

4. **Mode switching te frequent**
   - Symptom: Batterij schakelt elke 5-10s tussen modes
   - Oorzaak: Geen hysteresis bij mode decision
   - Fix: Min 60s tussen mode switches (al geïmplementeerd maar niet effectief)

### Medium (Kan beter)
5. **Dubbele SOC implementatie**
   - Simple Rule heeft eigen SOC check
   - `check_minimum_soc()` wordt niet gebruikt
   - Fix: Consolideer naar 1 implementatie

6. **Legacy code cleanup**
   - `control_loop()` is dead code
   - Oude regellogica helpers nog aanwezig
   - Fix: Verwijder ongebruikte functies

7. **Config reload niet overal**
   - Battery config reload werkt, maar niet voor Simple Rule config
   - Fix: Hot reload voor alle config files

### Low (Nice to have)
8. **Dashboard caching**
   - No-cache headers aanwezig maar soms nog delays
   - Fix: WebSocket voor real-time updates

9. **Logging te verbose**
   - Debug logs blijven actief in productie
   - Fix: Log level configureerbaar maken

10. **Error recovery**
    - Bij Modbus error stopt alles (te defensief)
    - Fix: Continue met defaults, log warning

---

## ✅ 11. WAT WERKT GOED

### Sterk
1. **Simple Rule surplus charging** - Effectief bij export
2. **SOC Safety Monitor** - Voorkomt over-discharge
3. **Multi-battery support** - Beide batterijen werken parallel
4. **Dashboard visualisatie** - Duidelijk real-time overzicht
5. **WhatsApp notificaties** - Betrouwbare tips 2x per dag
6. **Modbus reliability** - set_control() werkt consistent
7. **API structure** - Goede separation per battery
8. **Auto-restart** - Simple Rule start automatisch bij boot

### Acceptabel
9. **Mode switching** - Werkt maar te frequent
10. **Anti-feed detection** - Logica klopt, timing niet
11. **Energy calculations** - Formules correct, maar edge cases
12. **Weather integration** - Data klopt, gebruik kan beter

---

## 🎯 12. AANBEVOLEN WIJZIGINGEN

### Prioriteit 1 (Deze week)
1. **Fix Anti-Feed stuck issue**
   - Add mode state tracking
   - Add forced mode check elke 60s
   - Better error handling bij mode switch failure

2. **Improve Modbus timeout handling**
   - Wrap ALL Modbus reads in `asyncio.wait_for()`
   - Continue loop bij timeout (don't stop everything)
   - Log errors maar crash niet

3. **Reduce mode switching frequency**
   - Add min 120s hysteresis tussen mode changes
   - State machine voor cleaner transitions
   - Track "time in mode" voor debugging

### Prioriteit 2 (Deze maand)
4. **Consolideer SOC checking**
   - Verwijder `check_minimum_soc()` Modbus functie
   - Simple Rule is single source of truth
   - Update docs

5. **Optimize ramping**
   - Reduce step size naar 50W
   - Add max ramp rate (500W/min)
   - Smooth setpoint changes

6. **Cleanup legacy code**
   - Verwijder `control_loop()`
   - Verwijder oude Eddi priority helpers
   - Update README

### Prioriteit 3 (Toekomst)
7. **Smart PV forecasting**
   - Gebruik weather forecast voor pre-charging
   - Morning ramp-up prediction
   - SOC planning based on forecast

8. **Advanced rules engine**
   - Time-based rules (cheap/expensive hours)
   - Multi-rule priority system
   - User-friendly rule builder

9. **WebSocket dashboard**
   - Real-time updates zonder polling
   - Grafana-style metrics
   - Historical data charts

---

## 📝 13. CONFIG FILES OVERZICHT

### battery_config.json
```json
{
  "venus_ev2_92": {
    "minimum_soc_percent": 19.0,
    "auto_charge_enabled": true
  },
  "venus_ev2_74": {
    "minimum_soc_percent": 19.0,
    "auto_charge_enabled": true
  }
}
```

### energy_rules_config.json
```json
{
  "boiler_priority": {
    "enabled": true,
    "tank": "tank2",
    "min_temp": 36
  },
  "surplus_charging": {
    "enabled": true,
    "min_export_w": 100
  },
  "emergency_charge": {
    "enabled": true,
    "power_w": 2000
  },
  "smart_control": {
    "enabled": true,
    "check_interval_seconds": 10,
    "step_size_w": 200,
    "min_eddi_power_for_scaling": 3000,
    "hysteresis_seconds": 120
  }
}
```

**Status:** ⚠️ Wordt NIET gebruikt door Simple Rule (legacy)

---

## 🔍 14. DEBUG TIPS

### Check Simple Rule Status
```bash
curl http://localhost:8000/api/simple_rule/status
```

### Check Battery Status
```bash
curl http://localhost:8000/api/batteries/venus_ev2_92/status
curl http://localhost:8000/api/batteries/venus_ev2_74/status
```

### Check Logs
```bash
curl http://localhost:8000/api/logs/tail?n=100
# Of direct:
tail -f logs/app.log
```

### Manual Control Tests
```bash
# Stop all batteries
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/control \
  -H "Content-Type: application/json" \
  -d '{"action":"stop"}'

# Charge at 500W
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/control \
  -H "Content-Type: application/json" \
  -d '{"action":"charge","power_w":500}'

# Set work mode to Anti-Feed
curl -X POST http://localhost:8000/api/batteries/venus_ev2_92/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":1}'
```

### Modbus Register Scan
```bash
# Scan holding registers
curl "http://localhost:8000/api/battery/scan?start=42000&count=50&kind=holding"

# Read specific registers
curl "http://localhost:8000/api/battery/read_many?addrs=42000,42010,42020,43000"
```

---

## 🎬 15. STARTUP SEQUENCE

1. **App Boot** (`@app.on_event("startup")`)
   - Start SOC Safety Monitor (altijd actief)
   - Start WhatsApp Tips Scheduler (9:00 & 13:00)
   - Start Simple Rule Engine (auto-enable)

2. **Simple Rule Init**
   - Load battery config (min SOC limits)
   - Reset state (prev_set_total = 0)
   - Start loop met 5s interval

3. **First Cycle**
   - Fetch MyEnergi data (grid, PV, Eddi, Zappi)
   - Read battery SOC (beide batterijen)
   - Check Anti-Feed conditie
   - Calculate setpoints
   - Apply to batteries

---

## 💭 CONCLUSIE & ACTIE

### Wat Goed Werkt ✅
- Simple Rule surplus charging
- Multi-battery coordination
- Modbus control reliability
- Dashboard visualisatie
- WhatsApp notificaties

### Wat Moet Beter ⚠️
- Anti-Feed mode switching (stuck issue)
- Modbus timeout handling (blokkeert loop)
- Mode hysteresis (te frequent)
- Ramping snelheid (te agressief)
- Code cleanup (legacy removal)

### Volgende Stappen Morgen
1. Review dit document samen
2. Prioriteer issues (top 3)
3. Test scenario's opstellen
4. Implementeer fixes stap-voor-stap
5. Monitor gedrag overdag met zon

---

**Document Einde**  
Slaap lekker! 🌙
