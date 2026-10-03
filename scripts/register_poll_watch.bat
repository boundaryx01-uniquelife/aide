@echo off
REM Keep the instant button watcher alive (hidden, no admin needed).
REM Every minute it tries to start; if one is already running it exits at once,
REM so a crashed watcher comes back within a minute.
REM Stop now: schtasks /End /TN "aide-poll-watch"   Remove: schtasks /Delete /TN "aide-poll-watch" /F
schtasks /Create /SC MINUTE /MO 1 /TN "aide-poll-watch" /TR "wscript.exe \"%~dp0poll_watch_hidden.vbs\"" /F
if errorlevel 1 (
  echo FAILED to create the task. See the message above.
  exit /b 1
)
echo Created. It starts within a minute, or now with: schtasks /Run /TN "aide-poll-watch"
