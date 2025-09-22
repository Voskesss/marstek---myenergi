#!/usr/bin/env python3
"""
Quick BLE test with improved discovery
"""
import asyncio
import os

async def quick_test():
    os.environ["MARSTEK_USE_BLE"] = "true"
    
    try:
        from ble_client import get_ble_client
        
        print("🔍 Testing improved BLE discovery...")
        
        ble_client = get_ble_client()
        print(f"Initial device name: {ble_client.device_name}")
        
        # Test discovery
        found = await ble_client.discover_device()
        
        if found:
            print(f"✅ Found device: {ble_client.device_name}")
            print(f"   Address: {ble_client.device_address}")
            
            # Test connection
            print("🔗 Testing connection...")
            connected = await ble_client.connect()
            
            if connected:
                print("✅ Connected!")
                
                # Test battery status
                status = await ble_client.get_battery_status()
                print(f"📊 Status: {status}")
                
                await ble_client.disconnect()
            else:
                print("❌ Connection failed")
        else:
            print("❌ No Marstek devices found")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(quick_test())
