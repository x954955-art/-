@echo off
rem Double-click: search Yu-Gi-Oh! cards again every 15 minutes and show only new candidates. Press Ctrl+C to stop.
cd /d "%~dp0"
python -m furima_finder --config yugioh.toml --html yugioh.html --open --watch 15
echo.
pause
