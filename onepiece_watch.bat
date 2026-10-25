@echo off
rem Double-click: search One Piece cards again every 15 minutes and show only new candidates. Press Ctrl+C to stop.
cd /d "%~dp0"
python -m furima_finder --config onepiece.toml --html onepiece.html --open --watch 15
echo.
pause
