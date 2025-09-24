#!/usr/bin/env python3
"""
Battery 78 Modbus-MQTT Bridge
Real-time bridge between Venus E battery 78 and MQTT dashboard
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Dict, Any

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusTcpClient

class Battery78ModbusBridge:
    def __init__(self, modbus_host='192.168.68.92', mqtt_host='localhost'):
        self.modbus_host = modbus_host
        self.modbus_port = 502
        self.mqtt_host = mqtt_host
        self.mqtt_port = 1883
        
        # Modbus client
        self.modbus_client = None
        self.modbus_connected = False
        
        # MQTT client
        self.mqtt_client = None
        self.mqtt_connected = False
        
        # Battery register mapping (discovered from scan)
        self.battery_registers = {
            30000: {"name": "soc_percent", "scale": 1, "unit": "%"},
            30001: {"name": "voltage_v", "scale": 0.01, "unit": "V"},  # 5375 = 53.75V
            30002: {"name": "current_a", "scale": 0.01, "unit": "A"},  # Signed value
            30003: {"name": "power_w", "scale": 1, "unit": "W"},
            30004: {"name": "temperature_c", "scale": 0.1, "unit": "°C"},
            30005: {"name": "status_code", "scale": 1, "unit": ""},
            30006: {"name": "mode", "scale": 1, "unit": ""},
            30008: {"name": "cycles", "scale": 1, "unit": ""},
            30009: {"name": "capacity_ah", "scale": 1, "unit": "Ah"}
        }
        
        # Control registers (to be discovered)
        self.control_registers = {
            "charge_mode": 42001,  # Estimated
            "max_charge_power": 42002,  # Estimated
            "force_charge": 42003  # Estimated
        }
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def connect_modbus(self) -> bool:
        """Connect to Modbus TCP server"""
        try:
            self.modbus_client = ModbusTcpClient(self.modbus_host, port=self.modbus_port)
            self.modbus_connected = self.modbus_client.connect()
            
            if self.modbus_connected:
                self.logger.info(f"✅ Modbus connected to battery 78 at {self.modbus_host}:{self.modbus_port}")
            else:
                self.logger.error(f"❌ Modbus connection failed to {self.modbus_host}:{self.modbus_port}")
            
            return self.modbus_connected
            
        except Exception as e:
            self.logger.error(f"❌ Modbus connection error: {e}")
            return False
    
    def connect_mqtt(self) -> bool:
        """Connect to MQTT broker"""
        try:
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
            
            # Wait for connection
            time.sleep(2)
            return self.mqtt_connected
            
        except Exception as e:
            self.logger.error(f"❌ MQTT connection error: {e}")
            return False
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.mqtt_connected = True
            self.logger.info("✅ MQTT connected")
            
            # Subscribe to battery 78 command topics
            topics = [
                "marstek/command/mode",
                "marstek/command/power",
                "marstek/command/charge",
                "marstek/status/request"
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
        
        self.logger.info(f"📥 MQTT command: {topic} = {payload}")
        
        # Handle battery commands
        asyncio.create_task(self._handle_battery_command(topic, payload))
    
    async def _handle_battery_command(self, topic: str, payload: str):
        """Handle battery control commands"""
        if not self.modbus_connected:
            self.logger.error("❌ Modbus not connected - cannot send command")
            return
        
        try:
            if "command/mode" in topic:
                await self._set_charge_mode(payload)
            elif "command/power" in topic:
                await self._set_charge_power(int(payload))
            elif "command/charge" in topic:
                await self._set_force_charge(payload == "true")
            elif "status/request" in topic:
                await self._publish_battery_status()
                
        except Exception as e:
            self.logger.error(f"❌ Command error: {e}")
    
    async def _set_charge_mode(self, mode: str):
        """Set battery charge mode"""
        mode_map = {
            "auto": 0,
            "force_charge": 1,
            "force_discharge": 2,
            "idle": 3
        }
        
        if mode.lower() in mode_map:
            mode_value = mode_map[mode.lower()]
            
            try:
                result = self.modbus_client.write_register(
                    address=self.control_registers["charge_mode"], 
                    value=mode_value, 
                    slave=1
                )
                
                if not result.isError():
                    self.logger.info(f"✅ Set charge mode to: {mode}")
                    self._publish_mqtt("marstek/status/mode", mode)
                else:
                    self.logger.error(f"❌ Failed to set mode: {result}")
                    
            except Exception as e:
                self.logger.error(f"❌ Mode command error: {e}")
    
    async def _set_charge_power(self, power_w: int):
        """Set charge/discharge power"""
        try:
            result = self.modbus_client.write_register(
                address=self.control_registers["max_charge_power"], 
                value=power_w, 
                slave=1
            )
            
            if not result.isError():
                self.logger.info(f"✅ Set charge power to: {power_w}W")
                self._publish_mqtt("marstek/status/power", str(power_w))
            else:
                self.logger.error(f"❌ Failed to set power: {result}")
                
        except Exception as e:
            self.logger.error(f"❌ Power command error: {e}")
    
    async def _set_force_charge(self, force: bool):
        """Set force charge mode"""
        try:
            result = self.modbus_client.write_register(
                address=self.control_registers["force_charge"], 
                value=1 if force else 0, 
                slave=1
            )
            
            if not result.isError():
                self.logger.info(f"✅ Set force charge: {force}")
                self._publish_mqtt("marstek/status/force_charge", str(force).lower())
            else:
                self.logger.error(f"❌ Failed to set force charge: {result}")
                
        except Exception as e:
            self.logger.error(f"❌ Force charge error: {e}")
    
    def read_battery_data(self) -> Dict[str, Any]:
        """Read all battery data from Modbus"""
        if not self.modbus_connected:
            return {}
        
        battery_data = {}
        
        for reg_addr, reg_info in self.battery_registers.items():
            try:
                result = self.modbus_client.read_holding_registers(
                    address=reg_addr, count=1, slave=1
                )
                
                if hasattr(result, 'registers') and not result.isError():
                    raw_value = result.registers[0]
                    
                    # Handle signed values (current can be negative)
                    if reg_info["name"] == "current_a" and raw_value > 32767:
                        raw_value = raw_value - 65536  # Convert to signed 16-bit
                    
                    # Apply scaling
                    scaled_value = raw_value * reg_info["scale"]
                    
                    battery_data[reg_info["name"]] = {
                        "value": scaled_value,
                        "raw": raw_value,
                        "unit": reg_info["unit"],
                        "register": reg_addr,
                        "timestamp": datetime.now().isoformat()
                    }
                    
            except Exception as e:
                self.logger.error(f"❌ Error reading register {reg_addr}: {e}")
        
        return battery_data
    
    async def _publish_battery_status(self):
        """Read and publish battery status to MQTT"""
        battery_data = self.read_battery_data()
        
        if battery_data:
            # Publish individual values
            for param, data in battery_data.items():
                topic = f"marstek/status/{param}"
                self._publish_mqtt(topic, str(data["value"]))
            
            # Publish complete status as JSON
            self._publish_mqtt("marstek/status/full", json.dumps(battery_data, indent=2))
            
            self.logger.info(f"📊 Published battery status: SoC={battery_data.get('soc_percent', {}).get('value', 'N/A')}%")
    
    def _publish_mqtt(self, topic: str, payload: str):
        """Publish message to MQTT"""
        if self.mqtt_connected and self.mqtt_client:
            self.mqtt_client.publish(topic, payload)
            self.logger.debug(f"📤 MQTT: {topic} = {payload}")
    
    async def run_status_loop(self):
        """Main loop - publish battery status every 10 seconds"""
        self.logger.info("🔄 Starting battery status loop...")
        
        while True:
            try:
                if self.modbus_connected and self.mqtt_connected:
                    await self._publish_battery_status()
                else:
                    self.logger.warning("⚠️ Connections not ready")
                
                await asyncio.sleep(10)  # Every 10 seconds
                
            except Exception as e:
                self.logger.error(f"❌ Status loop error: {e}")
                await asyncio.sleep(30)
    
    def run(self):
        """Run the bridge"""
        self.logger.info("🚀 Starting Battery 78 Modbus-MQTT Bridge")
        
        # Connect to services
        if not self.connect_modbus():
            self.logger.error("❌ Cannot start without Modbus connection")
            return
        
        if not self.connect_mqtt():
            self.logger.error("❌ Cannot start without MQTT connection")
            return
        
        self.logger.info("✅ All connections established - starting bridge")
        
        try:
            # Run the status loop
            asyncio.run(self.run_status_loop())
            
        except KeyboardInterrupt:
            self.logger.info("🛑 Shutting down bridge...")
            
            if self.modbus_client:
                self.modbus_client.close()
            
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()

if __name__ == "__main__":
    bridge = Battery78ModbusBridge()
    bridge.run()
