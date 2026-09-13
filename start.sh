#!/bin/bash
# start.sh — Run the TOS ORB Bot on VPS
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt -q

echo "Starting TOS ORB Bot on port 5001..."
python main.py --port 5001
