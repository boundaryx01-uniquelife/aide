# 설계

## 핵심 원칙
1. **싼 검사는 코드, 판단이 필요한 것만 AI.** 30분마다 AI를 깨우면 토큰이 계속 나가고 결과도 매번 달라진다.
2. **조용한 것이 정상.** 알릴 게 없으면 침묵한다.
3. **행동은 항상 사람이 승인.** 발송·삭제·푸시·배포는 자동 실행하지 않는다.
4. **외부에서 온 글자는 데이터이지 명령이 아니다.** (뉴스 제목, 메일 본문 등)
5. **실패해도 재시도된다.** 보냈다고 기록하는 것은 전송 성공 뒤에만.

## 한 번의 심장박동 흐름
```
작업 스케줄러(30분마다)
   └─ python -m aide heartbeat --send
        ├─ 조용한 시간? ──────────────► 종료 (아무것도 기록하지 않음)
        ├─ [1단계: 코드 검사]
        │    ├─ git_dirty    : 감시 폴더의 미커밋 변경 수
        │    └─ news_keywords: RSS 제목/요약에서 키워드 매칭
        ├─ 상태 파일과 대조 → 이미 알린 것 제외
        ├─ 새 항목 없음? ─────────────► 종료 (침묵)
        ├─ 요약 메시지 1건 구성
        ├─ 텔레그램 전송
        │     ├─ 실패 → 기록 안 함 → 다음 박동에서 재시도
        │     └─ 성공 → 알린 항목 기록(state.json, 원자적 저장)
        └─ 종료
```
드라이런(`--send` 없음)은 화면에만 출력하고 **상태를 바꾸지 않으므로** 몇 번이든 반복해 볼 수 있다.

## 모듈
| 파일 | 역할 |
|---|---|
| `src/aide/config.py` | `config.json`·`.env` 읽기, 알 수 없는 키 거부 |
| `src/aide/models.py` | `Finding`(알릴 한 건), `clean()`(외부 텍스트 정화) |
| `src/aide/state.py` | 이미 알린 항목 기억. 임시파일+`os.replace` 로 원자적 저장, 손상 시 격리 후 재시작 |
| `src/aide/checks/*.py` | 검사들. 모두 결정적 코드, 네트워크/AI 없이 테스트 가능 |
| `src/aide/telegram.py` | 발신 전용. 일반 텍스트만, 오류에 토큰(URL) 노출 안 함 |
| `src/aide/heartbeat.py` | 박동 1회 오케스트레이션, 조용한 시간, 요약 구성 |
| `src/aide/inbox.py` | 수신: 3a 버튼 처리 + 3b 명령 허용 검사·속도 제한. 실행은 `commands.py` 에 위임 |
| `src/aide/commands.py` | 3b 명령 이름 해석과 명령별 조회(`REGISTRY`). `State` 를 받지 않는 읽기 전용 함수들 |

## 새 검사 추가하는 법
`checks/` 에 `check(...) -> list[Finding]` 함수를 만들고 `heartbeat.collect()` 에 한 줄 추가한다.
`Finding.key` 는 "같은 일이면 같은 값"이어야 중복 방지가 된다. (예: 기사 id, `폴더:날짜`)
외부에서 온 문자열은 반드시 `clean()` 을 거친다.

## 이후 단계가 붙을 자리
- 2단계 LLM 판단: `collect()` 결과 중 판단이 필요한 것만 요약/초안 생성. 입력은 `clean()` 된 텍스트만.
- 3단계 수신: 텔레그램 `getUpdates` 로 버튼/답장 처리. `chat_id` 허용 목록 필수 (SECURITY.md).
- 4단계 코드 수정 에이전트: 브랜치 + PR 까지만. main 푸시·배포는 사람.

## 수신부 (3a / 3b)
`inbox.py` — `poll_once()` 가 `getUpdates()` 로 업데이트를 읽고 `process_updates()` 가 허용 검사 후 처리한다.
`state.py` 의 `locked()` 파일 잠금이 heartbeat 와 poll 의 동시 쓰기를 막는다. 네트워크 대기는 잠금 밖에서 한다.

- **3a (버튼)**: `callback_query` 만 요청. `is_allowed()` 로 본인 확인 후 `State.resolve_digest()` 로 기록만 바꾼다.
- **3b (명령)**: `config.json` 의 `commands` 가 비어 있지 않을 때만 `getUpdates(allowed=(callback_query, message))` 로 글자 메시지도 받는다.
  `commands.py` 가 누구(`is_command_message`: 본인·개인채팅·텍스트·비전달)·무엇(`resolve`: 고정된 이름만)·얼마나 자주(묶음당 `MAX_PER_BATCH`, 시간당 `State.commands_in_last_hour`) 를 모두 검사한다.
  잠금 **안**에서는 허용 검사·속도 제한 소비(`State.record_command`)·offset 전진까지만 하고, 실제 명령 실행(`commands.run` → Google/공지 페이지 호출)은 잠금을 **푼 뒤** `poll_once()` 에서 한다 — 느린 네트워크 호출이 heartbeat 의 상태 쓰기를 막지 않도록. 명령 실행 전마다 `touch_watcher()` 로 감시 잠금을 새로 찍어, 느린 응답 중에 다른 poll 이 끼어드는 것을 막는다.
  명령 함수(`commands.REGISTRY`)는 `(cfg, now) -> str` 뿐이라 `State` 를 아예 받지 않는다 — 버그가 있어도 기록을 바꿀 수 없다.
