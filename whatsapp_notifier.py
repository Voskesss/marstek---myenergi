#!/usr/bin/env python3
"""WhatsApp Notifier via CallMeBot - Smart Energy Tips"""

import aiohttp
import asyncio
from urllib.parse import quote
import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class WhatsAppNotifier:
    def __init__(self):
        self.base_url = "https://api.callmebot.com/whatsapp.php"
        self.contacts = {
            "jos": {
                "phone": "31610911365",
                "apikey": "4226121"
            },
            "emmy": {
                "phone": "31612087275",
                "apikey": "6478375"
            }
        }
        self.last_sent = {}  # Track when we last sent messages
    
    async def send_message(self, contact: str, message: str) -> bool:
        """Send WhatsApp message via CallMeBot"""
        try:
            if contact not in self.contacts:
                logger.error(f"Unknown contact: {contact}")
                return False
            
            contact_info = self.contacts[contact]
            phone = contact_info["phone"]
            apikey = contact_info["apikey"]
            
            # Skip if not configured
            if not phone or not apikey:
                logger.warning(f"Contact {contact} not configured, skipping")
                return False
            
            # URL encode message
            encoded_message = quote(message)
            
            url = f"{self.base_url}?phone={phone}&text={encoded_message}&apikey={apikey}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    response_text = await response.text()
                    if response.status == 200 and "queued" in response_text.lower():
                        logger.info(f"✅ WhatsApp sent to {contact}: {message[:50]}...")
                        self.last_sent[contact] = datetime.now()
                        return True
                    else:
                        logger.error(f"❌ WhatsApp failed: {response.status} - {response_text}")
                        return False
        
        except Exception as e:
            logger.error(f"❌ WhatsApp error for {contact}: {e}")
            return False
    
    async def send_to_both(self, message: str):
        """Send message to both contacts"""
        tasks = []
        for contact in ["jos", "emmy"]:
            if self.contacts[contact]["phone"]:  # Only if configured
                tasks.append(self.send_message(contact, message))
        
        if tasks:
            await asyncio.gather(*tasks)
    
    def _get_weather_emoji(self, clouds: int) -> str:
        """Get emoji based on cloud coverage"""
        if clouds < 20:
            return "☀️"
        elif clouds < 40:
            return "🌤️"
        elif clouds < 60:
            return "⛅"
        elif clouds < 80:
            return "🌥️"
        else:
            return "☁️"
    
    async def send_energy_tip(self, energy_data: Dict[str, Any]):
        """Send smart energy tip based on current situation
        
        Args:
            energy_data: {
                "clouds": 45,           # Cloud coverage %
                "temperature": 18,      # Celsius
                "pv_now_w": 3500,      # Current PV generation
                "grid_w": -800,        # Grid import/export (negative = export)
                "battery_soc": 85,     # Battery %
                "battery_power": 1200, # Battery charging power
                "overschot_w": 1500,   # Available surplus
                "eddi_w": 2000,        # Eddi using
                "house_w": 1200        # House consumption
            }
        """
        clouds = energy_data.get("clouds", 100)
        pv_now = energy_data.get("pv_now_w", 0)
        grid_w = energy_data.get("grid_w", 0)
        battery_soc = energy_data.get("battery_soc", 0)
        overschot = energy_data.get("overschot_w", 0)
        eddi_w = energy_data.get("eddi_w", 0)
        house_w = energy_data.get("house_w", 0)
        
        # Determine scenario
        emoji = self._get_weather_emoji(clouds)
        now = datetime.now()
        time_str = now.strftime("%H:%M")
        
        # Scenario 1: Slecht weer (weinig zon)
        if clouds > 70 or pv_now < 500:
            message = f"""{emoji} *Energie Tip ({time_str})*

Vandaag is het waardeloos weer...
Weinig zon, dus geen verschil wanneer je de vaatwasser of wasmachine aanzet.

☁️ Bewolking: {clouds}%
⚡ PV nu: {pv_now}W
💡 Tip: Gewoon doen wanneer het uitkomt!

_myEnergy systeem_"""
        
        # Scenario 2: Goed weer + veel overschot!
        elif pv_now > 2000 and overschot > 1000 and battery_soc > 70:
            message = f"""{emoji} *Energie Tip ({time_str})*

🎉 HET IS EEN TOPDAG!

De zon schijnt volop en er is veel overschot beschikbaar. Perfect moment om apparaten aan te zetten!

☀️ PV generatie: {pv_now}W
⚡ Overschot: {overschot}W
🔋 Batterijen: {battery_soc}% (zitten goed vol!)

💡 *ACTIE:* Vaatwasser, wasmachine, droger - alles mag aan! Gratis energie! 🎊

_myEnergy systeem_"""
        
        # Scenario 3: Goed weer, maar batterijen aan het laden
        elif pv_now > 2000 and battery_soc < 80 and overschot < 500:
            message = f"""{emoji} *Energie Tip ({time_str})*

Zon schijnt goed! ☀️

De batterijen zijn nu aan het laden ({battery_soc}%). Straks komt er meer overschot beschikbaar.

⚡ PV generatie: {pv_now}W
🔋 Batterijen laden: {battery_soc}%

💡 Tip: Wacht nog even (±30 min), dan is er meer overschot voor grote apparaten!

_myEnergy systeem_"""
        
        # Scenario 4: Goed weer, Eddi bezig (boiler)
        elif pv_now > 2500 and eddi_w > 1500:
            message = f"""{emoji} *Energie Tip ({time_str})*

Lekker zonnetje! De Eddi warmt het water op. 🔥

♨️ Boiler: {eddi_w}W
☀️ PV: {pv_now}W
⚡ Overschot: {overschot}W

💡 Tip: {'Perfect moment voor kleine apparaten!' if overschot > 500 else 'Wacht met grote apparaten tot boiler klaar is'}

_myEnergy systeem_"""
        
        # Scenario 5: Matig weer, klein beetje overschot
        elif pv_now > 1000 and overschot > 300:
            message = f"""{emoji} *Energie Tip ({time_str})*

Redelijk zonnetje vandaag! ⛅

Er is wat overschot, genoeg voor kleinere apparaten.

⚡ PV: {pv_now}W
💡 Overschot: {overschot}W

Tip: Wasmachine kan aan, maar vermijd grote verbruikers zoals droger.

_myEnergy systeem_"""
        
        # Scenario 6: Matig weer, geen overschot
        else:
            message = f"""{emoji} *Energie Tip ({time_str})*

Beetje zon, maar niet genoeg voor extra's.

⚡ PV: {pv_now}W
🏠 Huis verbruik: {house_w}W
🔋 Batterijen: {battery_soc}%

💡 Tip: Alleen doen wat nodig is, of wacht op meer zon!

_myEnergy systeem_"""
        
        # Send to both
        await self.send_to_both(message)
    
    async def send_battery_warning(self, battery_id: str, soc: float, reason: str):
        """Send battery warning (only to Jos)"""
        message = f"""⚠️ *Batterij Waarschuwing*

🔋 {battery_id}: {soc}%
❗ {reason}

Check het dashboard!

_myEnergy systeem_"""
        
        await self.send_message("jos", message)

# Global instance
whatsapp = WhatsAppNotifier()
