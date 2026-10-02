@echo off
REM Optional: instant button handling. Leave this window open (Ctrl+C to stop).
setlocal
cd /d "%~dp0.."
set PYTHONPATH=%CD%\src
set PYTHONUTF8=1
python -m aide poll --watch
