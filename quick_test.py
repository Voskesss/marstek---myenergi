#!/usr/bin/env python3
"""
Quick test after enabling Local API with Edge browser
"""
import socket
import json
import time

IP = "192.168.68.66"
PORT = 30000

def quick_test():
    print(f"🔍 Quick test: {IP}:{PORT}")
    
    # Test 1: Port scan
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((IP, PORT))
        if result == 0:
            print("✅ TCP port 30000: OPEN")
        else:
            print("❌ TCP port 30000: closed")
        sock.close()
    except Exception as e:
        print(f"❌ TCP test error: {e}")
    
    # Test 2: UDP test
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        
        cmd = {"id": 1, "method": "ES.GetStatus", "params": {"id": 0}}
        message = json.dumps(cmd).encode()
        
        sock.sendto(message, (IP, PORT))
        data, addr = sock.recvfrom(1024)
        
        print(f"✅ UDP response: {data.decode()[:100]}...")
        return True
        
    except socket.timeout:
        print("❌ UDP: timeout")
        return False
    except Exception as e:
        print(f"❌ UDP error: {e}")
        return False
    finally:
        try: sock.close()
        except: pass

if __name__ == "__main__":
    quick_test()
