#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /Users/musicmancheef/Documents/Codex/2026-08-03/referenced-chatgpt-conversation-this-is-an-2/Dublin-local
.venv/bin/pip install -e . --quiet 2>&1 | tail -3
echo "=== RUFF ==="
.venv/bin/ruff check src/ tests/
echo "=== PYTEST ==="
.venv/bin/python -m pytest tests/ -v 2>&1
