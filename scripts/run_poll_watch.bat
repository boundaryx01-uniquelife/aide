@echo off
REM Instant button handling. Normally started hidden by poll_watch_hidden.vbs at logon.
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
python -m aide poll --watch >> data\aide.log 2>&1
