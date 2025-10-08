# WhatsApp Energy Tips - Setup Guide

## ✅ Wat is er gebouwd

**Slimme energie tips via WhatsApp - 2x per dag (09:00 & 13:00)**

### 📱 Voorbeeld Berichten:

**Topdag:**
```
🎉 HET IS EEN TOPDAG!

De zon schijnt volop en er is veel overschot beschikbaar. 
Perfect moment om apparaten aan te zetten!

☀️ PV generatie: 4200W
⚡ Overschot: 1800W
🔋 Batterijen: 85% (zitten goed vol!)

💡 ACTIE: Vaatwasser, wasmachine, droger - alles mag aan! 
Gratis energie! 🎊
```

**Slecht weer:**
```
☁️ Energie Tip (09:00)

Vandaag is het waardeloos weer...
Weinig zon, dus geen verschil wanneer je de vaatwasser 
of wasmachine aanzet.

☁️ Bewolking: 85%
⚡ PV nu: 280W
💡 Tip: Gewoon doen wanneer het uitkomt!
```

---

## 🚀 Setup (15 minuten)

### **Stap 1: Activeer CallMeBot (voor beide)**

**Jos (al gedaan!):**
- ✅ Nummer: 31610911365
- ✅ API key: 4226121

**Vrouw (nog te doen):**
1. Open WhatsApp
2. Voeg toe: **+34 644 44 84 09**
3. Stuur bericht: `I allow callmebot to send me messages`
4. Ontvang API key terug
5. Geef API key door aan Jos

### **Stap 2: Configureer in App**

Edit `whatsapp_notifier.py`:

```python
self.contacts = {
    "jos": {
        "phone": "31610911365",
        "apikey": "4226121"
    },
    "vrouw": {
        "phone": "31612345678",  # Haar nummer (zonder +)
        "apikey": "HAAR_KEY"      # Haar API key
    }
}
```

### **Stap 3: Herstart App**

```bash
./start_production.sh
```

### **Stap 4: Test!**

```bash
# Test simpel bericht
curl -X POST "http://localhost:8000/api/whatsapp/test?contact=jos"

# Test energie tip (met echte data!)
curl -X POST http://localhost:8000/api/whatsapp/tip/now
```

---

## ⏰ Wanneer Komen Berichten?

**09:00 uur - Ochtend tip:**
- Check weer + PV generatie
- Advies voor de dag
- Naar beide nummers 📱📱

**13:00 uur - Middag update:**
- Actuele status
- Overschot check
- Apparaten advies
- Naar beide nummers 📱📱

**Direct - Batterij waarschuwingen:**
- SOC te laag
- Modbus errors
- Alleen naar Jos 📱

---

## 🎯 Scenarios

### **Scenario 1: Topdag**
```
Clouds < 30%
PV > 2000W
Overschot > 1000W
Batterij > 70%

→ "HET IS EEN TOPDAG! Alles mag aan!"
```

### **Scenario 2: Slecht weer**
```
Clouds > 70%
PV < 500W

→ "Waardeloos weer, maakt niet uit wanneer"
```

### **Scenario 3: Batterijen laden**
```
PV > 2000W
Batterij < 80%
Overschot < 500W

→ "Zon schijnt, batterijen laden, straks meer overschot"
```

### **Scenario 4: Eddi bezig**
```
PV > 2500W
Eddi > 1500W

→ "Eddi warmt water, wacht met grote apparaten"
```

### **Scenario 5: Matig weer**
```
PV 1000-2000W
Overschot 300-1000W

→ "Redelijk zonnetje, kleine apparaten kunnen"
```

---

## 🧪 Testen

### **Test Simpel Bericht:**
```bash
curl -X POST "http://localhost:8000/api/whatsapp/test?contact=jos"
```

Krijg je een test bericht? ✅

### **Test Energie Tip:**
```bash
curl -X POST http://localhost:8000/api/whatsapp/tip/now
```

Krijg je een tip gebaseerd op huidige situatie? ✅

### **Check Logs:**
```bash
tail -f logs/app.log | grep WhatsApp
```

Zie je:
```
📱 WhatsApp tips scheduler started (09:00 & 13:00)
✅ WhatsApp sent to jos: ...
```

---

## ⚙️ Configuratie Aanpassen

### **Wijzig tijden:**

Edit `app.py` regel ~2356:

```python
if current_hour == 9 and current_minute == 0:  # Wijzig naar 8 voor 08:00
    tip_time = "09:00"
elif current_hour == 13 and current_minute == 0:  # Wijzig naar 14 voor 14:00
    tip_time = "13:00"
```

### **Voeg extra tijd toe (bijv. 17:00):**

```python
elif current_hour == 17 and current_minute == 0:
    tip_time = "17:00"
    should_send = f"{today_key}-17" not in sent_today
```

### **Pas berichten aan:**

Edit `whatsapp_notifier.py` - de berichten staan in de `send_energy_tip()` functie.

---

## 📊 Monitoring

### **Check of scheduler draait:**
```bash
curl http://localhost:8000/api/health
```

### **Check laatste bericht:**
```bash
tail logs/app.log | grep "Energy tip sent"
```

### **Force send tip (test):**
```bash
curl -X POST http://localhost:8000/api/whatsapp/tip/now
```

---

## ⚠️ Troubleshooting

### **Geen berichten ontvangen?**

1. **Check API keys:**
```python
# In whatsapp_notifier.py
print(self.contacts)  # Zijn de keys correct?
```

2. **Check CallMeBot status:**
```bash
curl "https://api.callmebot.com/whatsapp.php?phone=31610911365&text=Test&apikey=4226121"
```

3. **Check logs:**
```bash
grep "WhatsApp" logs/app.log
```

### **Berichten komen te laat?**

CallMeBot kan 10-60 seconden vertraging hebben. Dit is normaal! ⏱️

### **Foutmelding "whatsapp_notifier.py not found"?**

```bash
# Check of bestand bestaat
ls -la whatsapp_notifier.py

# Herstart app
./start_production.sh
```

### **Berichten naar verkeerd nummer?**

Check `whatsapp_notifier.py` regel 11-20:
- Nummer ZONDER '+' prefix
- Correct API key per nummer

---

## 🎉 Success Checklist

- [ ] Jos CallMeBot activated ✅
- [ ] Vrouw CallMeBot activated
- [ ] whatsapp_notifier.py configured
- [ ] App herstart
- [ ] Test bericht ontvangen (jos)
- [ ] Test bericht ontvangen (vrouw)
- [ ] Energie tip test gelukt
- [ ] Logs tonen "📱 WhatsApp tips scheduler started"

**Morgen 09:00:** Eerste automatische tip! 🎊

---

## 💡 Tips

- **Vrouw activeren:** Stuur haar de link + instructies via SMS
- **Test eerst met alleen Jos:** Pas vrouw toe als het werkt
- **Check om 09:05:** Als het niet automatisch komt, check logs
- **Pas berichten aan:** Maak ze persoonlijker/grappiger!
- **Extra features:** Batterij waarschuwingen komen automatisch (alleen naar Jos)

---

## 🚀 Volgende Stappen

1. ✅ **Nu:** Vrouw laten activeren
2. ✅ **Morgen 09:00:** Wachten op eerste tip
3. ✅ **Later:** Berichten personaliseren
4. 📅 **2025:** Frank Energie prijzen toevoegen

**Veel plezier met de energy tips!** ☀️📱
