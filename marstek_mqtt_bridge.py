#!/usr/bin/env python3
"""
Marstek MQTT Bridge
Bridges MQTT commands to Marstek battery control (BLE/UDP)
Based on SlimLaden Homey app approach
"""

import asyncio
import json
import logging
import time
from typing import Dict, Any, Optional

import paho.mqtt.client as mqtt

# Import our existing clients
try:
    from marstek_client import MarstekClient
except ImportError:
    MarstekClient = None

from marstek_udp_client import MarstekUDPClient

# Try to import BLE client
try:
    import sys
    sys.path.insert(0, '.')
    from external.marstek_venus_monitor.ble_client import BLEClient
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False

class MarstekMQTTBridge:
    def __init__(self, mqtt_host: str = "localhost", mqtt_port: int = 1883):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client = None
        
        # Battery clients
        self.batteries = {
            "marstek": {  # Battery 1
                "ip": "192.168.68.78",
                "port": 30000,
                "udp_client": None,
                "ble_client": None,
                "http_client": None
            },
            "marsteka": {  # Battery 2  
                "ip": "192.168.68.66",
                "port": 30000,
                "udp_client": None,
                "ble_client": None,
                "http_client": None
            }
        }
        
        # Initialize clients
        self._init_clients()
        
        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def _init_clients(self):
        """Initialize battery clients"""
        for name, config in self.batteries.items():
            # UDP client
            config["udp_client"] = MarstekUDPClient(config["ip"], config["port"])
            
            # HTTP client (fallback)
            if MarstekClient:
                config["http_client"] = MarstekClient(f"http://{config['ip']}:30000", "")
            else:
                config["http_client"] = None
            
            # BLE client (if available)
            if BLE_AVAILABLE:
                config["ble_client"] = BLEClient()
    
    def setup_mqtt(self):
        """Setup MQTT client"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            return True
        except Exception as e:
            self.logger.error(f"MQTT connection failed: {e}")
            return False
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.logger.info("✅ MQTT connected")
            
            # Subscribe to battery command topics
            for battery_name in self.batteries.keys():
                topics = [
                    f"{battery_name}/command/mode",
                    f"{battery_name}/command/power", 
                    f"{battery_name}/command/soc",
                    f"{battery_name}/status/request"
                ]
                
                for topic in topics:
                    client.subscribe(topic)
                    self.logger.info(f"📡 Subscribed to {topic}")
        else:
            self.logger.error(f"❌ MQTT connection failed: {rc}")
    
    def _on_mqtt_message(self, client, userdata, msg):
        """MQTT message callback"""
        topic = msg.topic
        payload = msg.payload.decode('utf-8')
        
        self.logger.info(f"📥 MQTT: {topic} = {payload}")
        
        # Parse topic: battery_name/command/action
        parts = topic.split('/')
        if len(parts) >= 3:
            battery_name = parts[0]
            command_type = parts[1]  # command or status
            action = parts[2]        # mode, power, soc, request
            
            if battery_name in self.batteries:
                asyncio.create_task(self._handle_battery_command(
                    battery_name, command_type, action, payload
                ))
    
    async def _handle_battery_command(self, battery_name: str, command_type: str, 
                                    action: str, payload: str):
        """Handle battery command via MQTT"""
        battery = self.batteries[battery_name]
        
        try:
            if command_type == "command":
                if action == "mode":
                    await self._set_battery_mode(battery_name, payload)
                elif action == "power":
                    await self._set_battery_power(battery_name, int(payload))
                elif action == "soc":
                    await self._get_battery_soc(battery_name)
                    
            elif command_type == "status" and action == "request":
                await self._get_battery_status(battery_name)
                
        except Exception as e:
            self.logger.error(f"❌ Command failed for {battery_name}: {e}")
            self._publish_error(battery_name, str(e))
    
    async def _set_battery_mode(self, battery_name: str, mode: str):
        """Set battery mode: auto, charge, discharge, idle"""
        battery = self.batteries[battery_name]
        
        # Try UDP first
        udp_client = battery["udp_client"]
        
        mode_map = {
            "auto": "Auto",
            "charge": "Manual", 
            "discharge": "Manual",
            "idle": "Passive"
        }
        
        marstek_mode = mode_map.get(mode.lower(), "Auto")
        
        if marstek_mode == "Manual":
            # Set manual mode with power
            power = 1000 if mode == "charge" else -1000  # 1kW charge/discharge
            result = await udp_client.call("ES.SetMode", {
                "id": 0,
                "config": {
                    "mode": "Manual",
                    "manual_cfg": {
                        "time_num": 1,
                        "start_time": "00:00",
                        "end_time": "23:59", 
                        "week_set": 127,  # All days
                        "power": power,
                        "enable": 1
                    }
                }
            })
        elif marstek_mode == "Passive":
            result = await udp_client.es_set_mode_passive(0, 0)
        else:
            # Auto mode
            result = await udp_client.call("ES.SetMode", {
                "id": 0,
                "config": {
                    "mode": "Auto",
                    "auto_cfg": {"enable": 1}
                }
            })
        
        if result:
            self._publish_status(battery_name, "mode", mode)
            self.logger.info(f"✅ {battery_name} mode set to {mode}")
        else:
            # Fallback to BLE
            await self._fallback_ble_command(battery_name, "mode", mode)
    
    async def _set_battery_power(self, battery_name: str, power: int):
        """Set battery power (W) - positive=charge, negative=discharge"""
        battery = self.batteries[battery_name]
        
        # Use passive mode with specific power
        udp_client = battery["udp_client"]
        result = await udp_client.es_set_mode_passive(power, 3600)  # 1 hour
        
        if result:
            self._publish_status(battery_name, "power", str(power))
            self.logger.info(f"✅ {battery_name} power set to {power}W")
        else:
            await self._fallback_ble_command(battery_name, "power", power)
    
    async def _get_battery_soc(self, battery_name: str):
        """Get battery State of Charge"""
        battery = self.batteries[battery_name]
        
        udp_client = battery["udp_client"]
        result = await udp_client.bat_get_status()
        
        if result and "result" in result:
            soc = result["result"].get("soc")
            if soc is not None:
                self._publish_status(battery_name, "soc", str(soc))
                self.logger.info(f"✅ {battery_name} SoC: {soc}%")
                return
        
        # Fallback to BLE
        await self._fallback_ble_command(battery_name, "soc", None)
    
    async def _get_battery_status(self, battery_name: str):
        """Get full battery status"""
        battery = self.batteries[battery_name]
        
        udp_client = battery["udp_client"]
        
        # Get multiple status calls
        bat_status = await udp_client.bat_get_status()
        es_status = await udp_client.es_get_status()
        es_mode = await udp_client.es_get_mode()
        
        status = {
            "timestamp": time.time(),
            "battery": bat_status,
            "energy_system": es_status,
            "mode": es_mode
        }
        
        self._publish_status(battery_name, "full_status", json.dumps(status))
        self.logger.info(f"✅ {battery_name} full status published")
    
    async def _fallback_ble_command(self, battery_name: str, command: str, value):
        """Fallback to BLE if UDP fails"""
        battery = self.batteries[battery_name]
        
        self.logger.info(f"🔵 Fallback to BLE for {battery_name} {command}")
        
        try:
            # Use FastAPI BLE endpoints as fallback
            import aiohttp
            async with aiohttp.ClientSession() as session:
                if command == "mode":
                    # Set battery mode via BLE
                    if value == "charge":
                        url = "http://localhost:8000/api/battery/force-charge"
                    elif value == "discharge":
                        url = "http://localhost:8000/api/battery/force-discharge"
                    elif value == "auto":
                        url = "http://localhost:8000/api/battery/allow-charge"
                    else:
                        url = "http://localhost:8000/api/battery/allow-charge"
                    
                    async with session.post(url) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            if result.get("ok"):
                                self._publish_status(battery_name, "mode", value)
                                self.logger.info(f"✅ BLE {command} success for {battery_name}")
                                return
                
                elif command == "soc":
                    # Get battery SoC via BLE
                    async with session.get("http://localhost:8000/api/battery/status") as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            soc = result.get("soc")
                            if soc is not None:
                                self._publish_status(battery_name, "soc", str(soc))
                                self.logger.info(f"✅ BLE SoC: {soc}% for {battery_name}")
                                return
                
                elif command == "power":
                    # Set battery power via BLE (limited support)
                    self.logger.info(f"⚠️ BLE power control not implemented for {battery_name}")
                    
        except Exception as e:
            self.logger.error(f"❌ BLE fallback failed for {battery_name}: {e}")
        
        self._publish_error(battery_name, f"Both UDP and BLE failed for {command}")
    
    def _publish_status(self, battery_name: str, key: str, value: str):
        """Publish status to MQTT"""
        topic = f"{battery_name}/status/{key}"
        if self.mqtt_client:
            self.mqtt_client.publish(topic, value)
            self.logger.debug(f"📤 MQTT: {topic} = {value}")
    
    def _publish_error(self, battery_name: str, error: str):
        """Publish error to MQTT"""
        topic = f"{battery_name}/error"
        if self.mqtt_client:
            self.mqtt_client.publish(topic, error)
    
    async def start_status_loop(self):
        """Periodically publish battery status"""
        while True:
            try:
                for battery_name in self.batteries.keys():
                    await self._get_battery_status(battery_name)
                
                await asyncio.sleep(30)  # Every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Status loop error: {e}")
                await asyncio.sleep(60)
    
    def run(self):
        """Run the MQTT bridge"""
        if not self.setup_mqtt():
            return
        
        self.logger.info("🚀 Starting Marstek MQTT Bridge")
        
        # Start MQTT loop in background
        self.mqtt_client.loop_start()
        
        # Start status publishing loop
        try:
            asyncio.run(self.start_status_loop())
        except KeyboardInterrupt:
            self.logger.info("🛑 Shutting down...")
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    bridge = MarstekMQTTBridge()
    bridge.run()
