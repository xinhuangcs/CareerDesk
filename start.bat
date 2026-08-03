@echo off
rem Double-click launcher for Windows source checkouts.
cd /d "%~dp0"
uv run --project backend python run.py
pause
