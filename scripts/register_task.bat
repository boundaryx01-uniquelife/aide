@echo off
REM 30분마다 aide 점검을 실행하도록 Windows 작업 스케줄러에 등록합니다.
REM 먼저 `python -m aide heartbeat --send` 가 손으로 성공하는지 확인하세요.
REM 해제: schtasks /Delete /TN "aide-heartbeat" /F
schtasks /Create /SC MINUTE /MO 30 /TN "aide-heartbeat" /TR "\"%~dp0run_heartbeat.bat\"" /F
