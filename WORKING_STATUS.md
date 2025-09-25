# Werkende Status - 25 sept 2025 12:55

## ✅ WAT WERKT:
- Battery Power toont WERKELIJKE waarden (niet meer -1W)
- Force Charge/Discharge knoppen werken perfect
- Stop Force knop werkt
- RS485 Control Enable/Disable werkt
- Mode Cmd toont juiste commando's (Stop/Force Charge/Force Discharge)
- Huidige Actie toont juiste status (Standby/Charging/Discharging)
- Setpoint Charge/Discharge tonen ingestelde waarden

## ⚠️ PROBLEEM:
- Battery Power waarden zijn ~10x te hoog vergeleken met Marstek app
- Voorbeelden:
  - Marstek app: 0W → Dashboard: -166W
  - Marstek app: 1843W → Dashboard: 18303W  
  - Marstek app: 348W → Dashboard: -4245W

## 🔧 OPLOSSING:
- Register 32102 gebruikt nu scale: 0.08
- Moet waarschijnlijk scale: 0.008 of 0.01 zijn
- Backup bestanden gemaakt: venus_e_register_map_WORKING_BACKUP.py, app_WORKING_BACKUP.py

## ❌ NOG NIET WERKEND:
- Auto/Manual/Trade knoppen (werkmodus)
- Exacte scaling van Battery Power

## 📁 BACKUP BESTANDEN:
- venus_e_register_map_WORKING_BACKUP.py
- app_WORKING_BACKUP.py
- WORKING_STATUS.md (dit bestand)

## 🔄 TERUGZETTEN:
```bash
cp venus_e_register_map_WORKING_BACKUP.py venus_e_register_map.py
cp app_WORKING_BACKUP.py app.py
```
