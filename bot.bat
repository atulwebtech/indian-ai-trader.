@echo off
REM Starts the Telegram bot. On Windows also turn off sleep in Settings > System > Power.
cd /d "%~dp0"
run.bat telegram %*
