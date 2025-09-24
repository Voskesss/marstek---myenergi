import asyncio
from app import VenusEModbusClient

def test_read():
    print("\n--- ⚡️ BATTERY DATA TEST --- ")
    client = VenusEModbusClient()
    
    if not client.connect():
        print("❌ Connection failed. Please check HOST and PORT in .env file.")
        return

    data = client.read_battery_data()
    client.disconnect()

    if not data:
        print("❌ Failed to read data.")
        return

    if 'work_mode' in data:
        print("\n✅ RESULTAAT 'work_mode' (Register 35100):")
        print(f"  - Raw Value: {data['work_mode'].get('raw')}")
        print(f"  - Huidige (foute) vertaling: '{data['work_mode'].get('formatted')}'")
    else:
        print("\n⚠️ 'work_mode' (Register 35100) was not found in the response.")
    print("----------------------------")

if __name__ == "__main__":
    test_read()
