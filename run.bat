@echo off
REM Double-click this file to build a county. It asks which county,
REM and (the first time only) your API key and Minecraft world name.
cd /d "%~dp0"
python main.py %*
echo.
pause
