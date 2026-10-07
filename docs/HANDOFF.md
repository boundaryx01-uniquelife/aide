# aide 인수인계 (2026-10-08)

Claude Code 는 작업 시작 전에 이 파일을 먼저 읽는다. 상태가 바뀌면 이 파일도 갱신한다.

## 프로젝트
- aide: 동래발명교육센터 교사용 개인 비서. 텔레그램 알림(아침 인사·메일 요약·공지·저녁 정리), 버튼 응답 수신.
- 저장소: C:\dev\aide (GitHub boundaryx01-uniquelife/aide). 참고 문서: docs/ROADMAP.md, docs/SERVER.md, docs/GOOGLE_SETUP.md, SECURITY.md.

## 현재 상태 (완료)
- 서버(Ubuntu 24.04, UTC; 주소는 사용자가 알고 있음, 문서에 적지 않음)에서 운영 중. 전용 사용자 aide, /home/aide/aide, main 브랜치.
- systemd: aide-heartbeat.timer(매시 :07, :37), aide-watch.service(Restart=always). 서버에는 TZ=Asia/Seoul 환경변수 필요(naive datetime 사용).
- 구글 계정 3개(기본/개인/학교)의 메일·일정 통합 동작, 메시지에 [이름] 표시. config.json 의 google_accounts, 토큰은 data/google_token.json, google_token_personal.json, google_token_school.json (서버: chown aide:aide, chmod 600).
- 테스트 256개 통과. 비밀값(.env, config.json, 토큰, client json)은 깃 제외.
- 서버 git 명령은 항상 `sudo -u aide git -C /home/aide/aide ...` (root 로 하면 dubious ownership).

## 주의사항
- 시크릿(토큰·비밀번호·키)은 채팅·커밋·문서에 올리지 않는다.
- PC 명령은 PowerShell 문법, 서버 명령은 bash. 답변은 짧게(핵심·결론 위주), 상세는 요청 시.
- 모델 라우팅: 모델은 직접 바꿀 수 없으므로 상향/하향은 "권장"만 하고 전환은 사용자가 한다.
- 텔레그램 봇은 프로그램마다 별개. getUpdates 리더는 하나만(watcher 잠금 poll_watch.lock). 둘이면 409 충돌.

## 만료 대비 (구글 로그인, 약 10/11부터)
- OAuth 앱이 Testing 상태라 갱신 토큰이 약 7일 뒤 만료될 수 있다(로그인 10/04~10/06 → 10/11~10/13).
- 신호: 서버 로그에 "불러오지 못했어요" 또는 "불러오지 못한 계정: 이름", 아침 인사에 일정 누락.
- 복구(계정별 반복):
  1. PC: `cd C:\dev\aide` → `python -m aide google-login --account 이름` (이름: 기본/개인/학교, 브라우저에서 해당 계정 선택)
  2. PC: `scp data\google_token*.json root@서버IP:/home/aide/aide/data/`
  3. 서버: `chown aide:aide /home/aide/aide/data/google_token*.json` 후 `chmod 600 /home/aide/aide/data/google_token*.json`
  4. 서버: `systemctl restart aide-watch` 후 `tail -n 30 /home/aide/aide/data/aide.log`
- 학교 계정은 관리자 정책으로 막힐 수 있다(막히면 화면 문구 확인).
- 근본 해결 후보: OAuth 앱 프로덕션 게시(민감 범위라 심사 필요할 수 있음) 또는 만료 예고 알림 추가.

## 남은 정리
- PC 예약 작업 aide-poll-watch, aide-heartbeat 삭제(서버 이전 완료, 현재 Disabled). 삭제 여부를 사용자에게 확인.
- 선택 개선: 저녁 마감 창 7일로 확대, 제목 마감일 `(~10/12(월))` 파싱, 예약 시각 :00/:30 이동, 공공데이터(data.go.kr) 연동(ROADMAP F).

## 다음 작업: 3b (텔레그램 읽기 전용 명령)
- 목표: `/오늘 /일정 /마감 /메일` 명령으로 즉시 조회.
- 순서: 1) ROADMAP.md, SECURITY.md 읽기 2) 설계안 제안 3) 보안 검토 4) 승인 후 구현·테스트 5) 서버 배포.
- 보안 필수: 허용된 TELEGRAM_CHAT_ID 만 응답, 읽기 전용(쓰기·전송·삭제 명령 없음), 입력 길이·형식 제한, 로그에 메시지 원문·개인정보 최소화, 실패 시 조용히 거부.
- 권장 모델: 설계·보안 검토는 Opus + Extended thinking, 구현·테스트는 Sonnet.

## 최근 건드린 파일
- 구글 다중 계정: src/aide/accounts.py, config.py, heartbeat.py, morning.py, evening.py, __main__.py, tests/test_accounts.py
- 서버 운영: deploy/*, scripts/run_heartbeat.sh, docs/SERVER.md, SECURITY.md
