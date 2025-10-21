# 🥧 Raspberry Pi Installatie Guide

Step-by-step installatie van MyEnergy Marstek op Raspberry Pi.

## ✅ **Stap 1: Raspberry Pi Klaar Maken**

### Hardware
- Raspberry Pi 3B+ of nieuwer
- MicroSD kaart (min. 16GB)
- Stroomadapter

### OS Installeren
1. Download **Raspberry Pi Imager**: https://www.raspberrypi.com/software/
2. Kies **Raspberry Pi OS Lite (64-bit)**
3. Configureer via ⚙️ knop:
   - Hostname: `myenergy`
   - WiFi: Jouw netwerk
   - SSH: **Aanzetten!**
   - Username: `pi` / Password: (eigen keuze)
4. Flash naar SD kaart
5. Stop SD kaart in Pi en start op

---

## ✅ **Stap 2: Eerste Verbinding**

SSH naar je Pi:
```bash
ssh pi@myenergy.local
# Of met IP: ssh pi@192.168.68.XXX
```

Update systeem:
```bash
sudo apt update && sudo apt upgrade -y
```

---

## ✅ **Stap 3: Dependencies**

```bash
# Python en tools
sudo apt install -y python3-pip python3-venv git

# Bluetooth voor Marstek BLE
sudo apt install -y bluetooth bluez

# Network tools
sudo apt install -y net-tools curl
```

---

## ✅ **Stap 4: Code Downloaden**

Clone van GitHub:
```bash
cd ~
git clone https://github.com/Voskesss/marstek---myenergi.git
cd marstek---myenergi
```

Checkout de juiste branch:
```bash
git checkout feature/optimize-simple-rule
```

---

## ✅ **Stap 5: Python Environment**

Maak virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## ✅ **Stap 6: Configuratie**

Kopieer .env template:
```bash
cp .env.example .env
nano .env
```

Vul in met jouw gegevens:
```bash
# MyEnergi
MYENERGI_BASE_URL=https://s18.myenergi.net
MYENERGI_SERIAL=Z12345678
MYENERGI_API_KEY=jouw_api_key

# Batterijen (Modbus IPs)
VENUS_MODBUS_HOST=192.168.68.92
VENUS_MODBUS_HOST2=192.168.68.74

# OpenWeather
OPENWEATHER_API_KEY=jouw_openweather_key

# WhatsApp (optioneel)
WHATSAPP_TOKEN=jouw_token
WHATSAPP_PHONE_ID=jouw_phone_id
WHATSAPP_RECIPIENT=jouw_nummer

# P1 Meter (optioneel)
P1_METER_HOST=192.168.68.73
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

---

## ✅ **Stap 7: Test Draaien**

```bash
source .venv/bin/activate
python app.py
```

Als alles werkt zie je:
```
INFO: Started server process
INFO: Uvicorn running on http://0.0.0.0:8000
```

Test in browser: `http://myenergy.local:8000`

Stop met `Ctrl+C`

---

## ✅ **Stap 8: Systemd Service (Auto-start)**

Maak service file:
```bash
sudo nano /etc/systemd/system/myenergy.service
```

Plak dit erin:
```ini
[Unit]
Description=MyEnergy Marstek Integration
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/marstek---myenergi
Environment="PATH=/home/pi/marstek---myenergi/.venv/bin"
ExecStart=/home/pi/marstek---myenergi/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

Enable en start service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable myenergy
sudo systemctl start myenergy
```

Check status:
```bash
sudo systemctl status myenergy
```

Je zou moeten zien: **Active: active (running)**

---

## ✅ **Stap 9: Dashboard Openen**

Open in browser op je laptop/desktop:
```
http://myenergy.local:8000
```

Of met IP:
```
http://192.168.68.XXX:8000
```

---

## 🔧 **Handige Commando's**

### Service Beheer
```bash
# Herstarten
sudo systemctl restart myenergy

# Stoppen
sudo systemctl stop myenergy

# Starten
sudo systemctl start myenergy

# Status checken
sudo systemctl status myenergy
```

### Logs Bekijken
```bash
# Realtime logs
sudo journalctl -u myenergy -f

# Laatste 100 regels
sudo journalctl -u myenergy -n 100

# Vanaf vandaag
sudo journalctl -u myenergy --since today
```

### Code Updaten
```bash
cd ~/marstek---myenergi
git pull
sudo systemctl restart myenergy
```

### Log Files Opschonen
```bash
cd ~/marstek---myenergi
./cleanup_logs.sh
```

---

## 🎯 **Automatische Log Cleanup (Cron)**

Voeg toe aan crontab:
```bash
crontab -e
```

Voeg deze regel toe (dagelijks om 3:00):
```
0 3 * * * cd /home/pi/marstek---myenergi && ./cleanup_logs.sh
```

---

## ⚠️ **Troubleshooting**

### Service start niet
```bash
# Check logs voor errors
sudo journalctl -u myenergy -n 50

# Check of poort 8000 vrij is
sudo netstat -tlnp | grep 8000

# Test handmatig
cd ~/marstek---myenergi
source .venv/bin/activate
python app.py
```

### Geen verbinding met batterijen
```bash
# Test Modbus connectie
ping 192.168.68.92
ping 192.168.68.74

# Check firewall
sudo iptables -L
```

### Dashboard niet bereikbaar
```bash
# Check of service draait
sudo systemctl status myenergy

# Check Pi IP adres
hostname -I

# Test lokaal op Pi
curl http://localhost:8000/health
```

---

## 📋 **Aanbevelingen**

### Static IP
Geef Pi een vast IP in je router voor `myenergy.local`

### Firewall
```bash
# Alleen nodig als firewall actief is
sudo ufw allow 8000/tcp
```

### Backup
Maak regelmatig backup van:
- `.env` file
- `battery_config.json`
- `phase_*.json` files

```bash
# Backup maken
tar -czf ~/myenergy-backup-$(date +%Y%m%d).tar.gz \
  ~/marstek---myenergi/.env \
  ~/marstek---myenergi/*.json
```

---

## ✅ **Klaar!**

Je MyEnergy systeem draait nu 24/7 op je Raspberry Pi! 🎉

Dashboard: `http://myenergy.local:8000`
