#!/bin/bash
# Quick stop script voor myenergi-marstek app

echo "🛑 Stopping myenergi-marstek app..."

# Kill uvicorn processes
echo "🔄 Killing uvicorn processes..."
pkill -f "uvicorn app:app" 2>/dev/null && echo "✅ Stopped uvicorn processes" || echo "ℹ️  No uvicorn processes found"

# Kill anything on port 8000
echo "🔄 Freeing port 8000..."
lsof -ti:8000 | xargs kill -9 2>/dev/null && echo "✅ Port 8000 freed" || echo "ℹ️  Port 8000 already free"

# Kill any Python processes with our app
echo "🔄 Cleaning up Python processes..."
pkill -f "python.*app.py" 2>/dev/null && echo "✅ Python processes cleaned" || echo "ℹ️  No Python app processes found"

echo "✅ App stopped successfully!"
echo
echo "To restart: ./start_test.sh"
