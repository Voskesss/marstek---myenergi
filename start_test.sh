#!/bin/bash
# Start app voor live testing

echo "🧪 Starting myenergi-marstek for Live Testing"
echo "=============================================="

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
    echo "❌ Error: .venv not found."
    exit 1
fi

# Set configuration for testing
export MARSTEK_USE_BLE=true
export EDDI_PRIORITY_MODE=threshold
export EDDI_RESERVE_W=3000
export ZAPPI_RESERVE_W=2000
export BATTERY_MIN_EXPORT_W=5000
export BATTERY_HYSTERESIS_W=500
export MIN_SWITCH_COOLDOWN_S=10  # Shorter for testing

echo "🔧 Test Configuration:"
echo "   MARSTEK_USE_BLE=$MARSTEK_USE_BLE"
echo "   EDDI_PRIORITY_MODE=$EDDI_PRIORITY_MODE"
echo "   BATTERY_MIN_EXPORT_W=$BATTERY_MIN_EXPORT_W"
echo "   BATTERY_HYSTERESIS_W=$BATTERY_HYSTERESIS_W"
echo "   MIN_SWITCH_COOLDOWN_S=$MIN_SWITCH_COOLDOWN_S"
echo

# Function to cleanup
cleanup() {
    echo
    echo "🛑 Shutting down test..."
    exit 0
}

# Set trap for cleanup
trap cleanup SIGINT SIGTERM

# Start FastAPI app
echo "🚀 Starting app on port 8000..."
echo
echo "📊 Test URLs:"
echo "   Live Dashboard:  http://localhost:8000/dashboard"
echo "   API Status:      http://localhost:8000/api/status"
echo "   BLE Status:      http://localhost:8000/api/ble/status"
echo "   BLE UI:          http://localhost:8000/ble/"
echo
echo "🎮 Manual Controls:"
echo "   Allow Battery:   curl -X POST http://localhost:8000/api/marstek/allow"
echo "   Block Battery:   curl -X POST http://localhost:8000/api/marstek/inhibit"
echo
echo "Press Ctrl+C to stop"
echo

uvicorn app:app --host 0.0.0.0 --port 8000 --reload
