#!/usr/bin/env python3
"""
Debug UDP communication with Marstek Venus E battery.
Tests multiple commands and shows detailed debug info.
"""
import socket
import json
import time
import binascii

IP = "192.168.68.66"
PORT = 30000
TIMEOUT = 5.0

# Test commands from Marstek API documentation
COMMANDS = [
    {"id": 1, "method": "ES.GetStatus", "params": {"id": 0}},
    {"id": 2, "method": "Bat.GetStatus", "params": {"id": 0}},
    {"id": 3, "method": "Wifi.GetStatus", "params": {"id": 0}},
    {"id": 4, "method": "Marstek.GetDevice", "params": {"ble_mac": "0"}},
    {"id": 5, "method": "BLE.GetStatus", "params": {"id": 0}},
]

def test_udp_command(cmd):
    print(f"\n--- Testing: {cmd['method']} ---")
    
    try:
        # Create socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(TIMEOUT)
        
        # Send command
        message = json.dumps(cmd).encode('utf-8')
        print(f"Sending to {IP}:{PORT}")
        print(f"Payload: {cmd}")
        print(f"Raw bytes: {binascii.hexlify(message).decode()}")
        
        sock.sendto(message, (IP, PORT))
        print("✅ Sent successfully")
        
        # Wait for response
        print("Waiting for response...")
        data, addr = sock.recvfrom(4096)
        
        print(f"🎉 Response from {addr}:")
        print(f"Raw bytes: {binascii.hexlify(data).decode()}")
        
        try:
            response = json.loads(data.decode('utf-8'))
            print(f"JSON: {json.dumps(response, indent=2)}")
            return True
        except json.JSONDecodeError:
            print(f"Raw text: {data.decode('utf-8', errors='replace')}")
            return True
            
    except socket.timeout:
        print("❌ Timeout - no response received")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    finally:
        try:
            sock.close()
        except:
            pass

def main():
    print("🔍 Marstek Venus E UDP Debug Test")
    print(f"Target: {IP}:{PORT}")
    print("=" * 50)
    
    # Test network connectivity first
    print("\n🌐 Testing network connectivity...")
    import subprocess
    result = subprocess.run(['ping', '-c', '2', IP], capture_output=True, text=True)
    if result.returncode == 0:
        print("✅ Device is reachable via ping")
    else:
        print("❌ Device not reachable - check network")
        return
    
    # Test each command
    success_count = 0
    for cmd in COMMANDS:
        if test_udp_command(cmd):
            success_count += 1
        time.sleep(1)  # Small delay between commands
    
    print(f"\n📊 Summary: {success_count}/{len(COMMANDS)} commands successful")
    
    if success_count == 0:
        print("\n🔧 Troubleshooting suggestions:")
        print("1. Wait 2-3 more minutes after enabling Local API")
        print("2. Try physical power cycle of battery (unplug/plug)")
        print("3. Check if firewall is blocking UDP traffic")
        print("4. Verify battery and laptop are on same network segment")
        print("5. Try from a different device/network location")

if __name__ == "__main__":
    main()
