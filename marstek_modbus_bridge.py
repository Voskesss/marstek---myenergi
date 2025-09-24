#!/usr/bin/env python3
"""
Marstek MQTT-Modbus Bridge
Bridges MQTT commands to Modbus TCP (RS-485)
"""

import asyncio
import json
import logging
import time
from typing import Dict, Any

import paho.mqtt.client as mqtt
from marstek_modbus_client import MarstekModbusClient

class MarstekModbusBridge:
    def __init__(self, mqtt_host: str = "localhost", mqtt_port: int = 1883):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client = None
        
        # Modbus clients for each battery
        self.batteries = {
            "marstek": {
                "ip": "192.168.68.100",  # IP of RS-485 converter 1
                "port": 502,
                "unit_id": 1,
                "modbus_client": None
            },
            "marsteka": {
                "ip": "192.168.68.101",  # IP of RS-485 converter 2  
                "port": 502,
                "unit_id": 1,
                "modbus_client": None
            }
        }
        
        self._init_modbus_clients()
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def _init_modbus_clients(self):
        """Initialize Modbus clients"""
        for name, config in self.batteries.items():
            config["modbus_client"] = MarstekModbusClient(
                config["ip"], 
                config["port"], 
                config["unit_id"]
            )
    
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
        
        parts = topic.split('/')
        if len(parts) >= 3:
            battery_name = parts[0]
            command_type = parts[1]
            action = parts[2]
            
            if battery_name in self.batteries:
                asyncio.create_task(self._handle_modbus_command(
                    battery_name, command_type, action, payload
                ))
    
    async def _handle_modbus_command(self, battery_name: str, command_type: str, 
                                   action: str, payload: str):
        """Handle battery command via Modbus"""
        battery = self.batteries[battery_name]
        modbus_client = battery["modbus_client"]
        
        try:
            # Connect if not connected
            if not modbus_client.connected:
                if not modbus_client.connect():
                    self._publish_error(battery_name, "Modbus connection failed")
                    return
            
            if command_type == "command":
                if action == "mode":
                    success = modbus_client.set_charge_mode(payload)
                    if success:
                        self._publish_status(battery_name, "mode", payload)
                        
                elif action == "power":
                    power = int(payload)
                    success = modbus_client.set_charge_power(power)
                    if success:
                        self._publish_status(battery_name, "power", payload)
                        
            elif command_type == "status" and action == "request":
                status = modbus_client.get_battery_status()
                if status:
                    self._publish_status(battery_name, "full_status", json.dumps(status))
                    
                    # Publish individual values
                    if status.get("soc"):
                        self._publish_status(battery_name, "soc", str(status["soc"]))
                    if status.get("power"):
                        self._publish_status(battery_name, "power", str(status["power"]))
                
        except Exception as e:
            self.logger.error(f"❌ Modbus command failed for {battery_name}: {e}")
            self._publish_error(battery_name, str(e))
    
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
                    await self._handle_modbus_command(
                        battery_name, "status", "request", "get_status"
                    )
                
                await asyncio.sleep(30)  # Every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Status loop error: {e}")
                await asyncio.sleep(60)
    
    def run(self):
        """Run the MQTT-Modbus bridge"""
        if not self.setup_mqtt():
            return
        
        self.logger.info("🚀 Starting Marstek MQTT-Modbus Bridge")
        
        # Start MQTT loop in background
        self.mqtt_client.loop_start()
        
        # Start status publishing loop
        try:
            asyncio.run(self.start_status_loop())
        except KeyboardInterrupt:
            self.logger.info("🛑 Shutting down...")
            
            # Disconnect Modbus clients
            for battery in self.batteries.values():
                if battery["modbus_client"]:
                    battery["modbus_client"].disconnect()
            
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    bridge = MarstekModbusBridge()
    bridge.run()
