#!/usr/bin/env python3
"""Test WhatsApp CallMeBot API"""

import requests
from urllib.parse import quote

# Jouw gegevens
phone = "31610911365"
apikey = "4226121"
message = "Test van myEnergy systeem!"

# URL encode het bericht
encoded_message = quote(message)

# Maak de URL
url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded_message}&apikey={apikey}"

print(f"📱 Sturen naar: {phone}")
print(f"📝 Bericht: {message}")
print(f"🔗 URL: {url}")
print()

# Verstuur
try:
    response = requests.get(url, timeout=10)
    print(f"✅ Status code: {response.status_code}")
    print(f"📄 Response: {response.text}")
    
    if response.status_code == 200:
        print("\n✅ Bericht succesvol verstuurd! Check je WhatsApp!")
    else:
        print(f"\n❌ Error: Status {response.status_code}")
        print(f"Mogelijk probleem: {response.text}")
except Exception as e:
    print(f"\n❌ Fout: {e}")
