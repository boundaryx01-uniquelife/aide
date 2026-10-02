# aide

먼저 말을 걸어주는 개인 비서의 **1단계**.
정해진 주기로 깨어나서, 코드로 싸게 검사하고, 알릴 게 있을 때만 텔레그램으로 알려줍니다.

> 현재 단계: **1단계 (알림 전용)** — AI 호출 없음, 메일 접근 없음, 수신(명령) 없음.
> 전체 계획은 [docs/ROADMAP.md](docs/ROADMAP.md), 설계는 [ARCHITECTURE.md](ARCHITECTURE.md), 보안 규칙은 [SECURITY.md](SECURITY.md).

## 1단계가 하는 일
| 검사 | 내용 | 중복 방지 |
|---|---|---|
| 작업 폴더 | 감시 중인 git 폴더에 커밋 안 된 변경이 N개 이상이면 알림 | 폴더당 하루 1회 |
| 뉴스 키워드 | RSS 피드 제목/요약에 키워드가 있으면 알림 | 기사당 1회 |

- 조용한 시간(기본 22:30–07:00)에는 점검만 건너뜁니다. 쌓인 알림은 아침에 한 번에 옵니다.
- 알릴 게 없으면 **아무 말도 하지 않습니다.** 조용한 것이 정상입니다.

## 빠른 시작 (Windows)
```bat
cd C:\dev\aide
copy config.example.json config.json
copy .env.example .env
```
1. `config.json` 을 열어 감시할 폴더와 키워드를 수정합니다.
2. `.env` 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 를 넣습니다. (**.env 는 git 에 올라가지 않습니다**)
3. 점검과 시험 실행:
```bat
set PYTHONPATH=%CD%\src
python -m aide selfcheck
python -m aide heartbeat            :: 드라이런. 화면에만 출력, 전송/상태 변경 없음
python -m aide heartbeat --send     :: 실제 전송
```
4. 정상이면 30분마다 자동 실행 등록: `scripts\register_task.bat`

## 테스트
```bat
set PYTHONPATH=%CD%\src
python -m unittest discover -s tests -v
```
외부 의존성 없음 (Python 3.9+ 표준 라이브러리만 사용).

## 텔레그램 봇 준비
1. 텔레그램에서 `@BotFather` → `/newbot` → 토큰 받기
2. 만든 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `chat.id` 확인
3. 두 값을 `.env` 에만 저장 (코드·설정 파일·채팅에 붙여넣지 말 것)

## 버튼 응답 (3a)
알림 아래의 [확인] [무시] [나중에] 버튼은 `python -m aide poll` 이 받아 기록합니다.
- 예약 작업(`scripts\run_heartbeat.bat`)이 점검 직전에 `poll` 을 먼저 실행하므로 최대 30분 안에 반영됩니다.
- 즉시 반영하려면 `scripts\register_poll_watch.bat` 로 백그라운드 감시를 등록하세요(창 없이, 관리자 권한 불필요, 꺼지면 1분 안에 자동 재시작). 지금 바로 켜려면 `schtasks /Run /TN "aide-poll-watch"`. 감시가 켜져 있으면 예약된 `poll` 은 자동으로 건너뜁니다(텔레그램은 동시에 한 곳만 읽을 수 있음).
- 버튼은 기록만 바꿉니다. 명령 실행은 3b 에서 별도로 엽니다.
