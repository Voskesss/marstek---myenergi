#!/bin/bash
# Start myenergi-marstek integration with integrated BLE

echo "🔋 Starting myenergi-marstek integration with integrated BLE"
echo "============================================================="

# Check if we're in the right directory
if [ ! -f "app.py" ]; then
    echo "❌ Error: app.py not found. Run from project directory."
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

# Check if bleak is installed
if ! python -c "import bleak" 2>/dev/null; then
    echo "📦 Installing BLE support..."
    pip install bleak
fi

# Set integrated BLE mode
export MARSTEK_USE_BLE=true

echo "🔧 Configuration:"
echo "   MARSTEK_USE_BLE=$MARSTEK_USE_BLE"
echo "   Mode: Integrated BLE (no separate bridge process)"
echo

# Function to cleanup
cleanup() {
    echo
    echo "🛑 Shutting down..."
    exit 0
}

# Set trap for cleanup
trap cleanup SIGINT SIGTERM

# Test BLE first (optional)
if [ "$1" = "--test" ]; then
    echo "🧪 Running BLE test first..."
    python test_integrated.py
    if [ $? -ne 0 ]; then
        echo "❌ BLE test failed. Check battery connection."
        exit 1
    fi
    echo
fi

# Start main FastAPI app with integrated BLE
echo "🚀 Starting integrated app on port 8000..."
echo "   BLE client will be initialized automatically"
echo

uvicorn app:app --host 0.0.0.0 --port 8000 --reload &
APP_PID=$!

echo "✅ App started (PID: $APP_PID)"
echo
echo "🌐 Services:"
echo "   Main App:    http://localhost:8000"
echo "   BLE Status:  http://localhost:8000/api/ble/status"
echo "   BLE UI:      http://localhost:8000/ble/"
echo "   Status:      http://localhost:8000/api/status"
echo
echo "📊 Test commands:"
echo "   curl http://localhost:8000/api/ble/status"
echo "   curl http://localhost:8000/api/ble/info"
echo "   curl -X POST http://localhost:8000/api/ble/connect"
echo
echo "Press Ctrl+C to stop"

# Wait for user interrupt
wait
