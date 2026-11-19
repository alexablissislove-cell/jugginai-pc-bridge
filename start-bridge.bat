@echo off
rem JugginAI PC bridge launcher. Starts bridge.py (127.0.0.1:8899) and the
rem cloudflared connector for pc.jugginai.net. Re-launches itself hidden so no
rem console window stays open (no .vbs / wscript needed).
setlocal
if /i not "%~1"=="hidden" (
  powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%~f0' -ArgumentList 'hidden'"
  exit /b 0
)

set "JDIR=%USERPROFILE%\.jugginai"
set "BRIDGE=%JDIR%\bridge"
if not exist "%JDIR%\logs" mkdir "%JDIR%\logs"
cd /d "%BRIDGE%" || exit /b 1

set "PYW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if defined PYW (
  start "" "%PYW%" "%BRIDGE%\bridge.py"
) else (
  rem fall back to the Python launcher's windowless mode
  start "" pyw.exe -3 "%BRIDGE%\bridge.py"
)

if exist "%JDIR%\cloudflared-token" (
  start "" /B "%JDIR%\cloudflared.exe" tunnel --no-autoupdate --logfile "%JDIR%\logs\cloudflared.log" run --token-file "%JDIR%\cloudflared-token"
)
endlocal
exit /b 0
