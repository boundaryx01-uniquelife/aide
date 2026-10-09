# aide 인수인계 (2026-10-09, 3b 완료 · 2a 설계 확정)

Claude Code 는 작업 시작 전에 이 파일을 먼저 읽는다. 상태가 바뀌면 이 파일도 갱신한다.

## 프로젝트
- aide: 동래발명교육센터 교사용 개인 비서. 텔레그램 알림(아침 인사·메일 요약·공지·저녁 정리), 버튼 응답 수신.
- 저장소: C:\dev\aide (GitHub boundaryx01-uniquelife/aide). 참고 문서: docs/ROADMAP.md, docs/SERVER.md, docs/GOOGLE_SETUP.md, SECURITY.md.

## 현재 상태 (완료)
- 서버(Ubuntu 24.04, UTC; 주소는 사용자가 알고 있음, 문서에 적지 않음)에서 운영 중. 전용 사용자 aide, /home/aide/aide, main 브랜치.
- systemd: aide-heartbeat.timer(매시 :07, :37), aide-watch.service(Restart=always). 서버에는 TZ=Asia/Seoul 환경변수 필요(naive datetime 사용).
- 구글 계정 3개(기본/개인/학교)의 메일·일정 통합 동작, 메시지에 [이름] 표시. config.json 의 google_accounts, 토큰은 data/google_token.json, google_token_personal.json, google_token_school.json (서버: chown aide:aide, chmod 600).
- 테스트 309개 통과(3b 추가분 포함). 비밀값(.env, config.json, 토큰, client json)은 깃 제외.
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

## 3b 완료 (텔레그램 읽기 전용 명령) — main, 서버 배포·실사용 확인까지 끝남
- 내용: `/오늘 /일정 /마감 /메일 /도움` (영문 별칭 today/week/due/mail/help/start). 기본 꺼짐(`config.json` 의 `commands: []`).
- 설계·보안 검토는 Opus, 구현·테스트는 Sonnet (사용자 지시). 보안 검토 내용은 SECURITY.md "3b: 읽기 전용 명령" 참고.
- 새 파일: `src/aide/commands.py` (명령 해석·렌더링, `State` 미사용). `tests/test_commands.py`.
- 바뀐 파일: `inbox.py`(명령 허용 검사·속도 제한·잠금 분리), `telegram.py`(`get_updates(allowed=...)`), `state.py`(`cmd_times` 속도 제한 기록), `config.py`(`commands` 필드·검증), `accounts.py`(`tag_mail` — heartbeat 의 `_tag_account` 를 옮겨 commands.py 와 공유), `heartbeat.py`(위 이동 반영), `__main__.py`(selfcheck 에 commands 상태 한 줄), `config.example.json`.
- 문서: README.md("읽기 전용 명령 (3b)"), ARCHITECTURE.md("수신부 3a/3b"), SECURITY.md, docs/ROADMAP.md.
- PR #1 (구현), PR #2 (HANDOFF 갱신) 병합 완료 (2026-10-09, squash, main).
- 서버: `config.json` 에 `commands` 추가 후 `aide-watch` 재시작 완료. 텔레그램에서 `/오늘 /일정 /마감 /메일` 실제 응답 확인 완료 (2026-10-09, 사용자 확인).
- 남음(선택): BotFather 명령 메뉴에 영문 별칭 등록(수동), 속도 제한(묶음 3개/시간당 20개)이 실사용에 거북하면 `commands.py` 의 `MAX_PER_BATCH`/`MAX_PER_HOUR` 조정.

## 다음 작업: 2a LLM 중요도 판단 — `feat/llm-rank` 브랜치, 설계 확정·구현 전
- 설계: ARCHITECTURE.md "LLM 판단 (2a)" (흐름·모듈·요청 형식·스키마·설정), 보안: SECURITY.md "LLM 판단 2a".
- 사용자 결정(2026-10-09): `claude-haiku-5-5`, 표준 라이브러리 HTTP 직접 호출(SDK 안 씀), 학교 계정 제외, 기본 꺼짐.
- 구현 순서(테스트 먼저): 1) `netutil.post_json` 2) `Finding.account` + `accounts.tag_mail` 3) `state.llm_calls` 4) `llm.py`(rank, 응답 검증) 5) `heartbeat` 정렬·"■ 중요 (AI 판단)" 섹션 6) config 키·검증 7) selfcheck 8) README.
- 꼭 넣을 테스트: 키 없음/꺼짐→호출 안 함, 응답 형식 오류·번호 누락·중복·이상한 값·`refusal`·`max_tokens`·시간 초과→`None` 이고 digest 는 기존과 동일, 제외 계정·git 은 전송 안 됨, 전송 본문에 URL·키 없음, 하루 상한, 드라이런은 상태 불변, 로그에 본문 없음.
- 권장 모델: 구현·테스트는 Sonnet.
- 켜기 전 사용자 확인: Anthropic 데이터 보존·학습 정책, Console 전용 키·지출 한도, 서버 `.env` 에 키 추가(채팅에 붙여넣지 말 것).

## 최근 건드린 파일
- 3b 명령: 위 섹션 참고.
- 구글 다중 계정: src/aide/accounts.py, config.py, heartbeat.py, morning.py, evening.py, __main__.py, tests/test_accounts.py
- 서버 운영: deploy/*, scripts/run_heartbeat.sh, docs/SERVER.md, SECURITY.md
