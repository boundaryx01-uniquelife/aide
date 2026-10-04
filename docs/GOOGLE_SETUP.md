# Google 캘린더 연결 (읽기 전용)

아침 인사에 오늘 일정을 넣기 위한 1회 설정입니다. 비밀값은 `data\` 폴더(깃 제외)에만 둡니다.

1. https://console.cloud.google.com 에서 새 프로젝트 만들기 (예: `aide`)
2. "API 및 서비스 → 라이브러리"에서 **Google Calendar API** 사용 설정
3. "OAuth 동의 화면": 외부, 앱 이름 `aide`, **테스트 사용자에 본인 계정 추가** (게시하지 않아도 됨)
4. "사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID" → 유형 **데스크톱 앱**
5. JSON 다운로드 후 `C:\dev\aide\data\google_client.json` 으로 저장 (이름 그대로)
6. PowerShell:
   ```powershell
   cd C:\dev\aide
   python -m aide google-login
   ```
   브라우저에서 계정 선택 → "확인되지 않은 앱" 경고는 본인 앱이므로 고급 → 계속 → **캘린더 읽기** 권한 허용
7. `config.json` 에 `"calendar_enabled": true` (날씨는 `"weather_enabled": true`, 위치는 `latitude`/`longitude`)
8. 확인: `python -m aide selfcheck`, `python -m aide morning` (미리보기)

참고
- 요청 권한은 `calendar.readonly` 하나뿐입니다. 일정 생성·수정·삭제는 불가능합니다.
- 테스트 모드 앱의 로그인은 7일 뒤 만료될 수 있습니다. 그때 `google-login` 을 다시 실행하세요 (인사에는 "불러오지 못했어요"만 표시).
- 로그인 취소: https://myaccount.google.com/permissions 에서 aide 제거 + `data\google_token.json` 삭제.
- `google_client.json`, `google_token.json` 은 채팅·커밋에 올리지 마세요.

## Gmail 추가 (메일 알림용, 읽기 전용)
1. Google Cloud 콘솔에서 **Gmail API** 도 사용 설정 (검색창에 `Gmail API`)
2. 같은 프로젝트·클라이언트를 그대로 사용합니다. 대상(테스트 사용자)도 이미 설정되어 있으면 추가 작업 없음
3. PowerShell: `python -m aide google-login` 을 다시 실행 → 동의 화면에 **캘린더 읽기**와 **Gmail 읽기** 두 권한이 보입니다
4. `config.json`: `"mail_enabled": true`, `"mail_senders": ["pen.go.kr"]`
5. 시험: `python -m aide heartbeat` (미리보기). 안 읽은 허용 발신자 메일이 있으면 "메일 (허용 발신자)" 묶음으로 나옵니다.
- 로그인을 다시 하기 전에는 메일 기능이 "권한 없음"으로 건너뛰어지고 다른 알림은 정상 동작합니다.
- 권한 철회: https://myaccount.google.com/permissions
