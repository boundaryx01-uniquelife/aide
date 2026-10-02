@echo off
REM Send the daily good-morning message (once per day; safe to run again).
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
python -m aide morning --send >> data\aide.log 2>&1
