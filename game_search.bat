@echo off
rem Double-click: find game consoles and popular games on Mercari that can be resold at a profit (settings: game.toml)
cd /d "%~dp0"
python -m furima_finder --config game.toml --html game.html --open
echo.
pause
