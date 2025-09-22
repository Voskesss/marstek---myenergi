# 🧪 Live Test Guide - myenergi ↔ Marstek

## Quick Start

**1. Start de test app:**
```bash
cd /Users/josklijnhout/myenergy-marstek
./start_test.sh
```

**2. Open het Live Dashboard:**
```
http://localhost:8000/dashboard
```

## Wat je ziet in het Dashboard

### 📊 Real-time Data
- **⚡ Grid Export** - Hoeveel stroom je exporteert naar net
- **🔥 Eddi** - Warmwater boiler status en temperaturen  
- **🚗 Zappi** - Auto laadpaal status (als je die hebt)
- **🔋 Batterij** - SoC, vermogen, en of hij mag laden

### 🧠 Beslissing Logic
- **Huidige status** - Batterij geblokkeerd of toegestaan
- **Reden** - Waarom deze beslissing is genomen
- **Cooldown** - Tijd tot volgende schakel mogelijk is

### 📋 Live Logs
- Real-time logging van alle beslissingen
- Timestamps van schakelingen
- Error meldingen

## Test Scenario's

### 🌤️ Normale Zonnige Dag
1. **Weinig zon (< 3kW export):**
   - Batterij: 🚫 GEBLOKKEERD
   - Reden: "Export 2000W < Eddi reserve 3000W"

2. **Matige zon (3-5kW export):**
   - Batterij: 🚫 GEBLOKKEERD  
   - Reden: "Export 4000W < battery minimum 5000W (Eddi buffer zone)"

3. **Veel zon (> 5kW export):**
   - Batterij: ✅ TOEGESTAAN
   - Reden: "Export 6000W ≥ 5000W (enough for both)"

### 🚗 Auto Laden (Zappi)
1. **Zappi actief:**
   - Batterij: 🚫 GEBLOKKEERD
   - Reden: "Zappi active: 3000W > 200W (auto charging priority)"

### 🔄 Anti-Toggle Test
1. **Batterij is AAN, export daalt naar 4800W:**
   - Batterij: ✅ BLIJFT AAN
   - Reden: Hysterese (4800W > 4500W uit-drempel)

2. **Batterij is UIT, export stijgt naar 5200W:**
   - Batterij: ✅ GAAT AAN
   - Reden: Boven aan-drempel (5200W > 5500W)

## Handmatige Tests

### 🎮 Manual Controls
```bash
# Batterij handmatig toestaan
curl -X POST http://localhost:8000/api/marstek/allow

# Batterij handmatig blokkeren  
curl -X POST http://localhost:8000/api/marstek/inhibit

# Status opvragen
curl http://localhost:8000/api/status | jq
```

### 🔍 BLE Tests
```bash
# BLE batterij status
curl http://localhost:8000/api/ble/status | jq

# BLE connectie info
curl http://localhost:8000/api/ble/info | jq

# BLE handmatig verbinden
curl -X POST http://localhost:8000/api/ble/connect
```

## Troubleshooting

### ❌ Batterij reageert niet
1. Check BLE verbinding: `/api/ble/status`
2. Test handmatige controle: `/api/marstek/allow`
3. Check logs in dashboard voor errors

### ❌ myenergi data ontbreekt
1. Check API credentials in .env
2. Test: `curl http://localhost:8000/api/status`
3. Kijk naar `myenergi_raw` field voor data

### ❌ Verkeerde beslissingen
1. Check configuratie in dashboard
2. Verifieer export waarden
3. Test verschillende scenario's handmatig

## Live Monitoring Tips

### 📈 Wat te monitoren
- **Export fluctuaties** - Hoe snel verandert het?
- **Schakel momenten** - Wanneer schakelt batterij?
- **Cooldown timing** - Voorkomt rapid switching?
- **BLE stabiliteit** - Blijft verbinding stabiel?

### 🎯 Success Criteria
- ✅ Eddi krijgt altijd eerste prioriteit
- ✅ Batterij schakelt niet te vaak (max 1x per minuut)
- ✅ Geen energie verspilling bij veel zon
- ✅ Auto laden gaat voor batterij
- ✅ Stabiele BLE verbinding

## Configuration Tweaks

Als je gedrag wilt aanpassen, edit deze waarden in `.env`:

```bash
# Drempels aanpassen
EDDI_RESERVE_W=3000          # Hoeveel voor Eddi reserveren
BATTERY_MIN_EXPORT_W=5000    # Minimum export voor batterij
BATTERY_HYSTERESIS_W=500     # Anti-toggle buffer

# Timing aanpassen  
MIN_SWITCH_COOLDOWN_S=60     # Minimum tijd tussen schakelingen
POLL_INTERVAL_S=2            # Hoe vaak checken

# Zappi prioriteit
ZAPPI_ACTIVE_W=200           # Wanneer is Zappi actief
ZAPPI_RESERVE_W=2000         # Reserve voor Zappi
```

## Real-world Testing

### 🌅 Ochtend Test
- Start vroeg (weinig zon)
- Monitor eerste Eddi activiteit
- Check batterij blijft uit

### ☀️ Middag Test  
- Monitor export stijging
- Check batterij gaat aan bij >5kW
- Test hysterese bij wolken

### 🌆 Avond Test
- Monitor export daling
- Check batterij gaat uit
- Test failsafe bij lage SoC

**Happy Testing! 🚀**
