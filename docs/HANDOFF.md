# aide 인수인계 (2026-10-20, 3b·2a·2b·L1 병합·배포 · 구글 로그인 프로덕션 게시 · 대화 C1 구현)

Claude Code 는 작업 시작 전에 이 파일을 먼저 읽는다. 상태가 바뀌면 이 파일도 갱신한다.

## 프로젝트
- aide: 동래발명교육센터 교사용 개인 비서. 텔레그램 알림(아침 인사·메일 요약·공지·저녁 정리), 버튼 응답 수신.
- 저장소: C:\dev\aide (GitHub boundaryx01-uniquelife/aide). 참고 문서: docs/ROADMAP.md, docs/SERVER.md, docs/GOOGLE_SETUP.md, SECURITY.md.

## 현재 상태 (완료)
- 서버(Ubuntu 24.04, UTC; 주소는 사용자가 알고 있음, 문서에 적지 않음)에서 운영 중. 전용 사용자 aide, /home/aide/aide, main 브랜치.
- systemd: aide-heartbeat.timer(매시 :07, :37), aide-watch.service(Restart=always). 서버에는 TZ=Asia/Seoul 환경변수 필요(naive datetime 사용).
- 구글 계정 3개(기본/개인/학교)의 메일·일정 통합 동작, 메시지에 [이름] 표시. config.json 의 google_accounts, 토큰은 data/google_token.json, google_token_personal.json, google_token_school.json (서버: chown aide:aide, chmod 600).
- 테스트 361개 통과(3b + 2a 추가분 포함). 비밀값(.env, config.json, 토큰, client json, ANTHROPIC_API_KEY)은 깃 제외.
- 서버 git 명령은 항상 `sudo -u aide git -C /home/aide/aide ...` (root 로 하면 dubious ownership).

## 주의사항
- 시크릿(토큰·비밀번호·키)은 채팅·커밋·문서에 올리지 않는다.
- PC 명령은 PowerShell 문법, 서버 명령은 bash. 답변은 짧게(핵심·결론 위주), 상세는 요청 시.
- 모델 라우팅: 모델은 직접 바꿀 수 없으므로 상향/하향은 "권장"만 하고 전환은 사용자가 한다.
- 텔레그램 봇은 프로그램마다 별개. getUpdates 리더는 하나만(watcher 잠금 poll_watch.lock). 둘이면 409 충돌.

## 구글 로그인 만료 (2026-10-13 발생 → 프로덕션 게시로 대응)
- 2026-10-13 14시대부터 기본, 21시부터 개인 계정이 "로그인이 만료되었습니다"로 실패, 그날 저녁 정리의 내일 일정이 비었다. 원인은 OAuth 앱이 "테스트" 상태라 갱신 토큰이 약 7일 뒤 만료되는 것.
- 대응(사용자 진행): Google Cloud 프로젝트 **My First Project(번호 340195897630)** 의 Google 인증 플랫폼 → 브랜딩(앱 이름·지원 이메일·홈페이지·개인정보처리방침 URL) 채움 → 대상에서 **앱 게시(프로덕션)** → 세 계정 재로그인(10/13 23:12~23:13) → 서버에 복사 → 저녁 정리 미리보기에서 일정 정상 조회 확인(2026-10-13, 사용자 확인).
  - 홈페이지: `https://github.com/boundaryx01-uniquelife/aide`, 개인정보처리방침: `https://github.com/boundaryx01-uniquelife/aide/blob/main/PRIVACY.md`(저장소 루트 `PRIVACY.md`, PR #8). 이 문서는 aide 가 실제로 읽는 데이터와 전달 경로를 적은 것이므로 기능이 바뀌면(예: 메일 본문을 읽게 되면) 같이 고친다.
  - 게시 상태가 "프로덕션"으로 바뀐 화면은 직접 확인받지 못했다. **실제 검증은 2026-10-20 무렵**: 그때까지 서버 로그에 "로그인이 만료되었습니다"가 다시 안 나오면 성공.
- 신호: 서버 로그에 "로그인이 만료되었습니다", 저녁 정리의 "불러오지 못한 계정: 이름", 아침 인사에 일정 누락. (확인: `grep "만료" /home/aide/aide/data/aide.log | tail`)
- 복구(계정별 반복, 서버 파일은 PC 에서 받은 로그인을 **복사해야** 바뀐다):
  1. PC: `cd C:\dev\aide` → `python -m aide google-login --account 이름` (이름: 기본/개인/학교, 브라우저에서 해당 계정 선택, "확인되지 않은 앱" 경고는 고급 → 이동)
  2. PC: `scp C:\dev\aide\data\google_token.json C:\dev\aide\data\google_token_personal.json C:\dev\aide\data\google_token_school.json root@서버IP:/home/aide/aide/data/` (와일드카드 대신 파일 이름을 직접 적는다)
  3. 서버: `chown aide:aide /home/aide/aide/data/google_token*.json` 후 `chmod 600 /home/aide/aide/data/google_token*.json`
  4. 서버: `systemctl restart aide-watch` 후 `sudo -u aide bash -c 'cd /home/aide/aide && TZ=Asia/Seoul PYTHONPATH=src python3 -m aide evening'` 로 일정이 나오는지 확인(미리보기, 전송 안 함)
  - 주의: 조용한 시간(22:30~07:00)에는 점검이 구글을 부르지 않아 로그만으로는 성공 여부를 알 수 없다. 위 미리보기로 확인한다. 서버 파일 시각(`ls -l --time-style=long-iso`)이 방금 복사한 시각인지도 본다.
- 학교 계정은 관리자 정책으로 막힐 수 있다(막히면 화면 문구 확인).
- 남은 안전망 후보: 로그인이 만료되면 텔레그램으로 먼저 알려 주는 "만료 예고 알림"(아직 안 만듦).

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

## 2a 완료 (LLM 중요도 판단) — main, 서버 배포·실사용 확인까지 끝남
- 내용: 메일·공지·뉴스 항목에 high/normal/low 를 매겨 **순서만** 바꿈(숨김·삭제 없음). 기본 꺼짐(`llm_enabled: false`). 사용자 결정(2026-10-09): `claude-haiku-5-5`, 표준 라이브러리 HTTP 직접 호출, 학교 계정 제외.
- 설계·보안 검토는 Opus, 구현·테스트는 Sonnet(사용자 지시). 설계: ARCHITECTURE.md "LLM 판단 (2a)", 보안: SECURITY.md "LLM 판단 2a".
- **구현 시 설계에서 바꾼 것**: 드라이런(`heartbeat` 미리보기)은 LLM 을 **부르지 않음**(돈이 나가는 첫 외부 호출이라 비용 안전을 위해 보수적으로 수정). `--send` 경로에서만 호출.
- 새 파일: `src/aide/llm.py`(`rank`/`apply_priority`/`labels_by_key`, `State` 미사용). `tests/test_llm.py`.
- PR #4 병합 완료(squash, main, 2026-10-09).
- 서버: `git pull`(최초에 origin/main 추적이 안 돼 `fetch`+`merge --ff-only` 로 해결) → `.env` 에 `ANTHROPIC_API_KEY` 추가 → `config.json` 에 `llm_enabled: true` 등 추가 → `aide-watch` 재시작 → selfcheck 로 `llm: 켜짐 (claude-haiku-5-5)` 확인 → **정상 작동 확인 완료 (2026-10-09, 사용자 확인)**.
- 남음(선택): `llm_sources`/`llm_exclude_accounts` 실사용 중 조정, 2b(요약) 설계는 2a 를 며칠 돌려 본 뒤.

## 2b 구현 완료 (오늘의 핵심) — main 병합됨(PR #6), 서버에서 `llm_highlights` 는 아직 안 켬
- 내용: 아침/저녁(평일은 "내일의 핵심", 일요일은 "이번 주 핵심") 맨 위에 최대 3줄. 닫힌 출력(A안) — 모델은 번호+이유 코드만, 문장은 원본 데이터+고정 문구로 조립. 기본 꺼짐(`llm_highlights: []`).
- 설계·보안 검토는 Opus, 구현·테스트는 Sonnet(사용자 지시). 설계: ARCHITECTURE.md "LLM 판단 (2b)", 보안: SECURITY.md "LLM 판단 2b". 설계대로 구현됨(2a 와 달리 구현 중 바꾼 것 없음).
- 새 파일 없음. 바뀐 파일: `gcal.py`(`Event.accounts`), `accounts.py`(`gather_events` 가 account 채움, `event_excluded()`), `llm.py`(공통 `_call_api` 로 `rank`/`pick` 공유, `pick()`+검증+`WHY_PHRASES`), `config.py`(`llm_highlights`+검증), `morning.py`/`evening.py`(조회·LLM 호출·조립을 상태 잠금 **밖**으로 옮기는 재구성 — 2a 이전부터 있던 "메시지 조립이 잠금 안에서 돈다" 문제를 2b 가 고침), `__main__.py`(selfcheck), `config.example.json`, README.
- 테스트 408개 통과(기존 361 + 신규 47): `llm.pick()` 자체 검증, 계정 제외(두 계정 섞인 경우 포함), morning/evening 통합(꺼짐/드라이런 시 호출 없음, 하루 상한 기록, 평일/일요일 헤더 구분).
- **남은 일**: PR 생성 → main 병합 → 서버: `config.json` 에 `llm_highlights` 추가(예: `["morning", "evening"]`) → `aide-watch` 재시작 → 아침/저녁 알림에서 핵심 섹션 실제 확인.
- 남은 확인(사용자, 2a 때부터 이어짐): Anthropic 데이터 보존·학습 정책 — 아직 미확인으로 SECURITY.md 에 남아 있음.

## 선호 학습 L1 완료 (내 규칙) — main 병합(PR #7), 서버에서 `rules_enabled` 켬, [정리] 버튼 실사용 확인 전
- 내용: 메일 알림 아래 [🧹 정리] → 발신자 → 알림제외/유지/중요/수신거부. 규칙은 주소 하나 단위, `data/rules.json`(깃 제외). 일요일 저녁 정리에 "걸러낸 것" 요약. `mail_bulk_fold` 로 광고·정기 메일 한 줄 접기. LLM 안 씀. 기본 꺼짐(`rules_enabled`, `mail_bulk_fold`).
- 설계·보안 검토는 Opus, 구현·테스트는 Sonnet(사용자 지시). 설계: ARCHITECTURE.md "선호 학습 L1", 보안: SECURITY.md "선호 학습 L1". 설계 대비 달라진 점은 ARCHITECTURE.md 끝에 기록(저녁 정리 필수, 확인 후에도 정리 버튼 유지).
- 새 파일: `src/aide/rules.py`, `src/aide/cleanup.py`, `tests/test_rules.py`. 바뀐 파일: `models.py`(Finding 필드 3개), `gmail.py`(헤더 3개 요청·bulk 감지, 마지막 항목 복사 버그 방지), `state.py`(digest targets·hidden), `telegram.py`(edit_text·set_buttons·URL 버튼), `heartbeat.py`(규칙 적용·접기·섹션·targets), `inbox.py`(m:/r: 버튼), `evening.py`(걸러낸 것), `config.py`·`__main__.py`(설정·`rules` 명령·selfcheck), `.gitignore`, `config.example.json`, README.
- 테스트 470개 통과(기존 408 + 신규 62).
- 서버: 코드 반영·`rules_enabled: true`·재시작 후 selfcheck 로 `rules : 켜짐 / 광고·정기 메일 접기 꺼짐` 확인(2026-10-13, 사용자 확인). **남은 확인**: 메일 알림에 [🧹 정리] 버튼이 뜨고 동작하는지(아직 규칙을 만든 적 없어 `data/rules.json` 없음). `mail_bulk_fold` 는 켜지 않음.
- 다음: L2(같은 발신자 광고·정기 메일이 반복되면 규칙 제안), L3(취향을 LLM 판단에 반영, 뉴스 입맛), L4(수신 거부 대행, 별도 검토). 2b 는 병합됐지만 서버에서 `llm_highlights` 는 아직 안 켬.

## 대화 C1 구현 완료 (문장으로 묻기) — 브랜치 feat/chat-c1, PR 전·서버 `chat_enabled` 는 아직 안 켬
- 내용: 텔레그램에 `/` 없이 문장을 쓰면 모델(Haiku)이 **본인 문장만** 보고 조회 요청 하나(일정/마감/메일/날씨/오늘/도움/모름 + 날짜·일수·계정·키워드)로 바꾸고, 고정 읽기 전용 렌더러가 답한다. 메일·일정 내용은 모델에 안 간다. 첫 줄에 "▸ …로 찾아봤어요" 로 해석을 보여 준다. 기본 꺼짐(`chat_enabled`, `chat_max_per_day` 20).
- 사용자 결정(2026-10-20): C1 만 먼저, 조회 다섯 가지(+오늘 요약·도움), 문장을 Anthropic 으로 보내는 것에 동의, 서버(VPS) 에서 동작(PC 는 서버로 쓰지 않음). Google Tasks 는 로그인 안정 확인(~10/20) 뒤로 미룸(`tasks.readonly` 추가·재로그인·PRIVACY.md 수정 필요).
- 설계·보안 검토는 Opus, 구현·테스트는 Sonnet(사용자 지시). 설계: ARCHITECTURE.md "대화 C1", 보안: SECURITY.md "대화 C1".
- 새 파일: `src/aide/chat.py`, `tests/test_chat.py`. 바뀐 파일: `commands.py`(렌더러 인자 추가·`/날씨`), `inbox.py`(문장 분기·전용 예산), `state.py`(`chat_calls`), `config.py`(`chat_*`·`날씨` 명령), `weather.py`(`on_day`), `accounts.py`(`gather_events(only=)`), `__main__.py`(selfcheck), `config.example.json`, README/ARCHITECTURE/SECURITY/ROADMAP.
- 테스트 582개 통과(기존 491 + 신규 91).
- **검증 에이전트 6종(2026-10-20)**: 대학생·영업팀장·은퇴 교장·학부모·개발자·보안 레드팀 페르소나로 시험. 중대 취약점 없음. 그 결과 같은 브랜치에 **A 항목 수정**을 반영함: 검증 실패 시 부분 보정+사유별 안내, 연결 실패·키 없음 환불, 대화 전용 시간당 한도(10)·안내 답장 잠금 밖·종류당 1번, 재전달 건너뜀, 자정 직전 시각 버그, API 키 형식 검사와 `ValueError`→`NetError`, 키워드 허용 문자 확대, 메일 키워드가 계정 라벨에 걸리던 버그, 조사 오류 없는 첫 줄("▸ 찾아본 것: …"), 전각 슬래시, 과거 시작일 62일, 도움말에 개인정보 경고·'한 단어만 써도 돼요'.
- **남은 일**: PR 생성 → 사용자가 "병합" 하면 main 병합 → 서버: 코드 반영(`fetch` + `merge --ff-only`) → `config.json` 에 `"chat_enabled": true` (+ `commands` 에 `날씨` 추가 선택) → `aide-watch` 재시작 → selfcheck 에서 `chat : 켜짐 (하루 20회)` 확인 → 텔레그램에서 "내일 일정 뭐야?" 로 실사용 확인 → 켠 날짜를 SECURITY.md 에 기록. Console 에서 월 지출 한도 권장.
- 알려진 한계(다음 PR 후보 B): 이전 대화를 기억하지 않음(B4, 보안 검토 필요). **B1** 일정에 종료 시각·여러 날 기간·장소 표시와 겹침·빈 시간(영업팀장·학부모 공통, 가장 큰 가치). **B2** 일정 캘린더당 50건 상한과 잘림 안내/서버 검색. **B3** 메일 검색 확장(읽은 메일·발신자·낱말 여러 개, 키워드가 Gmail 질의에 들어가므로 별도 보안 검토). **B5** 주간 날씨·지역 지정·일정+날씨 묶기. **B6** 처리 중 표시, 수정된 메시지·음성 대응, 오전/오후, 별칭. **B7** 할 일(Google Tasks)·알림.
- 다음 후보: Google Tasks 연동(아침 인사에 "오늘 할 일"), C2 종합 답변(별도 보안 검토), L2/L3.

## 최근 건드린 파일
- 3b 명령: 위 섹션 참고.
- 구글 다중 계정: src/aide/accounts.py, config.py, heartbeat.py, morning.py, evening.py, __main__.py, tests/test_accounts.py
- 서버 운영: deploy/*, scripts/run_heartbeat.sh, docs/SERVER.md, SECURITY.md
