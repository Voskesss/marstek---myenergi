#!/usr/bin/env python3
"""
Test script to run after rebooting battery with Local API enabled.
Tests both TCP and UDP on common ports.
"""
import socket
import json
import time

IP = "192.168.68.66"
PORTS = [30000, 30001, 8080]

def test_tcp(ip, port):
    """Test if TCP port is open and try HTTP request"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((ip, port))
        if result == 0:
            print(f"✅ TCP {port}: OPEN")
            # Try HTTP request
            try:
                sock.send(b"GET /status HTTP/1.1\r\nHost: " + ip.encode() + b"\r\n\r\n")
                response = sock.recv(1024)
                print(f"   HTTP response: {response[:100]}...")
            except:
                print("   HTTP request failed")
            return True
        else:
            print(f"❌ TCP {port}: closed")
            return False
    except Exception as e:
        print(f"❌ TCP {port}: error {e}")
        return False
    finally:
        sock.close()

def test_udp(ip, port):
    """Test UDP with Marstek commands"""
    commands = [
        {"id": 1, "method": "ES.GetStatus", "params": {"id": 0}},
        {"id": 2, "method": "Bat.GetStatus", "params": {"id": 0}},
        {"id": 3, "method": "Marstek.GetDevice", "params": {"ble_mac": "0"}}
    ]
    
    for cmd in commands:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3)
            sock.sendto(json.dumps(cmd).encode(), (ip, port))
            data, addr = sock.recvfrom(4096)
            print(f"✅ UDP {port}: Response to {cmd['method']}")
            print(f"   {data.decode()[:100]}...")
            return True
        except socket.timeout:
            continue
        except Exception as e:
            print(f"❌ UDP {port}: error {e}")
            break
        finally:
            sock.close()
    
    print(f"❌ UDP {port}: no response")
    return False

def main():
    print(f"🔍 Testing {IP} after battery reboot...")
    print("=" * 50)
    
    # First check if device is reachable
    import subprocess
    result = subprocess.run(['ping', '-c', '3', IP], capture_output=True)
    if result.returncode == 0:
        print("✅ Device is pingable")
    else:
        print("❌ Device not reachable - check network")
        return
    
    found_service = False
    
    for port in PORTS:
        print(f"\n--- Testing port {port} ---")
        if test_tcp(IP, port):
            found_service = True
        if test_udp(IP, port):
            found_service = True
    
    if found_service:
        print("\n🎉 Found working service! Update your app configuration.")
    else:
        print("\n❌ No services found. Try:")
        print("1. Wait longer after reboot (up to 5 minutes)")
        print("2. Check BLE: Read Local API (0x28) - should show Enabled=Yes")
        print("3. Try different port in BLE (30001, 8080)")
        print("4. Check if battery is on same network as laptop")

if __name__ == "__main__":
    main()
