#!/usr/bin/env bash
# Operation MARTINA — Launch script

set -e
cd "$(dirname "$0")"

echo "AutoPurple — Operation MARTINA"
echo "================================"

# Install deps if needed
if ! python3 -c "import fastapi, anthropic, uvicorn" 2>/dev/null; then
  echo "Installing dependencies..."
  pip3 install -r requirements.txt --quiet
fi

# API key check
if [ -z "$ANTHROPIC_API_KEY" ]; then
  KEY=$(python3 -c "import json; d=json.load(open('config.json')); print(d.get('anthropic_api_key',''))" 2>/dev/null || true)
  if [ -z "$KEY" ]; then
    echo ""
    echo "⚠  ANTHROPIC_API_KEY not set."
    echo "   Edit config.json and add your key, or:"
    echo "   export ANTHROPIC_API_KEY=sk-ant-..."
    echo ""
  fi
fi

echo "Starting on http://127.0.0.1:7749"
echo "Access code:  $(python3 -c "import json; print(json.load(open('config.json'))['access_code'])")"
echo "Admin key:    $(python3 -c "import json; print(json.load(open('config.json'))['admin_key'])")"
echo ""
echo "Open http://127.0.0.1:7749 in a browser and give Martina the access code."
echo "Use the admin key to monitor progress."
echo ""

python3 server.py
