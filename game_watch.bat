@echo off
rem Double-click: search game consoles and games again every 15 minutes and show only new candidates. Press Ctrl+C to stop.
cd /d "%~dp0"
python -m furima_finder --config game.toml --html game.html --open --watch 15
echo.
pause
