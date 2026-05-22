@echo off
echo Closing any existing Chrome instances...
taskkill /F /IM chrome.exe >nul 2>&1
echo Waiting for Chrome processes to fully exit...
timeout /t 3 /nobreak >nul

echo Launching Chrome with CDP debug port + Discord...
start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="C:\chrome-cdp-profile" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://discord.com/channels/@me"

echo Waiting for Chrome and Discord to load (15s)...
timeout /t 15 /nobreak >nul

echo Checking port 9222...
netstat -ano | findstr :9222
if errorlevel 1 (
  echo WARNING: Port 9222 not detected. Chrome may not have started with CDP.
  echo Try closing Chrome manually and running this bat again.
  pause
  exit /b 1
)

echo Starting bot...
python bot.py
pause
