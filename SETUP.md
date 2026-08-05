# Marstek Myenergi Integration - Setup & Documentatie

## Overzicht

Dit project integreert Marstek batterijen met myenergi (Eddi, Zappi, Harvi) via een Raspberry Pi. Het systeem:
- Monitort energie flows in realtime
- Stuurt batterijen automatisch op basis van export naar net
- Toont een visuele flow visualisatie op `/flow.html`
- Houdt dagelijkse energie statistieken bij
- Heeft een rapportpagina met CSV export

## Architectuur

```
┌─────────────────┐
│  Raspberry Pi   │
│  (192.168.68.157) │
└────────┬────────┘
         │
    ┌────┴────┬──────────────────────┐
    │         │                      │
┌───▼───┐ ┌──▼──────┐         ┌─────▼─────┐
│ myenergi│ │Marstek  │         │  myenergi │
│  API   │ │Batterijen│         │  Devices  │
│(cloud) │ │(Modbus) │         │   (Eddi,  │
└───────┘ │  74, 92 │         │   Zappi)  │
         └──────────┘         └───────────┘
```

### Componenten

1. **Raspberry Pi** — Draait de Python backend (FastAPI) en frontend
2. **myenergi API** — Cloud API voor Eddi, Zappi, Harvi status
3. **Marstek Batterijen** — Twee batterijen via Modbus TCP:
   - `192.168.68.74` — Venus EV2 (tank 1)
   - `192.168.68.92` — Venus EV2 (tank 2)
4. **Backend** (`app.py`) — FastAPI server op poort 8000
5. **Frontend** — `flow.html`, `dashboard.html`, `rapport.html`

## SSH Connectie

### vanaf Mac/Linux

```bash
ssh josklijnhout@192.168.68.157
```

### vanaf Windows (PowerShell)

```powershell
ssh josklijnhout@192.168.68.157
```

### vanaf Windows (PuTTY)
- Host: `192.168.68.157`
- Port: `22`
- User: `josklijnhout`

### SSH Key setup (optioneel, voor passwordless login)

Op je Mac/Linux:
```bash
# Genereer key als je die nog niet hebt
ssh-keygen -t ed25519

# Kopieer key naar Pi
ssh-copy-id josklijnhout@192.168.68.157
```

## Project Structuur

```
marstekbatterijen/
├── app.py              # Backend (FastAPI, batterij logica, myenergi API)
├── battery_config.json # Batterij configuratie (Modbus registers)
├── flow.html           # Energie flow visualisatie
├── dashboard.html      # Myenergi dashboard
├── rapport.html        # Energie rapport + CSV export
├── app.html           # Wrapper pagina (niet meer gebruikt)
├── energy_daily.json   # Dagelijkse energie statistieken
├── requirements.txt    # Python dependencies
└── .env                # Environment variables (indien gebruikt)
```

## Git Branches

- `main` — Hoofd branch
- `raspberry-pi` — Actieve branch op de Pi (waar wijzigingen worden gemaakt)
- `raspberry-pi-ready` — Voorbereide Pi versie
- `feature/optimize-simple-rule` — Feature branch voor batterij regeloptimalisatie

## Service Management

De service draait als systemd service:

```bash
# Service status checken
sudo systemctl status myenergi-marstek

# Service herstarten
sudo systemctl restart myenergi-marstek

# Service logs bekijken
sudo journalctl -u myenergi-marstek -f

# Service stoppen
sudo systemctl stop myenergi-marstek

# Service starten
sudo systemctl start myenergi-marstek
```

## API Endpoints

### Status & Energie
- `GET /api/status` — Volledige status (myenergi + batterijen)
- `GET /api/batteries/{id}/status` — Specifieke batterij status
- `GET /api/energy/today` — Energie statistieken vandaag
- `GET /api/energy/history?days=7` — Historie (max 365 dagen)
- `GET /api/energy/csv?days=90` — CSV export

### Simple Battery Rule
- `GET /api/simple_rule/status` — Regel status
- `POST /api/simple_rule/enable` — Regel aanzetten
- `POST /api/simple_rule/disable` — Regel uitzetten
- `POST /api/simple_rule/setpoint?w=2500` — Laadvermogen instellen

### Pagina's
- `GET /flow.html` — Flow visualisatie
- `GET /dashboard` — Myenergi dashboard
- `GET /rapport` — Energie rapport

## Lokale Ontwikkeling

Op je Mac:

```bash
# Clone repository
git clone https://github.com/Voskesss/marstek---myenergi.git
cd marstek---myenergi

# Switch naar raspberry-pi branch
git checkout raspberry-pi

# Installeer dependencies
pip install -r requirements.txt

# Start server (lokaal)
python app.py
```

## Wijzigingen Deployen

Van Mac naar Raspberry Pi:

```bash
# Op Mac: commit en push
git add .
git commit -m "Beschrijving van wijziging"
git push origin raspberry-pi

# Op Raspberry Pi: pull
cd /home/josklijnhout/marstekbatterijen
git pull origin raspberry-pi

# Herstart service
sudo systemctl restart myenergi-marstek
```

## Troubleshooting

### Batterij niet bereikbaar
```bash
# Ping batterij
ping 192.168.68.74
ping 192.168.68.92

# Check Modbus connectie
sudo journalctl -u myenergi-marstek | grep "Modbus"
```

### myenergi API errors
```bash
# Check logs voor 401 errors
sudo journalctl -u myenergi-marstek | grep "401"
```

### Service start niet
```bash
# Check service status
sudo systemctl status myenergi-marstek

# Bekijk laatste logs
sudo journalctl -u myenergi-marstek -n 50
```

### Frontend laadt niet
```bash
# Check of server draait
curl http://localhost:8000/api/status

# Check poort
netstat -tlnp | grep 8000
```

## Configuratie

Batterij configuratie in `battery_config.json`:
```json
{
  "batteries": [
    {
      "id": "venus_ev2_74",
      "name": "Batterij 74",
      "ip": "192.168.68.74",
      "port": 502,
      "unit": 1
    },
    {
      "id": "venus_ev2_92",
      "name": "Batterij 92",
      "ip": "192.168.68.92",
      "port": 502,
      "unit": 1
    }
  ]
}
```

## Netwerk

- Raspberry Pi: `192.168.68.157`
- Batterij 74: `192.168.68.74` (Modbus TCP op poort 502)
- Batterij 92: `192.168.68.92` (Modbus TCP op poort 502)
- myenergi Cloud: `https://s18.myenergi.net`

## Veiligheid

- SSH alleen toegankelijk binnen LAN
- API endpoints geen authenticatie (LAN only)
- Batterij Modbus geen encryptie (LAN only)
- myenergi API gebruikt basic auth (credentials in code)
