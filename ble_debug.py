#!/usr/bin/env python3
"""
BLE Debug - Scan for all Marstek devices and show details
"""
import asyncio
from bleak import BleakScanner

async def scan_all_devices():
    """Scan for all BLE devices and show Marstek ones"""
    print("🔍 Scanning for ALL BLE devices...")
    print("=" * 50)
    
    try:
        devices = await BleakScanner.discover(timeout=15.0)
        
        print(f"Found {len(devices)} total BLE devices:")
        print()
        
        marstek_devices = []
        all_devices = []
        
        for device in devices:
            name = device.name or "Unknown"
            address = device.address
            rssi = getattr(device, 'rssi', 'N/A')
            
            all_devices.append((name, address, rssi))
            
            # Check for Marstek devices (various possible names)
            if any(keyword in name.upper() for keyword in ['MST', 'MARSTEK', 'ACCP', 'TPM']):
                marstek_devices.append((name, address, rssi))
                print(f"🎯 MARSTEK DEVICE: {name}")
                print(f"   Address: {address}")
                print(f"   RSSI: {rssi}")
                print()
        
        if not marstek_devices:
            print("❌ No Marstek devices found")
            print("\n📋 All devices found:")
            for name, address, rssi in sorted(all_devices):
                print(f"   {name:20} | {address} | RSSI: {rssi}")
        else:
            print(f"✅ Found {len(marstek_devices)} Marstek device(s)")
            
        return marstek_devices
        
    except Exception as e:
        print(f"❌ Scan error: {e}")
        return []

async def test_specific_device(address, name):
    """Test connection to specific device"""
    print(f"\n🔗 Testing connection to {name} ({address})")
    
    try:
        from bleak import BleakClient
        
        client = BleakClient(address)
        connected = await client.connect()
        
        if connected:
            print("✅ Connected successfully!")
            
            # List services
            services = await client.get_services()
            print(f"📋 Services ({len(services)}):")
            
            for service in services:
                print(f"   {service.uuid}: {service.description}")
                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print(f"     └─ {char.uuid}: {props}")
            
            await client.disconnect()
            print("✅ Disconnected")
            return True
        else:
            print("❌ Connection failed")
            return False
            
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False

async def main():
    """Main debug function"""
    print("🔵 Marstek BLE Debug Tool")
    print("Make sure:")
    print("1. Bluetooth is ON")
    print("2. Marstek battery is ON and nearby")
    print("3. Battery is NOT connected to phone/tablet")
    print()
    
    # Scan for devices
    marstek_devices = await scan_all_devices()
    
    if marstek_devices:
        print("\n🧪 Testing connections...")
        for name, address, rssi in marstek_devices:
            await test_specific_device(address, name)
    else:
        print("\n💡 Troubleshooting suggestions:")
        print("1. Check if battery is ON (LED indicators)")
        print("2. Disconnect battery from phone/tablet if connected")
        print("3. Try power cycling the battery (off/on)")
        print("4. Move closer to battery (within 2-3 meters)")
        print("5. Check macOS Bluetooth permissions:")
        print("   System Preferences > Security & Privacy > Privacy > Bluetooth")

if __name__ == "__main__":
    asyncio.run(main())
