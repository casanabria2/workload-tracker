#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
exec python3 mcp_server.py
