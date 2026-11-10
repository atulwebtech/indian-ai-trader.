@echo off
REM Windows setup: creates virtual environment and installs libraries.
cd /d "%~dp0"
python -m venv .venv || goto :error
.venv\Scripts\python -m pip install --upgrade pip || goto :error
.venv\Scripts\python -m pip install -r requirements.txt || goto :error
if not exist .env copy .env.example .env
echo Setup complete.
goto :eof
:error
echo Setup failed. Please read the message above.
exit /b 1
