@echo off
REM Instant button handling. Normally started hidden by poll_watch_hidden.vbs.
REM Own log file: a long-running watcher holds its log open, which would block the
REM 30-minute task from appending to aide.log (it failed with exit code 1).
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
python -m aide poll --watch >> data\poll_watch.log 2>&1
