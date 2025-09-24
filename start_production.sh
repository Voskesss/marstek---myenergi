#!/bin/bash
# Production start script (no reload) voor myenergi-marstek

echo "🚀 Starting myenergi-marstek in PRODUCTION mode"
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

# Set production configuration
export MARSTEK_USE_BLE=true
export EDDI_PRIORITY_MODE=threshold
export EDDI_RESERVE_W=3000
export ZAPPI_RESERVE_W=2000
export BATTERY_MIN_EXPORT_W=5000
export BATTERY_HYSTERESIS_W=500
export MIN_SWITCH_COOLDOWN_S=60

echo "🔧 Production Configuration:"
echo "   MARSTEK_USE_BLE=$MARSTEK_USE_BLE"
echo "   EDDI_PRIORITY_MODE=$EDDI_PRIORITY_MODE"
echo "   BATTERY_MIN_EXPORT_W=$BATTERY_MIN_EXPORT_W"
echo

# Function to cleanup
cleanup() {
    echo
    echo "🛑 Shutting down production..."
    
    # Kill uvicorn processes
    echo "🔄 Stopping uvicorn processes..."
    pkill -f "uvicorn app:app" 2>/dev/null || true
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
    
    echo "✅ Production cleanup complete"
    exit 0
}

# Set trap for cleanup
trap cleanup SIGINT SIGTERM EXIT

# Start FastAPI app WITHOUT reload (more stable)
echo "🚀 Starting app on port 8000 (NO RELOAD)..."
echo
echo "📊 URLs:"
echo "   Live Dashboard:  http://localhost:8000/dashboard"
echo "   API Status:      http://localhost:8000/api/status"
echo
echo "Press Ctrl+C to stop"
echo

uvicorn app:app --host 0.0.0.0 --port 8000
