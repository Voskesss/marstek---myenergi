#!/usr/bin/env python3
"""
Quick port scanner for Marstek batteries
Tests common ports for Local API responses
"""

import socket
import json
import time

def quick_port_scan(ip, timeout=1):
    """Scan common ports for UDP responses"""
    print(f'🔍 Quick scanning {ip}')
    
    # Common battery system ports
    ports = [
        30000, 30001, 30002, 30003, 30004, 30005,  # Marstek range
        8080, 8081, 8000,                          # Web ports
        502, 503,                                  # Modbus
        1883,                                      # MQTT
        49152, 50000                               # High ports
    ]
    
    responding_ports = []
    
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        
        try:
            # Simple test
            sock.sendto(b'ping', (ip, port))
            resp, addr = sock.recvfrom(1024)
            print(f'✅ {ip}:{port} - Response: {resp[:30]}')
            responding_ports.append(port)
            
        except socket.timeout:
            print(f'⏰ {ip}:{port} - No response')
        except Exception as e:
            print(f'❌ {ip}:{port} - Error: {e}')
        finally:
            sock.close()
        
        time.sleep(0.1)  # Small delay between requests
    
    return responding_ports

def test_marstek_api_on_port(ip, port):
    """Test Marstek API calls on specific port"""
    print(f'📡 Testing Marstek API on {ip}:{port}')
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    
    try:
        # Marstek device discovery
        payload = {
            "id": 1,
            "method": "Marstek.GetDevice", 
            "params": {"ble_mac": "0"}
        }
        
        data = json.dumps(payload).encode('utf-8')
        sock.sendto(data, (ip, port))
        resp, addr = sock.recvfrom(4096)
        
        result = json.loads(resp.decode())
        print(f'✅ API Response: {result}')
        return True
        
    except socket.timeout:
        print(f'⏰ API timeout on {ip}:{port}')
        return False
    except Exception as e:
        print(f'❌ API error: {e}')
        return False
    finally:
        sock.close()

if __name__ == "__main__":
    print("🚀 Marstek Battery Port Scanner")
    print("=" * 40)
    
    batteries = [
        ("Battery 1", "192.168.68.78"),
        ("Battery 2", "192.168.68.66")
    ]
    
    all_results = {}
    
    for name, ip in batteries:
        print(f"\n🔋 {name} ({ip})")
        responding = quick_port_scan(ip)
        all_results[ip] = responding
        
        # Test API on responding ports
        for port in responding:
            test_marstek_api_on_port(ip, port)
    
    print("\n" + "=" * 40)
    print("📊 SUMMARY:")
    
    for ip, ports in all_results.items():
        if ports:
            print(f"✅ {ip}: Responding ports {ports}")
        else:
            print(f"❌ {ip}: No UDP responses")
    
    if not any(all_results.values()):
        print("\n💡 No responses found today")
        print("   - Try again tomorrow")
        print("   - Marstek may need time to activate API")
        print("   - Consider RS-485 Modbus solution")
    else:
        print("\n🎉 Found responses! API may be partially working")
