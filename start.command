#!/bin/bash
# Double-click launcher for macOS source checkouts.
cd "$(dirname "$0")" || exit 1
exec uv run --project backend python run.py
