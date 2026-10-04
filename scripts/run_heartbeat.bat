@echo off
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
REM 1) record button presses, 2) check and notify, 3) good-morning and 4) evening wrap-up (each once a day; skip quiet hours)
python -m aide poll >> data\aide.log 2>&1
python -m aide heartbeat --send >> data\aide.log 2>&1
python -m aide morning --send >> data\aide.log 2>&1
python -m aide evening --send >> data\aide.log 2>&1
