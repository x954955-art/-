@echo off
rem Double-click: find Pokemon cards on Mercari that can be resold at a profit (settings: pokeca.toml)
cd /d "%~dp0"
python -m furima_finder --config pokeca.toml --html pokeca.html --open
echo.
pause
