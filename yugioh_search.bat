@echo off
rem Double-click: find Yu-Gi-Oh! cards on Mercari that can be resold at a profit (settings: yugioh.toml)
cd /d "%~dp0"
python -m furima_finder --config yugioh.toml --html yugioh.html --open
echo.
pause
