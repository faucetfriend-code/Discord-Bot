@echo off
REM External liveness monitor for the Discord Signal Bot.
REM Runs watchdog.py in loop mode (interval = WATCHDOG_INTERVAL_SEC from .env,
REM default 120s) and appends all output to watchdog.log next to this script.
REM It is read-only: never writes bot.db, .env, or any CSV.
cd /d "%~dp0"

echo Starting watchdog loop (watchdog.py writes watchdog.log itself)...
REM Do NOT redirect stdout to watchdog.log: watchdog.py opens that file itself
REM and a shell redirect holds it open, causing "Permission denied" on every write.
python -u watchdog.py >> watchdog.console.log 2>&1
echo Watchdog exited with code %ERRORLEVEL% - see watchdog.log
pause
