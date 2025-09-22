#!/usr/bin/env python3
"""
Test script for BLE Bridge integration
"""
import asyncio
import os
import sys

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_ble_bridge():
    """Test BLE bridge service"""
    print("🔋 Testing BLE Bridge Service")
    print("=" * 50)
    
    # Test 1: Direct BLE bridge endpoints
    print("\n1. Testing BLE bridge endpoints...")
    
    import httpx
    
    bridge_url = "http://localhost:8001"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Health check
            print("   Health check...", end=" ")
            r = await client.get(f"{bridge_url}/")
            print(f"✅ {r.status_code}")
            print(f"      {r.json()}")
            
            # Battery status
            print("   Battery status...", end=" ")
            r = await client.get(f"{bridge_url}/api/battery/status")
            print(f"✅ {r.status_code}")
            status = r.json()
            print(f"      SoC: {status.get('soc', 'N/A')}%")
            print(f"      Power: {status.get('power', 'N/A')}W")
            
            # System info
            print("   System info...", end=" ")
            r = await client.get(f"{bridge_url}/api/system/info")
            print(f"✅ {r.status_code}")
            info = r.json()
            print(f"      Device: {info.get('device_name', 'N/A')}")
            print(f"      Connected: {info.get('connected', 'N/A')}")
            
    except Exception as e:
        print(f"❌ BLE bridge error: {e}")
        print("   Make sure BLE bridge is running: python ble_bridge.py")
        return False
    
    # Test 2: FastAPI app with BLE bridge enabled
    print("\n2. Testing FastAPI app with BLE bridge...")
    
    # Set environment variable to use BLE bridge
    os.environ["MARSTEK_USE_BLE"] = "true"
    
    try:
        # Import after setting env var
        from app import MarstekClient, MARSTEK_BLE_BRIDGE, MARSTEK_USE_BLE
        
        print(f"   BLE Bridge URL: {MARSTEK_BLE_BRIDGE}")
        print(f"   Use BLE: {MARSTEK_USE_BLE}")
        
        # Create client
        marstek = MarstekClient("http://dummy", timeout=5.0)
        
        # Test get_overview with BLE bridge
        print("   Getting overview via BLE...", end=" ")
        overview = await marstek.get_overview()
        print("✅")
        print(f"      Data: {overview}")
        
        # Test individual methods
        soc = await marstek.get_soc()
        power = await marstek.get_power()
        
        print(f"   SoC: {soc}%")
        print(f"   Power: {power}W")
        
        return True
        
    except Exception as e:
        print(f"❌ FastAPI integration error: {e}")
        return False

async def test_full_integration():
    """Test full myenergi + marstek integration"""
    print("\n3. Testing full integration...")
    
    try:
        from app import app_state, update_status
        
        # Run one update cycle
        print("   Running status update...", end=" ")
        await update_status()
        print("✅")
        
        # Check app state
        state = app_state.get_snapshot()
        print(f"   Marstek SoC: {state.get('marstek_soc', 'N/A')}%")
        print(f"   Marstek Power: {state.get('marstek_power', 'N/A')}W")
        print(f"   Grid Export: {state.get('grid_export_w', 'N/A')}W")
        
        return True
        
    except Exception as e:
        print(f"❌ Integration error: {e}")
        return False

async def main():
    """Main test function"""
    print("🧪 BLE Bridge Integration Test")
    print("Make sure to start BLE bridge first: python ble_bridge.py")
    print()
    
    success = True
    
    # Test BLE bridge
    if not await test_ble_bridge():
        success = False
    
    # Test full integration
    if not await test_full_integration():
        success = False
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 All tests passed!")
        print("\nTo use BLE bridge in production:")
        print("1. Start BLE bridge: python ble_bridge.py")
        print("2. Set environment: export MARSTEK_USE_BLE=true")
        print("3. Start main app: uvicorn app:app --port 8000")
    else:
        print("❌ Some tests failed")
        print("\nTroubleshooting:")
        print("1. Make sure BLE bridge is running on port 8001")
        print("2. Check if Marstek battery is in BLE range")
        print("3. Verify BLE permissions on macOS")

if __name__ == "__main__":
    asyncio.run(main())
