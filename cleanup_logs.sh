#!/bin/bash
# Cleanup old log files (keep last 7 days)

LOG_DIR="logs"
DAYS_TO_KEEP=7

if [ -d "$LOG_DIR" ]; then
    echo "🧹 Cleaning up logs older than $DAYS_TO_KEEP days..."
    find "$LOG_DIR" -name "*.log*" -type f -mtime +$DAYS_TO_KEEP -delete
    echo "✅ Cleanup complete!"
else
    echo "⚠️ Log directory not found"
fi
