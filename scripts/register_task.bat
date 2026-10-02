@echo off
REM Register aide heartbeat in Windows Task Scheduler (every 30 min).
REM First confirm: python -m aide heartbeat --send  works by hand.
REM Remove: schtasks /Delete /TN "aide-heartbeat" /F
schtasks /Create /SC MINUTE /MO 30 /TN "aide-heartbeat" /TR "\"%~dp0run_heartbeat.bat\"" /F
