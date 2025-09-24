#!/usr/bin/env python3
"""
Multi-battery discovery tool voor Marstek batterijen
Zoekt naar batterijen via BLE en network
"""
import asyncio
import socket
import struct
import time
from typing import List, Dict, Any

# BLE imports (optional)
try:
    from bleak import BleakScanner
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False
    print("⚠️  BLE not available (install: pip install bleak)")

class BatteryDiscovery:
    def __init__(self):
        self.discovered_batteries = []
    
    async def discover_ble_batteries(self) -> List[Dict[str, Any]]:
        """Discover Marstek batteries via BLE"""
        if not BLE_AVAILABLE:
            return []
        
        print("🔍 Scanning for BLE batteries...")
        batteries = []
        
        try:
            devices = await BleakScanner.discover(timeout=15.0)
            
            # Look for Marstek devices
            marstek_keywords = ['ACCP', 'MST_ACCP', 'MST-ACCP', 'MST_SMR']
            
            for device in devices:
                if device.name:
                    name_upper = device.name.upper()
                    if any(keyword in name_upper for keyword in marstek_keywords):
                        battery_type = "ACCP" if "ACCP" in name_upper else "SMR"
                        batteries.append({
                            "type": "BLE",
                            "name": device.name,
                            "address": device.address,
                            "battery_type": battery_type,
                            "rssi": device.rssi if hasattr(device, 'rssi') else None
                        })
                        print(f"✅ Found BLE battery: {device.name} ({device.address}) - {battery_type}")
            
            if not batteries:
                print("❌ No BLE batteries found")
                
        except Exception as e:
            print(f"❌ BLE scan error: {e}")
        
        return batteries
    
    def discover_network_batteries(self, network_range: str = "192.168.68") -> List[Dict[str, Any]]:
        """Discover Marstek batteries via network (port 30000)"""
        print(f"🔍 Scanning network {network_range}.1-254 for batteries...")
        batteries = []
        
        # Common Marstek ports
        ports = [30000, 8080, 80]
        
        # Limit scan range for faster discovery
        scan_range = range(1, 100)  # Only scan first 100 IPs for speed
        
        for i in scan_range:
            ip = f"{network_range}.{i}"
            for port in ports:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.3)  # Even faster scan
                    result = sock.connect_ex((ip, port))
                    sock.close()
                    
                    if result == 0:
                        # Try to get device info
                        device_info = self.get_device_info(ip, port)
                        if device_info:
                            batteries.append({
                                "type": "Network",
                                "ip": ip,
                                "port": port,
                                "info": device_info
                            })
                            print(f"✅ Found network battery: {ip}:{port}")
                            break  # Found on this IP, try next IP
                        
                except Exception:
                    pass
        
        if not batteries:
            print("❌ No network batteries found")
            
        return batteries
    
    def get_device_info(self, ip: str, port: int) -> Dict[str, Any]:
        """Try to get device information via HTTP"""
        import requests
        
        base_url = f"http://{ip}:{port}"
        endpoints = ["/api/info", "/info", "/api/status", "/status", "/"]
        
        for endpoint in endpoints:
            try:
                response = requests.get(f"{base_url}{endpoint}", timeout=2)
                if response.status_code == 200:
                    try:
                        return {"endpoint": endpoint, "data": response.json()}
                    except:
                        return {"endpoint": endpoint, "data": response.text[:100]}
            except:
                continue
        
        return {"reachable": True}
    
    async def discover_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Discover all batteries (BLE + Network)"""
        print("🔋 Starting battery discovery...")
        print("=" * 50)
        
        # BLE discovery
        ble_batteries = await self.discover_ble_batteries()
        
        # Network discovery
        network_batteries = self.discover_network_batteries()
        
        # Try different network ranges if needed
        if not network_batteries:
            print("🔍 Trying different network ranges...")
            for network in ["192.168.1", "192.168.0", "10.0.0"]:
                network_batteries.extend(self.discover_network_batteries(network))
                if network_batteries:
                    break
        
        result = {
            "ble": ble_batteries,
            "network": network_batteries,
            "total": len(ble_batteries) + len(network_batteries)
        }
        
        print("=" * 50)
        print(f"🎯 Discovery complete: {result['total']} batteries found")
        print(f"   BLE: {len(ble_batteries)}")
        print(f"   Network: {len(network_batteries)}")
        
        return result

async def main():
    """Main discovery function"""
    discovery = BatteryDiscovery()
    
    print("🔋 Marstek Battery Discovery Tool")
    print("=" * 50)
    
    # Discover all batteries
    batteries = await discovery.discover_all()
    
    # Print detailed results
    print("\n📊 Detailed Results:")
    print("-" * 30)
    
    if batteries["ble"]:
        print("\n🔵 BLE Batteries:")
        for i, battery in enumerate(batteries["ble"], 1):
            print(f"  {i}. {battery['name']} ({battery['battery_type']})")
            print(f"     Address: {battery['address']}")
            if battery['rssi']:
                print(f"     Signal: {battery['rssi']} dBm")
    
    if batteries["network"]:
        print("\n🌐 Network Batteries:")
        for i, battery in enumerate(batteries["network"], 1):
            print(f"  {i}. {battery['ip']}:{battery['port']}")
            if 'info' in battery and 'endpoint' in battery['info']:
                print(f"     Endpoint: {battery['info']['endpoint']}")
    
    if batteries["total"] == 0:
        print("\n❌ No batteries found!")
        print("\nTroubleshooting:")
        print("- Check if batteries are powered on")
        print("- Verify network connectivity")
        print("- Try different network ranges")
        print("- Enable Bluetooth for BLE discovery")
    
    return batteries

if __name__ == "__main__":
    asyncio.run(main())
