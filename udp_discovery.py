#!/usr/bin/env python3
"""
UDP Discovery for Marstek devices based on official API documentation.
Sends broadcast discovery and listens for responses.
"""
import socket
import json
import time
import binascii

# Config
BROADCAST = "192.168.68.255"    # LAN broadcast - adjust if needed
PORTS = [30000, 30001, 49152]   # try default and alternatives
TIMEOUT = 5.0

# Discovery command from Marstek API docs
DISCOVER = {
    "id": 0,
    "method": "Marstek.GetDevice", 
    "params": {"ble_mac": "0"}  # "0" means find any device
}

def hexdump(data: bytes) -> str:
    return binascii.hexlify(data).decode()

def discover_on_port(broadcast: str, port: int):
    print(f"\n=== Discovery on {broadcast}:{port} ===")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(TIMEOUT)
    
    try:
        # Send discovery broadcast
        message = json.dumps(DISCOVER).encode('utf-8')
        print(f"Sending: {DISCOVER}")
        sock.sendto(message, (broadcast, port))
        
        # Listen for responses
        found = []
        end_time = time.time() + TIMEOUT
        
        while time.time() < end_time:
            try:
                data, addr = sock.recvfrom(8192)
                print(f"\n🎉 Response from {addr}:")
                
                try:
                    response = json.loads(data.decode('utf-8'))
                    print("JSON:", json.dumps(response, indent=2))
                    found.append((addr, response))
                except json.JSONDecodeError:
                    print("Raw data:", data.decode('utf-8', errors='replace'))
                    print("Hex:", hexdump(data))
                    found.append((addr, data))
                    
            except socket.timeout:
                break
                
        if not found:
            print("No responses received")
            
        return found
        
    finally:
        sock.close()

def main():
    print("🔍 Marstek UDP Discovery")
    print("=" * 50)
    
    all_found = []
    
    for port in PORTS:
        found = discover_on_port(BROADCAST, port)
        all_found.extend(found)
    
    print(f"\n📊 Summary: Found {len(all_found)} device(s)")
    
    if all_found:
        print("\n✅ Next steps:")
        print("1. Note the IP and port from the response")
        print("2. Update your testjos.py with the correct IP/port")
        print("3. Try direct UDP communication to that device")
    else:
        print("\n❌ No devices found. Check:")
        print("1. Is Open API enabled in Marstek mobile app?")
        print("2. Is UDP port configured in the app?")
        print("3. Did you restart the battery after enabling?")
        print("4. Are laptop and battery on same network/SSID?")
        print("5. Try different broadcast address if needed")

if __name__ == "__main__":
    main()
