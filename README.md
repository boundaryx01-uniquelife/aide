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

## 뉴스 선정 기준
1. `news_feeds` 의 RSS 에 있는 기사만 후보가 됩니다.
2. 제목·요약에 `news_keywords` 중 하나가 (대소문자 무시, 부분 일치) 들어 있어야 합니다.
3. 발행 시각이 `news_max_age_hours`(기본 36)보다 오래된 기사는 제외합니다. 날짜를 읽을 수 없는 기사는 제외하지 않습니다.
4. 같은 기사(ID/주소)와, 매체명을 뗀 제목이 같은 기사는 한 번만 보냅니다. 다른 매체가 같은 제목으로 다시 내도 보내지 않습니다. 표현이 조금이라도 다르면 다른 기사로 봅니다.
5. 새 기사 순서로(최신 먼저) 한 번에 `max_items_per_digest` 건까지 보냅니다.
## 로그 파일
- `data\aide.log`: 30분 점검(알림, 아침 인사)의 기록
- `data\poll_watch.log`: 버튼 즉시 감시의 기록 (감시가 파일을 계속 열어 두므로 분리)
둘 다 UTF-8 입니다. PowerShell 에서는 `Get-Content data\aide.log -Tail 20 -Encoding UTF8` 로 읽으세요.

## 아침 인사 (1.5)
`python -m aide morning` 은 어제 커밋 요약과 커밋 안 된 변경이 많은 폴더를 한 줄씩 보여 줍니다 (기본은 미리보기, 전송 안 함).
`--send` 로 텔레그램에 보내며 하루에 한 번만 갑니다. 조용한 시간(기본 22:30~07:00)에는 건너뜁니다.
설정 시 날씨(Open-Meteo)와 오늘 구글 캘린더 일정(읽기 전용)도 함께 보냅니다 → `docs/GOOGLE_SETUP.md`. 일정/날씨 조회가 실패해도 인사는 갑니다.
30분 점검 작업(`run_heartbeat.bat`)이 마지막에 `morning --send` 를 함께 실행하므로 별도 예약이 필요 없습니다. 조용한 시간이 끝난 뒤 첫 점검(최대 30분 안)에 하루 한 번만 전송됩니다. (`register_morning.bat` 은 관리자 권한이 필요해 쓰지 않습니다.)

## 기관 공지·마감 감시
`config.json` 의 `notice_pages` 에 로그인 없이 열리는 공지 목록 페이지 주소를 넣으면, 30분 점검 때 새 글을 알려 줍니다.
- 제목에 `notice_keywords`(공모·모집·신청·접수·연수·공고·대회·전시회·지원사업·발명·메이커 등)가 있어야 합니다.
- 행에 `~2026-10-12` 같은 접수 기간이 보이면 마감일을 알려 주고, 지난 건 제외, 마감 3일 전·1일 전에 한 번씩 다시 알립니다.
- 처음 등록하면 기존 글이 페이지당 최대 `notice_max_per_page`(기본 5)건 한 번 올 수 있습니다.
- 사이트마다 HTML 이 달라 먼저 시험하세요: `python -m aide notices` (전송·기록 없음). 0건이거나 엉뚱하면 알려 주세요(사이트별로 조정).
- 서버에서 완성된 HTML 로 주는 페이지만 읽습니다(자바스크립트로 그리는 페이지는 못 읽음). 로그인 필요한 게시판은 지원하지 않습니다.

## 메일 알림 (읽기 전용, 허용 발신자만)
`config.json` 에 `"mail_enabled": true`, `"mail_senders": ["pen.go.kr", "someone@site.kr"]` 를 넣으면 **그 발신자**가 보낸 **안 읽은 받은편지함 메일**(최근 2일)의 `보낸 사람: 제목` 한 줄만 알려 줍니다.
- 본문·첨부·스니펫은 가져오지 않습니다(헤더 From/Subject 만 요청). 허용 목록에 없는 메일은 조회 자체를 하지 않습니다.
- 알림에는 Gmail 링크가 붙고, 눌러서 열어 보는 것은 선생님입니다. aide 는 메일을 읽음 처리·삭제·전송하지 않습니다(권한이 읽기 전용).
- 메일 제목이 텔레그램(외부 서비스)으로 전달됩니다. 민감한 발신자는 목록에 넣지 마세요.
- 설정: `docs/GOOGLE_SETUP.md` 의 "Gmail 추가" 를 따라 Gmail API 를 켜고 `python -m aide google-login` 을 **다시** 실행해야 합니다.
