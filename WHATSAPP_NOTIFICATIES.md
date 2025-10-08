# WhatsApp Notificaties Integratie

## 📱 Doel
WhatsApp berichten sturen voor:
- ☀️ Dagelijkse zonnevoorspelling (naar jou + vrouw)
- 🔋 Batterij waarschuwingen (kritieke SOC, errors)
- 💶 Frank Energie prijswaarschuwingen (later)
- 📊 Dagelijkse statistieken

---

## 🚀 Gekozen Methode: CallMeBot

**Voordelen:**
- ✅ **GRATIS** - Onbeperkt berichten
- ✅ **Simpel** - Alleen phone nummer nodig
- ✅ **Direct** - Geen API keys, geen account
- ✅ **Betrouwbaar** - Werkt via WhatsApp Web

**Link:** https://www.callmebot.com/blog/free-api-whatsapp-messages/

---

## 📝 Setup (5 minuten!)

### **Stap 1: Activeer CallMeBot**

**Voor jou:**
1. Voeg nummer toe aan contacts: **+34 644 44 84 09**
2. Stuur WhatsApp bericht: `I allow callmebot to send me messages`
3. Je krijgt terug: `API Activated for your phone number. Your APIKEY is XXXXXX`
4. Bewaar je API key!

**Voor je vrouw:**
- Zelfde stappen herhalen met haar telefoon

### **Stap 2: Test Bericht**

```bash
# Test direct via curl
curl "https://api.callmebot.com/whatsapp.php?phone=+31612345678&text=Test+van+myEnergy+systeem&apikey=JOUW_KEY"
```

Als het werkt krijg je direct een WhatsApp! ✅

---

## 🔧 Implementatie

### **1. Python WhatsApp Client**

```python
# whatsapp_notifier.py
import aiohttp
from urllib.parse import quote
import logging

logger = logging.getLogger(__name__)

class WhatsAppNotifier:
    def __init__(self):
        self.base_url = "https://api.callmebot.com/whatsapp.php"
        self.contacts = {
            "jos": {
                "phone": "+31612345678",  # Jouw nummer
                "apikey": "JOUW_APIKEY"
            },
            "vrouw": {
                "phone": "+31687654321",  # Haar nummer
                "apikey": "HAAR_APIKEY"
            }
        }
    
    async def send_message(self, contact: str, message: str) -> bool:
        """Send WhatsApp message via CallMeBot"""
        try:
            if contact not in self.contacts:
                logger.error(f"Unknown contact: {contact}")
                return False
            
            contact_info = self.contacts[contact]
            phone = contact_info["phone"]
            apikey = contact_info["apikey"]
            
            # URL encode message
            encoded_message = quote(message)
            
            url = f"{self.base_url}?phone={phone}&text={encoded_message}&apikey={apikey}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        logger.info(f"✅ WhatsApp sent to {contact}")
                        return True
                    else:
                        logger.error(f"❌ WhatsApp failed: {response.status}")
                        return False
        
        except Exception as e:
            logger.error(f"❌ WhatsApp error: {e}")
            return False
    
    async def send_to_both(self, message: str):
        """Send message to both contacts"""
        await self.send_message("jos", message)
        await self.send_message("vrouw", message)
    
    async def send_daily_forecast(self, weather_data: dict):
        """Send daily weather forecast"""
        clouds = weather_data.get("clouds", 0)
        temp = weather_data.get("temperature", 0)
        
        # Determine if good or bad solar day
        if clouds < 30:
            emoji = "☀️"
            verdict = "GOEDE zonnedag"
        elif clouds < 60:
            emoji = "⛅"
            verdict = "GEMIDDELDE zonnedag"
        else:
            emoji = "☁️"
            verdict = "SLECHTE zonnedag"
        
        message = f"""
{emoji} *myEnergy Dagrapport*

📅 {weather_data.get('date', 'Vandaag')}
🌡️ Temperatuur: {temp}°C
☁️ Bewolking: {clouds}%

{verdict}!

🔋 Batterijen: {weather_data.get('battery_soc', 0)}%
⚡ Verwachte PV: {weather_data.get('expected_pv', 'Onbekend')}

_Automatisch bericht van myEnergy systeem_
        """.strip()
        
        await self.send_to_both(message)
    
    async def send_battery_warning(self, battery_id: str, soc: float, reason: str):
        """Send battery warning (only to Jos)"""
        message = f"""
⚠️ *Batterij Waarschuwing*

🔋 Batterij: {battery_id}
📊 SOC: {soc}%
❗ Reden: {reason}

Check het dashboard!
        """.strip()
        
        await self.send_message("jos", message)
    
    async def send_daily_stats(self, stats: dict):
        """Send end-of-day statistics"""
        message = f"""
📊 *myEnergy Dagrapport*

☀️ PV opgewekt: {stats.get('pv_kwh', 0):.1f} kWh
🔋 Batterij geladen: {stats.get('charged_kwh', 0):.1f} kWh
⚡ Huis verbruik: {stats.get('consumed_kwh', 0):.1f} kWh
🏠 Grid import: {stats.get('grid_import_kwh', 0):.1f} kWh
💰 Kosten: €{stats.get('cost', 0):.2f}

Zelfvoorzieningsgraad: {stats.get('self_sufficiency', 0):.0f}%
        """.strip()
        
        await self.send_message("jos", message)

# Global instance
whatsapp = WhatsAppNotifier()
```

---

### **2. Integratie in App.py**

```python
# In app.py - add to startup
from whatsapp_notifier import whatsapp

@app.on_event("startup")
async def startup_notifications():
    """Schedule daily notifications"""
    asyncio.create_task(_daily_forecast_task())
    asyncio.create_task(_daily_stats_task())

async def _daily_forecast_task():
    """Send morning forecast at 07:00"""
    while True:
        now = datetime.now()
        
        # Check if 07:00
        if now.hour == 7 and now.minute == 0:
            try:
                # Get weather forecast
                weather = await weather_service.get_current_weather()
                
                # Add battery info
                weather_data = {
                    "date": now.strftime("%d-%m-%Y"),
                    "clouds": weather.get("clouds", 0),
                    "temperature": weather.get("temperature", 0),
                    "battery_soc": await _get_total_battery_soc(),
                    "expected_pv": _estimate_pv_generation(weather)
                }
                
                # Send to both
                await whatsapp.send_daily_forecast(weather_data)
                
                # Sleep until tomorrow
                await asyncio.sleep(3600 * 23)  # 23 hours
            except Exception as e:
                logger.error(f"Daily forecast error: {e}")
                await asyncio.sleep(60)
        else:
            # Check every minute until 07:00
            await asyncio.sleep(60)

async def _daily_stats_task():
    """Send daily stats at 21:00"""
    while True:
        now = datetime.now()
        
        if now.hour == 21 and now.minute == 0:
            try:
                stats = await _calculate_daily_stats()
                await whatsapp.send_daily_stats(stats)
                await asyncio.sleep(3600 * 23)
            except Exception as e:
                logger.error(f"Daily stats error: {e}")
                await asyncio.sleep(60)
        else:
            await asyncio.sleep(60)
```

---

### **3. Config File**

```json
{
  "whatsapp_notifications": {
    "enabled": true,
    "contacts": {
      "jos": {
        "phone": "+31612345678",
        "apikey": "JOUW_CALLMEBOT_KEY"
      },
      "vrouw": {
        "phone": "+31687654321",
        "apikey": "HAAR_CALLMEBOT_KEY"
      }
    },
    "schedules": {
      "morning_forecast": "07:00",
      "evening_stats": "21:00"
    },
    "alerts": {
      "battery_low_soc": true,
      "battery_error": true,
      "modbus_error": false,
      "price_extremes": false
    }
  }
}
```

---

## 📱 Bericht Voorbeelden

### **1. Ochtend Voorspelling (07:00)**

**Goede zonnedag:**
```
☀️ myEnergy Dagrapport

📅 06-10-2025
🌡️ Temperatuur: 18°C
☁️ Bewolking: 15%

GOEDE zonnedag!

🔋 Batterijen: 85%
⚡ Verwachte PV: 25-30 kWh

Automatisch bericht van myEnergy systeem
```

**Slechte zonnedag:**
```
☁️ myEnergy Dagrapport

📅 06-10-2025
🌡️ Temperatuur: 12°C
☁️ Bewolking: 85%

SLECHTE zonnedag!

🔋 Batterijen: 45%
⚡ Verwachte PV: 3-5 kWh

Let op: weinig PV verwacht, batterijen laden?
```

---

### **2. Batterij Waarschuwing (direct)**

```
⚠️ Batterij Waarschuwing

🔋 Batterij: venus_ev2_92
📊 SOC: 32%
❗ Reden: Onder minimum 35%

Check het dashboard!
```

---

### **3. Avond Statistieken (21:00)**

```
📊 myEnergy Dagrapport

☀️ PV opgewekt: 18.5 kWh
🔋 Batterij geladen: 15.2 kWh
⚡ Huis verbruik: 22.3 kWh
🏠 Grid import: 4.1 kWh
💰 Kosten: €1.15

Zelfvoorzieningsgraad: 82%
```

---

## 🎯 Use Cases

### **Use Case 1: Goede Zonnedag**
```
07:00 - WhatsApp naar beide:
"☀️ GOEDE zonnedag! 15% bewolking, 25kWh PV verwacht"

→ Vrouw weet: Wasmachine, droger, vaatwasser kunnen aan
→ Jij weet: Batterijen zullen vol laden
```

### **Use Case 2: Slechte Zonnedag**
```
07:00 - WhatsApp naar beide:
"☁️ SLECHTE zonnedag! 85% bewolking, 3kWh PV verwacht"

→ Vrouw weet: Zuinig met stroom
→ Jij weet: Check of batterijen genoeg geladen zijn
```

### **Use Case 3: Batterij Probleem**
```
14:30 - WhatsApp naar Jos:
"⚠️ Batterij venus_ev2_92: SOC 30%, Modbus timeout"

→ Check dashboard
→ Los probleem op voordat 's avonds geen reserve
```

### **Use Case 4: Avond Samenvatting**
```
21:00 - WhatsApp naar Jos:
"📊 Vandaag: 18kWh PV, 82% zelfvoorzienend, €1.15 kosten"

→ Overzicht van de dag
→ Check of systeem goed presteert
```

---

## ⚙️ Extra Features

### **Feature 1: Eddi Boost Notificatie**
```python
async def notify_eddi_boost(target_temp: int):
    message = f"""
🔥 *Eddi Boost Gestart*

♨️ Boiler doeltemperatuur: {target_temp}°C
⚡ Overscho t beschikbaar: 3.2kW
⏱️ Geschatte tijd: 2 uur

PV wordt gebruikt voor warm water!
    """
    await whatsapp.send_message("vrouw", message)
```

**Trigger:** Als Eddi boost start met veel PV overschot

---

### **Feature 2: Frank Energie Prijswaarschuwing**
```python
async def notify_price_extreme(price: float, hour: int, type: str):
    emoji = "💰" if type == "cheap" else "⚠️"
    message = f"""
{emoji} *Frank Energie Waarschuwing*

{'⬇️ SUPER GOEDKOOP' if type == 'cheap' else '⬆️ SUPER DUUR'}

⏰ Uur: {hour}:00
💶 Prijs: €{price:.3f}/kWh

{'Batterijen laden gepland!' if type == 'cheap' else 'Gebruik minimaliseren!'}
    """
    await whatsapp.send_to_both(message)
```

**Trigger:** 
- Prijs < €0.08 → Laden notification
- Prijs > €0.40 → Duur notification

---

### **Feature 3: Wekelijkse Samenvatting**
```python
async def send_weekly_summary(stats: dict):
    message = f"""
📊 *Weekoverzicht ({stats['week']})*

☀️ Totale PV: {stats['total_pv']:.0f} kWh
⚡ Verbruik: {stats['total_consumption']:.0f} kWh
🏠 Grid import: {stats['total_grid']:.0f} kWh
💰 Totale kosten: €{stats['total_cost']:.2f}

📈 Zelfvoorzieningsgraad: {stats['self_sufficiency']:.0f}%
🔋 Batterij cyclussen: {stats['battery_cycles']}

{'🏆 Nieuwe record: Hoogste zelfvoorzieningsgraad!' if stats['is_record'] else ''}
    """
    await whatsapp.send_message("jos", message)
```

**Trigger:** Zondag 21:00

---

## 🔐 Security & Privacy

### **Best Practices:**

1. **API Keys in .env:**
```bash
# .env
WHATSAPP_JOS_PHONE="+31612345678"
WHATSAPP_JOS_KEY="123456"
WHATSAPP_VROUW_PHONE="+31687654321"
WHATSAPP_VROUW_KEY="789012"
```

2. **Rate Limiting:**
```python
# Max 1 message per second (CallMeBot limit)
import asyncio
from collections import deque

class RateLimiter:
    def __init__(self, max_per_second=1):
        self.max_per_second = max_per_second
        self.timestamps = deque()
    
    async def acquire(self):
        now = time.time()
        # Remove old timestamps
        while self.timestamps and self.timestamps[0] < now - 1:
            self.timestamps.popleft()
        
        # Wait if limit reached
        if len(self.timestamps) >= self.max_per_second:
            wait_time = 1 - (now - self.timestamps[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
        
        self.timestamps.append(time.time())
```

3. **Error Handling:**
```python
# Retry logic voor failed messages
async def send_with_retry(contact: str, message: str, max_retries=3):
    for attempt in range(max_retries):
        try:
            success = await whatsapp.send_message(contact, message)
            if success:
                return True
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
        except Exception as e:
            logger.error(f"Attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                return False
    return False
```

---

## 📊 Dashboard Integration

### **WhatsApp Status Widget:**

```html
<div class="card whatsapp-status">
    <h3>📱 WhatsApp Notificaties</h3>
    
    <div class="metric">
        <span class="metric-label">Status</span>
        <span class="metric-value status-good">✅ Actief</span>
    </div>
    
    <div class="metric">
        <span class="metric-label">Laatst verzonden</span>
        <span class="metric-value">07:00 - Dagvoorspelling</span>
    </div>
    
    <div class="metric">
        <span class="metric-label">Vandaag verzonden</span>
        <span class="metric-value">3 berichten</span>
    </div>
    
    <button class="btn btn-primary" onclick="testWhatsApp()">
        📱 Test Bericht
    </button>
</div>
```

### **API Endpoints:**

```python
@app.post("/api/whatsapp/test")
async def test_whatsapp(contact: str = "jos"):
    """Send test message"""
    message = "🧪 Test bericht van myEnergy systeem!"
    success = await whatsapp.send_message(contact, message)
    return {"success": success}

@app.get("/api/whatsapp/status")
async def whatsapp_status():
    """Get WhatsApp notification status"""
    return {
        "success": True,
        "enabled": True,
        "last_sent": "2025-10-06 07:00:00",
        "messages_today": 3,
        "contacts": ["jos", "vrouw"]
    }
```

---

## ✅ Implementatie Checklist

### **Setup (15 minuten):**
- [ ] Jos: Activeer CallMeBot API (5 min)
- [ ] Vrouw: Activeer CallMeBot API (5 min)
- [ ] Test berichten versturen (5 min)

### **Development (2-3 uur):**
- [ ] `whatsapp_notifier.py` maken
- [ ] Integratie in `app.py`
- [ ] Daily forecast functie
- [ ] Daily stats functie
- [ ] Config file support
- [ ] Dashboard widget

### **Testing (1 uur):**
- [ ] Test berichten naar beide nummers
- [ ] Test forecast bericht formatting
- [ ] Test stats bericht
- [ ] Test waarschuwingen
- [ ] Test rate limiting

### **Production:**
- [ ] Monitoring & logging
- [ ] Error alerting (als WhatsApp fails)
- [ ] Message templates in config
- [ ] Multi-language support (NL/EN)

---

## 💡 Toekomstige Ideeën

- [ ] **Voice berichten:** Text-to-speech via CallMeBot
- [ ] **Afbeeldingen:** Grafieken als PNG versturen
- [ ] **Interactieve berichten:** Reply met commando's
- [ ] **Groep chat:** Familie groep voor updates
- [ ] **Telegram integratie:** Als backup
- [ ] **Email fallback:** Als WhatsApp faalt

---

## 🎯 Conclusie

**WhatsApp notificaties = Super handig! 📱**

- ✅ 15 minuten setup
- ✅ 2-3 uur development
- ✅ Gratis (CallMeBot)
- ✅ Geen API keys gedoe
- ✅ Direct bruikbaar

**Voordelen:**
- Vrouw weet of het goed weer wordt → Plan huishoudelijke apparaten
- Jij krijgt waarschuwingen bij problemen → Snel oplossen
- Dagelijkse stats → Overzicht prestaties
- Frank Energie waarschuwingen → Optimaal laden/ontladen

**Start vandaag al met setup!** 🚀
