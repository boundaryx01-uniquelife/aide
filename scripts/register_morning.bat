@echo off
REM Run the morning message at logon. Do NOT register until stage 1 has run stably for a few days.
REM If this says access denied, run it from an administrator prompt.
REM Remove: schtasks /Delete /TN "aide-morning" /F
schtasks /Create /SC ONLOGON /DELAY 0001:00 /TN "aide-morning" /TR "\"%~dp0run_morning.bat\"" /F
