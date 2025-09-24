import socket
import json

BATTERY_IP = "192.168.68.66"   # vervang door jouw batterij-IP
BATTERY_PORT = 30000

# JSON commando om status op te vragen
command = {
    "id": 1,
    "method": "ES.GetStatus",
    "params": {
        "id": 0
    }
}

# UDP socket maken
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Verstuur request
message = json.dumps(command)
sock.sendto(message.encode("utf-8"), (BATTERY_IP, BATTERY_PORT))

# Wacht op antwoord
try:
    sock.settimeout(3)
    data, addr = sock.recvfrom(4096)
    print("Response:", data.decode())
except socket.timeout:
    print("Geen antwoord ontvangen (kan zijn dat Local API nog niet aan staat).")

sock.close()