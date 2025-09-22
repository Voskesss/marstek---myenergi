# 🔋 myenergi ↔ Marstek Smart Battery Control

Automatische controle van Marstek batterijen op basis van myenergi data met slimme prioriteit logica.

## ✨ Features

- **🎯 Smart Priority Logic**: Zappi (auto) > Eddi (warmwater) > Batterij (opslag)
- **🔄 Anti-Toggle Protection**: Hysterese voorkomt rapid switching bij wisselend weer
- **📱 BLE Support**: Bluetooth Low Energy fallback voor oude firmware
- **📊 Live Dashboard**: Real-time monitoring en controle
- **🔧 Multi-Battery Support**: Automatische discovery van meerdere batterijen
- **⚡ Threshold Management**: Configureerbare export drempels

## Functionaliteit
- Leest myenergi status via cloud (DigestAuth, `https://sXX.myenergi.net/cgi-jstatus-*`) of lokale hub.
- Afgeleiden: grid export/import (W, genormaliseerd: export > 0), Eddi-vermogen (W).
- Stuurt Marstek: `allow`/`inhibit` laden tijdens Eddi-verwarming op basis van drempels en hysterese.
- API endpoints:
  - `GET /health`
  - `GET /api/status`
  - `POST /api/control?action=allow|inhibit|status`

## Snel starten
1) Python omgeving
- Aanbevolen: Python 3.10+
- (optioneel) Virtuele omgeving
```
python3 -m venv .venv
source .venv/bin/activate
```

2) Dependencies installeren
```
pip install -r requirements.txt
```

3) .env instellen (niet committen)
- Maak een `.env` naast `app.py` met jouw waarden:
```
MYENERGI_BASE_URL=https://s18.myenergi.net
MYENERGI_HUB_SERIAL=Zxxxxxxxx
MYENERGI_API_KEY=<<JOUW_API_KEY>>

# Marstek lokaal (pas aan zodra bekend)
MARSTEK_BASE_URL=http://192.168.1.60
MARSTEK_API_TOKEN=

# Optionele drempels
EDDI_ACTIVE_W=200
EXPORT_ENOUGH_W=300
IMPORT_DIP_W=150
STABLE_EXPORT_SECONDS=30
MIN_SWITCH_COOLDOWN_S=60
SOC_FAILSAFE_MIN=15
POLL_INTERVAL_S=2
```
- Belangrijk: gebruik `.env.example` alleen als referentie. Laat daar geen echte secrets in staan.

4) Starten
```
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
- Test:
  - `http://localhost:8000/health` → `{ "ok": true }`
  - `http://localhost:8000/api/status` → JSON met myenergi/battery/derived/params

## MyEnergi cloud check (curl)
```
curl --digest -u HUB_SERIAL:API_KEY \
  -H "User-Agent: Wget/1.14 (linux-gnu)" \
  "https://s18.myenergi.net/cgi-jstatus-*"
```
- Vervang `s18` en `HUB_SERIAL`/`API_KEY` door jouw waarden.

## macOS 24/7 via launchd (geen Docker)
1) Pas de plist aan:
- Open `launchd/com.myenergy.marstek.plist` en wijzig:
  - `ProgramArguments` pad naar je Python en uvicorn
  - `WorkingDirectory` naar deze projectmap
  - `EnvironmentVariables` volgens jouw `.env`

2) Installeer en start
```
mkdir -p ~/Library/LaunchAgents
cp launchd/com.myenergy.marstek.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.myenergy.marstek.plist
```
- Logs: `log stream --predicate 'process == "com.myenergy.marstek"'`
- Stoppen: `launchctl unload -w ~/Library/LaunchAgents/com.myenergy.marstek.plist`

Tip: bij Python-venv, geef het volledige pad naar `.venv/bin/uvicorn`.

## Belangrijke notities
- MyEnergi cloud gebruikt DigestAuth en vereist een User-Agent header. De client detecteert automatisch cloud vs lokaal.
- `pgrid` normalisatie: veel firmwares gebruiken + = import, - = export. Wij keren het teken om zodat export positief is.
- Marstek endpoints zijn placeholders (`/api/overview`, `/api/control`). Pas aan zodra je de echte paden/velden hebt.
- Veiligheid: stel de service niet publiek bloot zonder extra auth/reverse proxy.

## Uitbreiden
- Web UI (grafiek + toggles) bovenop deze API.
- Persistente logging (SQLite) van schakelmomenten.
- Fijnmazige hysterese en rate-limiting.
- Auth-token op `/api/control`.

## Credits/referenties
- MyEnergi community-repo’s: twonk/MyEnergi-App-Api, ashleypittman/mec, DougieLawson/Zappi_API.
