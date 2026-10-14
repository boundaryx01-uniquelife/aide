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

## 계정이 여러 개일 때 (메일·일정 합쳐서 받기)
1. 계정마다 Google Auth Platform → 대상(Audience) → **테스트 사용자**에 그 주소를 추가한다. (학교·기관 관리 계정은 관리자가 외부 앱을 막았으면 로그인이 거부될 수 있다.)
2. `config.json` 에 추가:
   ```json
   "google_accounts": [
     {"name": "개인", "token_path": "data/google_token.json", "email": "내주소@gmail.com"},
     {"name": "학교", "token_path": "data/google_token_school.json", "email": "학교주소@gmail.com"}
   ]
   ```
   `email` 은 선택(메일 링크가 맞는 계정으로 열리게 함). 이름은 20자 이하·중복 불가, 최대 8개.
3. 계정마다 PC 에서 로그인: `python -m aide google-login --account 개인` / `--account 학교` (브라우저에서 해당 계정 선택).
4. 서버는 토큰 파일을 복사한 뒤 `chown aide:aide`, `chmod 600`.
- 메시지에는 `[이름]` 표시가 붙고, 두 계정에 같은 일정이 있으면 `[개인·학교]` 로 한 번만 나온다. 한 계정이 실패해도 나머지는 정상 표시된다.
- 계정이 하나면 `google_accounts` 를 비워 두면 기존과 동일하게 동작한다.

### 읽을 캘린더 고르기 (`calendars`)
기본적으로는 계정의 **기본 캘린더만** 읽는다. 다른 캘린더(예: `work`, `발명센터`)의 일정도 보려면 계정 항목에 `calendars` 로 이름을 적는다.
```json
{"name": "기본", "token_path": "data/google_token.json", "calendars": ["기본", "발명센터", "Family"]},
{"name": "개인", "token_path": "data/google_token_personal.json", "calendars": ["기본", "work"]},
{"name": "학교", "token_path": "data/google_token_school.json"}
```
- `"기본"`(또는 `"primary"`)은 그 계정의 기본 캘린더. 나머지는 구글 캘린더 왼쪽 목록에 보이는 캘린더 이름 그대로(캘린더 ID도 가능).
- 적지 않은 캘린더는 읽지 않는다. `calendars` 를 생략하면 기본 캘린더만 읽는다. `"work"` 만 적으면 기본 캘린더는 읽지 않는다.
- 이름이 틀리면 서버 로그에 `캘린더를 찾지 못했어요: 이름` 이 남고 그 캘린더만 건너뛴다. 모두 못 찾으면 기본 캘린더를 읽는다.
- 같은 일정이 두 캘린더에 있으면 한 번만 나온다. 로그인을 다시 할 필요는 없다.
