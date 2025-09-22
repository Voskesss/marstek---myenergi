#!/usr/bin/env python3
"""
Test integrated BLE functionality in main app
"""
import asyncio
import os
import sys

async def test_integrated_ble():
    """Test integrated BLE in main app"""
    print("🔋 Testing Integrated BLE in Main App")
    print("=" * 50)
    
    # Set BLE mode
    os.environ["MARSTEK_USE_BLE"] = "true"
    
    # Test import
    try:
        from ble_client import get_ble_client
        print("✅ BLE client import successful")
    except ImportError as e:
        print(f"❌ BLE import failed: {e}")
        print("   Run: pip install bleak")
        return False
    
    # Test BLE client
    try:
        ble_client = get_ble_client()
        print(f"✅ BLE client created: {ble_client.device_name}")
        
        # Test discovery
        print("🔍 Discovering device...", end=" ")
        found = await ble_client.discover_device()
        if found:
            print(f"✅ Found at {ble_client.device_address}")
        else:
            print("❌ Not found")
            return False
        
        # Test connection
        print("🔗 Connecting...", end=" ")
        connected = await ble_client.connect()
        if connected:
            print("✅ Connected")
        else:
            print("❌ Connection failed")
            return False
        
        # Test battery status
        print("📊 Getting battery status...", end=" ")
        status = await ble_client.get_battery_status()
        print("✅")
        print(f"   SoC: {status.get('soc', 'N/A')}%")
        print(f"   Power: {status.get('power', 'N/A')}W")
        print(f"   Connected: {status.get('connected', 'N/A')}")
        
        # Test system info
        print("ℹ️  Getting system info...", end=" ")
        info = await ble_client.get_system_info()
        print("✅")
        print(f"   Device: {info.get('device_name', 'N/A')}")
        print(f"   Address: {info.get('ble_address', 'N/A')}")
        
        # Cleanup
        await ble_client.disconnect()
        print("✅ Disconnected")
        
        return True
        
    except Exception as e:
        print(f"❌ BLE test error: {e}")
        return False

async def test_app_integration():
    """Test app integration with BLE"""
    print("\n🚀 Testing App Integration")
    print("=" * 30)
    
    # Set BLE mode
    os.environ["MARSTEK_USE_BLE"] = "true"
    
    try:
        # Import app components
        from app import MarstekClient, MARSTEK_USE_BLE, BLE_AVAILABLE
        
        print(f"✅ App imports successful")
        print(f"   MARSTEK_USE_BLE: {MARSTEK_USE_BLE}")
        print(f"   BLE_AVAILABLE: {BLE_AVAILABLE}")
        
        if not BLE_AVAILABLE:
            print("❌ BLE not available in app")
            return False
        
        # Test MarstekClient with BLE
        marstek = MarstekClient("http://dummy")
        
        print("📊 Testing get_overview...", end=" ")
        overview = await marstek.get_overview()
        print("✅")
        print(f"   Source: {overview.get('source', 'N/A')}")
        print(f"   SoC: {overview.get('soc', 'N/A')}%")
        print(f"   Power: {overview.get('batt_power', 'N/A')}W")
        
        # Test individual methods
        soc = await marstek.get_soc()
        power = await marstek.get_power()
        
        print(f"📈 Individual methods:")
        print(f"   SoC: {soc}%")
        print(f"   Power: {power}W")
        
        return True
        
    except Exception as e:
        print(f"❌ App integration error: {e}")
        return False

async def main():
    """Main test function"""
    print("🧪 Integrated BLE Test")
    print("Make sure Marstek battery is nearby and powered on")
    print()
    
    success = True
    
    # Test BLE client directly
    if not await test_integrated_ble():
        success = False
    
    # Test app integration
    if not await test_app_integration():
        success = False
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 All tests passed!")
        print("\nTo run with integrated BLE:")
        print("1. Set environment: export MARSTEK_USE_BLE=true")
        print("2. Start app: uvicorn app:app --port 8000")
        print("3. Test: curl http://localhost:8000/api/ble/status")
    else:
        print("❌ Some tests failed")
        print("\nTroubleshooting:")
        print("1. Install BLE: pip install bleak")
        print("2. Check Marstek battery is on and nearby")
        print("3. Verify BLE permissions on macOS")

if __name__ == "__main__":
    asyncio.run(main())
