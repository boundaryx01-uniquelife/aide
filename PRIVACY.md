# 개인정보처리방침 (Privacy Policy)

**aide** 는 한 사람(이 저장소의 소유자)이 자기 일정·메일 알림을 받으려고 직접 만들어 쓰는 개인 비서 프로그램입니다.
다른 사람에게 배포하거나 서비스로 제공하지 않으며, 로그인하는 사람은 소유자 본인과 소유자가 직접 등록한 테스트 계정뿐입니다.

## 어떤 정보에 접근하나
Google 로그인 때 아래 **읽기 전용** 권한만 요청합니다.

| 권한 | 읽는 것 | 읽지 않는 것 |
|---|---|---|
| Google Calendar (`calendar.readonly`) | 일정의 제목, 날짜·시각 | 일정 수정·삭제, 참석자 정보 |
| Gmail (`gmail.readonly`) | 메일의 **보낸 사람과 제목**, 광고·정기 메일 표시 헤더의 유무 | 메일 본문, 첨부 파일. 메일을 읽음 처리·삭제·전송하지 않음 |

## 어디에 쓰나
- 읽은 일정·메일 제목을 소유자 본인의 텔레그램 채팅으로 보내는 알림(아침 인사, 메일 요약, 저녁 정리)을 만드는 데에만 씁니다.
- 광고·판매·프로파일링에 쓰지 않습니다.

## 어디에 저장하나
- 로그인 토큰과 알림 기록(이미 알린 항목, 사용자가 정한 발신자 규칙)은 **소유자가 직접 관리하는 서버의 파일**에만 저장합니다. 파일은 서버 전용 계정만 읽을 수 있게 하고 깃 저장소에는 올리지 않습니다.
- 메일 본문은 저장하지 않습니다.

## 누구에게 전달되나
Google 에서 읽은 정보는 아래 외에는 누구에게도 팔거나 공유하지 않습니다.

1. **텔레그램**: 알림 문구가 소유자 본인의 텔레그램 채팅으로 전달됩니다.
2. **LLM 서비스(선택 기능)**: 소유자가 직접 켠 경우에만, 알림의 중요도를 고르기 위해 메일 보낸 사람·제목, 공지·일정 제목이 Anthropic API 로 전달됩니다. 이 기능은 기본이 꺼져 있고, 계정별로 제외할 수 있습니다. 해당 서비스의 약관이 적용됩니다.
3. **Open-Meteo**: 날씨 조회를 위해 대략의 지역 좌표가 전달됩니다(개인 식별 정보 없음).

## 삭제와 권한 해제
- 연결은 언제든 [Google 계정의 서드파티 연결 관리](https://myaccount.google.com/connections)에서 해제할 수 있고, 해제하면 aide 는 더 이상 읽을 수 없습니다.
- 서버에 저장된 토큰·기록 파일을 지우면 저장된 정보가 모두 사라집니다.

## 문의
이 저장소(GitHub `boundaryx01-uniquelife/aide`)의 소유자에게 이슈로 문의할 수 있습니다.

---

## Summary (English)
aide is a personal assistant that one individual (the repository owner) runs for their own use; it is not distributed or offered as a service.
It requests read-only access to Google Calendar (event titles and times) and Gmail (message sender and subject headers only — never message bodies or attachments)
solely to build notifications that are sent to the owner's own Telegram chat. Tokens and notification records are stored only on the owner's own server.
Google user data is not sold, used for advertising, or shared, except: (1) the owner's own Telegram chat, (2) an optional, off-by-default feature that sends
sender/subject/event titles to the Anthropic API when the owner explicitly enables it, and (3) coarse location coordinates to Open-Meteo for weather.
Access can be revoked at any time at https://myaccount.google.com/connections.
