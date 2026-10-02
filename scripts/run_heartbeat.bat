@echo off
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
python -m aide heartbeat --send >> data\aide.log 2>&1
