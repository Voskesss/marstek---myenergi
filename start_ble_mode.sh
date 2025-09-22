#!/bin/bash
# Start myenergi-marstek integration in BLE bridge mode

echo "🔋 Starting myenergi-marstek integration with BLE bridge"
echo "======================================================="

# Check if we're in the right directory
if [ ! -f "ble_bridge.py" ]; then
    echo "❌ Error: ble_bridge.py not found. Run from project directory."
    exit 1
fi

# Activate virtual environment
if [ -d ".venv" ]; then
    echo "📦 Activating virtual environment..."
    source .venv/bin/activate
else
    echo "❌ Error: .venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Set BLE mode
export MARSTEK_USE_BLE=true
export MARSTEK_BLE_BRIDGE=http://localhost:8001

echo "🔧 Configuration:"
echo "   MARSTEK_USE_BLE=$MARSTEK_USE_BLE"
echo "   MARSTEK_BLE_BRIDGE=$MARSTEK_BLE_BRIDGE"
echo

# Function to cleanup background processes
cleanup() {
    echo
    echo "🛑 Shutting down services..."
    if [ ! -z "$BLE_PID" ]; then
        kill $BLE_PID 2>/dev/null
        echo "   Stopped BLE bridge (PID: $BLE_PID)"
    fi
    if [ ! -z "$APP_PID" ]; then
        kill $APP_PID 2>/dev/null
        echo "   Stopped main app (PID: $APP_PID)"
    fi
    exit 0
}

# Set trap for cleanup
trap cleanup SIGINT SIGTERM

# Start BLE bridge in background
echo "🚀 Starting BLE bridge on port 8001..."
python ble_bridge.py &
BLE_PID=$!

# Wait a moment for BLE bridge to start
sleep 3

# Check if BLE bridge is running
if ! curl -s http://localhost:8001/ > /dev/null; then
    echo "❌ Error: BLE bridge failed to start"
    kill $BLE_PID 2>/dev/null
    exit 1
fi

echo "✅ BLE bridge started (PID: $BLE_PID)"

# Start main FastAPI app
echo "🚀 Starting main app on port 8000..."
uvicorn app:app --host 0.0.0.0 --port 8000 --reload &
APP_PID=$!

echo "✅ Main app started (PID: $APP_PID)"
echo
echo "🌐 Services running:"
echo "   BLE Bridge:  http://localhost:8001"
echo "   Main App:    http://localhost:8000"
echo "   BLE UI:      http://localhost:8000/ble/"
echo
echo "📊 Test endpoints:"
echo "   curl http://localhost:8001/api/battery/status"
echo "   curl http://localhost:8000/api/status"
echo
echo "Press Ctrl+C to stop all services"

# Wait for user interrupt
wait
