#!/bin/bash
# Auto-restart Marstek Dashboard

echo "🔄 Auto-restart Marstek Dashboard"
echo "Press Ctrl+C to stop completely"

cd /Users/josklijnhout/myenergy-marstek
source .venv/bin/activate

while true; do
    echo ""
    echo "🚀 Starting Marstek Dashboard..."
    echo "📡 Server: http://localhost:8000"
    echo "🌐 Dashboard: http://localhost:8000/dashboard"
    echo ""
    
    # Start the application
    uvicorn app:app --reload --port 8000 --host 0.0.0.0
    
    # If we get here, the app crashed or was stopped
    echo ""
    echo "⚠️  Application stopped. Restarting in 3 seconds..."
    echo "   Press Ctrl+C now to stop auto-restart"
    sleep 3
done
