@echo off
REM Start the instant button watcher at every logon (hidden window).
REM If this says access denied, run it from an administrator prompt.
REM Stop now: schtasks /End /TN "aide-poll-watch"   Remove: schtasks /Delete /TN "aide-poll-watch" /F
schtasks /Create /SC ONLOGON /DELAY 0000:30 /TN "aide-poll-watch" /TR "wscript.exe \"%~dp0poll_watch_hidden.vbs\"" /F
echo Created. Start it now with: schtasks /Run /TN "aide-poll-watch"
