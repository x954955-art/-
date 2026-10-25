@echo off
rem Double-click: find One Piece cards on Mercari that can be resold at a profit (settings: onepiece.toml)
cd /d "%~dp0"
python -m furima_finder --config onepiece.toml --html onepiece.html --open
echo.
pause
