#!/usr/bin/env python3
"""
Marstek Venus UDP Client (JSON over UDP)
Implements the Marstek Open API Rev 1.0 (method + params) over UDP.
Doc reference: Marstek_Device_Open_API_EN_(Rev1.0).txt
"""

import asyncio
import json
import socket
from typing import Dict, Any, Optional, Tuple

class MarstekUDPClient:
    def __init__(self, host: str, port: int = 30000, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send_and_recv(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Sync helper: send JSON and receive response over UDP."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            data = json.dumps(payload).encode("utf-8")
            print(f"📤 Sending UDP to {self.host}:{self.port}: {data.decode()}")
            sock.sendto(data, (self.host, self.port))
            resp, addr = sock.recvfrom(65535)
            text = resp.decode("utf-8", errors="replace")
            print(f"📥 Received from {addr}: {text}")
            return json.loads(text)
        finally:
            sock.close()

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None, id_value: int = 1) -> Optional[Dict[str, Any]]:
        payload = {
            "id": id_value,
            "method": method,
            "params": params or {"id": 0},
        }
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._send_and_recv, payload)
        except socket.timeout:
            print(f"⏰ UDP timeout after {self.timeout}s")
            return None
        except Exception as e:
            print(f"❌ UDP error: {e}")
            return None

    async def discover_broadcast(self, broadcast: Tuple[str, int]) -> Optional[Dict[str, Any]]:
        """Send Marstek.GetDevice broadcast to e.g. ("192.168.68.255", 30000)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(self.timeout)
        payload = {"id": 0, "method": "Marstek.GetDevice", "params": {"ble_mac": "0"}}
        try:
            data = json.dumps(payload).encode("utf-8")
            print(f"📤 Broadcasting to {broadcast[0]}:{broadcast[1]}: {data.decode()}")
            sock.sendto(data, broadcast)
            resp, addr = sock.recvfrom(65535)
            text = resp.decode("utf-8", errors="replace")
            print(f"📥 Broadcast response from {addr}: {text}")
            return json.loads(text)
        except socket.timeout:
            print("⏰ Broadcast timeout")
            return None
        finally:
            sock.close()

    # Convenience wrappers per spec
    async def wifi_get_status(self):
        return await self.call("Wifi.GetStatus", {"id": 0}, id_value=1)

    async def bat_get_status(self):
        return await self.call("Bat.GetStatus", {"id": 0}, id_value=2)

    async def es_get_status(self):
        return await self.call("ES.GetStatus", {"id": 0}, id_value=3)

    async def es_get_mode(self):
        return await self.call("ES.GetMode", {"id": 0}, id_value=4)

    async def es_set_mode_passive(self, power: int = 0, cd_time: int = 0):
        params = {"id": 0, "config": {"mode": "Passive", "passive_cfg": {"power": power, "cd_time": cd_time}}}
        return await self.call("ES.SetMode", params, id_value=5)

# Test functions
async def test_udp_client():
    print("🔋 Testing Marstek UDP Client (Rev1.0 JSON over UDP)")
    print("=" * 60)

    # First: try broadcast discovery on common broadcast addresses
    broadcast_candidates = [("192.168.68.255", 30000), ("255.255.255.255", 30000)]
    disc = MarstekUDPClient("0.0.0.0", 30000)
    for b in broadcast_candidates:
        res = await disc.discover_broadcast(b)
        if res:
            print("✅ Discovery response:")
            print(json.dumps(res, indent=2))
            break

    # Then: direct calls to known battery IP
    client = MarstekUDPClient("192.168.68.78", 30000)
    print("\n📡 Wifi.GetStatus")
    print(await client.wifi_get_status())

    print("\n🔋 Bat.GetStatus")
    print(await client.bat_get_status())

    print("\n⚡ ES.GetStatus")
    print(await client.es_get_status())

    print("\n🧭 ES.GetMode")
    print(await client.es_get_mode())

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Marstek UDP Client (Rev1.0)")
    parser.add_argument("--ip", type=str, default="192.168.68.78", help="Battery IP address")
    parser.add_argument("--port", type=int, default=30000, help="UDP port (default 30000)")
    parser.add_argument("--discover", action="store_true", help="Run UDP broadcast discovery first")
    args = parser.parse_args()

    async def main():
        print("🔋 Marstek UDP Client (Rev1.0)")
        print("=" * 60)

        if args.discover:
            disc = MarstekUDPClient("0.0.0.0", args.port)
            for b in [("192.168.68.255", args.port), ("255.255.255.255", args.port)]:
                res = await disc.discover_broadcast(b)
                if res:
                    print("✅ Discovery response:")
                    print(json.dumps(res, indent=2))
                    break

        client = MarstekUDPClient(args.ip, args.port)
        print(f"\n🎯 Target: {args.ip}:{args.port}")
        print("\n📡 Wifi.GetStatus")
        print(await client.wifi_get_status())

        print("\n🔋 Bat.GetStatus")
        print(await client.bat_get_status())

        print("\n⚡ ES.GetStatus")
        print(await client.es_get_status())

        print("\n🧭 ES.GetMode")
        print(await client.es_get_mode())

    asyncio.run(main())
