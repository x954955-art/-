@echo off
rem Double-click: check whether Mercari, Rakuma, Yahoo Flea Market and SNKRDUNK can be read
cd /d "%~dp0"
python -m furima_finder --probe
echo.
pause
