# Frank Energie Integratie Plan

## 📋 Doel
Frank Energie API integreren voor dynamische tarieven (2025+, na afschaffing saldering).
Batterijen slim laden/ontladen op basis van stroomprijzen.

---

## 💡 Concept

```
Frank Energie API → Lage prijzen detecteren
                  → Batterij laden (ook uit grid!)
                  → Hoge prijzen → Ontladen naar huis
                  → Bespaar tot €120/maand!
```

---

## 🔗 API Referentie

**GitHub:** https://github.com/bajansen/home-assistant-frank_energie

**Endpoints:**
- Prijzen vandaag/morgen
- Gas prijzen
- Maandverbruik
- Marketing data

---

## 🔧 Implementatie Plan

### **Fase 1: Frank API Client (1-2 uur)**

```python
# frank_energie.py
class FrankEnergieClient:
    def __init__(self, auth_token: str):
        self.base_url = "https://frank-energy-api.com"
        self.auth_token = auth_token
    
    async def get_prices_today(self) -> List[PricePoint]:
        """Haal elektriciteitsprijzen op voor vandaag"""
        # GET /prices/today
        return prices
    
    async def get_prices_tomorrow(self) -> List[PricePoint]:
        """Haal prijzen voor morgen (vanaf ~15:00 beschikbaar)"""
        # GET /prices/tomorrow
        return prices
    
    async def get_cheapest_hours(self, n: int = 4) -> List[int]:
        """Vind N goedkoopste uren van vandaag"""
        prices = await self.get_prices_today()
        sorted_prices = sorted(prices, key=lambda x: x.price)
        return [p.hour for p in sorted_prices[:n]]
    
    async def get_current_price(self) -> float:
        """Huidige prijs per kWh"""
        prices = await self.get_prices_today()
        current_hour = datetime.now().hour
        return next(p.price for p in prices if p.hour == current_hour)
```

**Data Model:**
```python
@dataclass
class PricePoint:
    hour: int           # 0-23
    price: float        # EUR per kWh (incl BTW)
    timestamp: datetime
```

---

### **Fase 2: Price-Based Charging Rule (2-3 uur)**

```python
# In app.py
class PriceBasedRule:
    def __init__(self):
        self.frank_client = FrankEnergieClient(auth_token=...)
        self.config = {
            "cheap_threshold": 0.15,    # < €0.15 = laden uit grid
            "expensive_threshold": 0.30, # > €0.30 = ontladen naar huis
            "max_charge_power_w": 5000,  # Max 5kW uit grid
            "respect_soc_limits": True,   # 35-90% range
            "eddi_priority": True,        # PV overschot eerst naar Eddi
        }
    
    async def run(self):
        """Main loop - check elke 5 minuten"""
        while True:
            try:
                current_price = await self.frank_client.get_current_price()
                current_hour = datetime.now().hour
                
                # Check if in cheap hours
                cheap_hours = await self.frank_client.get_cheapest_hours(4)
                
                # Decision logic
                if current_hour in cheap_hours and current_price < self.config["cheap_threshold"]:
                    await self._charge_from_grid()
                elif current_price > self.config["expensive_threshold"]:
                    await self._discharge_to_house()
                else:
                    await self._idle_or_pv_mode()
                
                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"Price rule error: {e}")
                await asyncio.sleep(60)
    
    async def _charge_from_grid(self):
        """Laad batterijen uit grid (goedkope stroom)"""
        # Check SOC < 90%
        # Set batteries to charge mode
        # Limit power to max_charge_power_w
        logger.info(f"💶 Charging from grid (cheap rate)")
    
    async def _discharge_to_house(self):
        """Ontlaad batterijen naar huis (dure stroom vermijden)"""
        # Check SOC > 35%
        # Set batteries to anti-feed mode
        logger.info(f"⚡ Discharging to house (expensive rate)")
    
    async def _idle_or_pv_mode(self):
        """Normale modus - PV overschot + Simple Rule"""
        # Let Simple Rule handle PV overschot
        logger.debug(f"⏸️ Idle/PV mode")
```

**Prioriteit Hiërarchie:**
1. **SOC Safety** (altijd!) - 35-90% range
2. **Eddi Priority** - PV overschot eerst naar Eddi
3. **Frank Prices** - Laden bij goedkoop, ontladen bij duur
4. **Simple Rule** - Fallback voor PV management

---

### **Fase 3: Dashboard Widget (1 uur)**

```html
<!-- dashboard.html -->
<div class="card frank-prices-card">
    <h3>💶 Frank Energie Prijzen</h3>
    
    <div class="price-now">
        <div class="metric-label">Huidige prijs</div>
        <div class="metric-value" id="frankPriceNow">€0.25/kWh</div>
    </div>
    
    <div class="price-forecast">
        <h4>Vandaag</h4>
        <div id="frankPriceChart">
            <!-- Bar chart: goedkoopste uren groen, duurste rood -->
        </div>
    </div>
    
    <div class="price-strategy">
        <div class="metric">
            <span class="metric-label">⬇️ Goedkoopste uur</span>
            <span class="metric-value">02:00 (€0.08)</span>
        </div>
        <div class="metric">
            <span class="metric-label">⬆️ Duurste uur</span>
            <span class="metric-value">18:00 (€0.42)</span>
        </div>
        <div class="metric">
            <span class="metric-label">🔋 Actie</span>
            <span class="metric-value status-good">Laden @ 02:00</span>
        </div>
    </div>
    
    <div class="price-savings">
        <span class="metric-label">💰 Vandaag bespaard</span>
        <span class="metric-value">€4.20</span>
    </div>
</div>
```

**API Endpoints:**
```
GET /api/frank/prices/current    → Huidige prijs
GET /api/frank/prices/today      → Alle prijzen vandaag
GET /api/frank/prices/tomorrow   → Prijzen morgen (vanaf 15:00)
GET /api/frank/strategy          → Huidige strategie (charge/discharge/idle)
GET /api/frank/savings           → Besparingen vandaag/week/maand
```

---

## 💰 Besparing Berekening

### **Scenario: Winter Dag (30kWh verbruik)**

**Zonder Frank optimalisatie:**
```
30kWh × €0.28 gemiddeld = €8.40/dag
```

**Met Frank optimalisatie:**
```
Nachttarief (02:00-06:00):
  Laad 20kWh @ €0.08/kWh = €1.60

Dag:
  PV overschot: 5kWh gratis
  Direct verbruik: 5kWh @ €0.28 = €1.40
  
Avond (18:00-20:00):
  Gebruik 15kWh uit batterij (€0.08 gekocht) = €1.20
  Direct verbruik: 5kWh @ €0.40 = €2.00

Totaal: €1.60 + €1.40 + €1.20 + €2.00 = €6.20
Besparing: €8.40 - €6.20 = €2.20/dag
```

**Maandelijks:**
- €2.20/dag × 30 dagen = **€66/maand**
- Jaarlijks: **€792/jaar** besparing! 💶

**Met meer batterij cyclussen (zomer):**
- Zomer: meer PV, minder grid charging → €40-50/maand
- Winter: meer grid charging → €70-90/maand
- **Gemiddeld: €60-70/maand = €720-840/jaar**

---

## ⚠️ Belangrijke Overwegingen

### **1. Batterij Levensduur**
```
LiFePO4 cyclus kosten: ~€0.05/kWh
Frank voordeel: €0.10-0.20/kWh (duur - goedkoop)
Netto voordeel: €0.05-0.15/kWh ✅

Voorbeeld:
- Koop 20kWh @ €0.08 = €1.60
- Gebruik @ €0.35 = €7.00 vermeden
- Cyclus kosten: 20kWh × €0.05 = €1.00
- Netto: €7.00 - €1.60 - €1.00 = €4.40 bespaard! ✅
```

**Advies:** Limit tot 1-2 volledige cyclussen/dag voor optimale levensduur.

### **2. Grid Capaciteit**
```
Main fuse: 3×25A = 17.25kW max
Huis verbruik: ~2kW gemiddeld
Beschikbaar voor batterijen: ~15kW
Beide batterijen: 2×2.5kW = 5kW ✅

Conclusie: Grid charging is geen probleem!
```

### **3. Eddi Prioriteit Behouden**
```
Hiërarchie:
1. PV → Eddi (boiler, altijd voorrang)
2. PV overschot → Batterijen
3. Frank goedkoop → Batterijen (alleen 's nachts)
4. Frank duur → Batterijen ontladen
```

### **4. SOC Management**
```
Minimum: 35% (winterbescherming)
Maximum: 90% (levensduur)
Target voor grid charge: 80-85%

Night charging:
- Start @ 02:00 als SOC < 80%
- Stop @ 06:00 of als SOC = 90%
```

---

## 🎯 Config File

```json
{
  "frank_energie": {
    "enabled": false,
    "auth_token": "YOUR_FRANK_API_TOKEN",
    "thresholds": {
      "cheap_price": 0.15,
      "expensive_price": 0.30
    },
    "charging": {
      "max_grid_power_w": 5000,
      "target_soc": 85,
      "max_cycles_per_day": 2,
      "allowed_hours": [0, 1, 2, 3, 4, 5, 6]
    },
    "discharging": {
      "min_soc": 35,
      "max_power_w": 5000,
      "priority_hours": [17, 18, 19, 20]
    },
    "integration": {
      "respect_eddi_priority": true,
      "respect_simple_rule": true,
      "override_manual": false
    }
  }
}
```

---

## 📊 Monitoring & Logging

### **Metrics to Track:**
```python
daily_stats = {
    "grid_charged_kwh": 20.5,
    "grid_charge_cost": 1.64,
    "discharged_kwh": 18.2,
    "avoided_cost": 6.37,
    "cycle_cost": 1.03,
    "net_savings": 3.70,
    "cheapest_hour_price": 0.08,
    "most_expensive_hour_price": 0.42
}
```

### **Dashboard Graphs:**
- **Prijzen lijn:** Prijs per uur (24h), markeer charge/discharge uren
- **Besparing bar:** Dagelijks/wekelijks/maandelijks
- **Cyclus counter:** Aantal cyclussen deze maand
- **ROI tracker:** Totaal bespaard vs batterij degradatie

---

## 🚀 Implementatie Checklist

### **Pre-requirements:**
- [ ] Frank Energie account + API token
- [ ] Test API calls met Postman/curl
- [ ] Batterijen configuratie klaar (2× Venus E)
- [ ] SOC Safety Monitor draait

### **Development:**
- [ ] Frank API client (`frank_energie.py`)
- [ ] Price-based rule (`PriceBasedRule` class)
- [ ] Dashboard widget (HTML/CSS/JS)
- [ ] Config file support (`config.json`)
- [ ] REST API endpoints (`/api/frank/*`)

### **Testing:**
- [ ] API call success rate
- [ ] Price data parsing
- [ ] Charge/discharge switching
- [ ] SOC limits respected
- [ ] Eddi priority maintained
- [ ] Error handling (API down)

### **Production:**
- [ ] Logging & monitoring
- [ ] Grafana dashboard (optional)
- [ ] Alerting bij errors
- [ ] ROI tracking
- [ ] Monthly reports

---

## 📅 Timeline

**Schatting:** 4-6 uur development + 1-2 dagen testen

**Milestone 1 (2 uur):** Frank API client + basic price fetching  
**Milestone 2 (2 uur):** Price-based charging logic  
**Milestone 3 (1 uur):** Dashboard widget  
**Milestone 4 (1 uur):** Testing + refinement  

**Target:** Klaar voor januari 2025! 🎯

---

## 💡 Future Enhancements

### **Phase 2:**
- [ ] Weather forecast integratie (PV voorspelling)
- [ ] ML-based load prediction
- [ ] Auto-optimize cheap hour selection
- [ ] Multi-day planning (morgen's prijzen gebruiken)

### **Phase 3:**
- [ ] ENTSO-E integration (imbalance markt)
- [ ] Vehicle-to-Grid (V2G) support
- [ ] Community battery sharing
- [ ] Blockchain energy trading (🚀 toekomst)

---

## 📚 Links & Resources

- **Frank Energie API:** https://github.com/bajansen/home-assistant-frank_energie
- **Dynamic pricing info:** https://www.frank-energie.nl/dynamisch-contract
- **Battery degradation:** https://batteryuniversity.com/article/bu-808-how-to-prolong-lithium-based-batteries
- **LiFePO4 specs:** Venus E datasheet

---

## ❓ Open Vragen

1. **Frank API rate limits?** → Check documentation
2. **Real-time prijzen of dag vooruit?** → Dag vooruit vanaf 15:00
3. **Negatieve prijzen?** → Ja, soms! Extra voordelig!
4. **Grid teruglevering tijdens ontladen?** → Mogelijk, check met netbeheerder
5. **Belasting op batterij opslag?** → Check met accountant (energiebelasting)

---

## 🎯 Conclusie

**Frank Energie integratie = No-brainer voor 2025!**

- ✅ €60-90/maand besparing
- ✅ Batterijen optimaal benutten
- ✅ Eddi voorrang behouden
- ✅ 4-6 uur development tijd
- ✅ ROI binnen 1 maand! 💰

**Laten we het bouwen zodra saldering afloopt!** 🚀
