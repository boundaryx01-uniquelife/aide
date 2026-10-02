@echo off
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
REM 1) record button presses first, 2) then check and notify
python -m aide poll >> data\aide.log 2>&1
python -m aide heartbeat --send >> data\aide.log 2>&1
