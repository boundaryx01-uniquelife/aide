# 서버 이전 (Ubuntu 24.04, 공용 서버)

목표: PC 절전·꺼짐과 상관없이 07시 아침 인사, 08/18시 메일, 20시 저녁 정리가 제때 오게 한다.
aide 는 **포트를 열지 않는다**(밖으로 요청만). 표준 라이브러리만 써서 `pip install` 이 필요 없다 (Python 3.12 확인).

## 이 서버의 사정 (2026-10-05 점검)
- 이미 돌고 있는 것: nginx, docker(9000), pm2, 여러 python/node 서비스, 투자 브리핑·신호 타이머 등. 메모리 약 1GB, 스왑 600MB 사용 중.
- 방화벽(ufw) 켜짐. 다른 서비스는 대부분 root 로 돈다 → aide 는 **전용 계정 `aide`** 로 분리한다(메일 읽기 구글 토큰 보호).
- 서버 시간대가 UTC → 서비스에 `TZ=Asia/Seoul` 을 넣어 한국 시간으로 돌린다(서버 전체 시간대는 바꾸지 않는다).
- 텔레그램 봇은 프로그램마다 별개(사용자 확인). **aide 봇의 `getUpdates` 읽기는 한 곳만**: PC 와 서버가 동시에 읽으면 안 된다.
- 공지 사이트 3곳(pen.go.kr, kosac.re.kr, kipa.org)은 서버에서 200 으로 열림 확인.
- 아침·저녁의 "작업(커밋)" 항목은 PC 의 `watch_repos` 폴더를 본다. 서버에는 그 폴더가 없으므로 서버 `config.json` 은 `"watch_repos": []` 로 둔다 (그 항목의 처리는 로드맵 메모 참고).

## 1. 코드 올리기 (PC, PowerShell)
```powershell
cd C:\dev\aide
git push -u origin feat/notice-exclude      # 병합 후에는 main
```

## 2. 서버 준비 (서버, root)
```bash
adduser --disabled-password --gecos "" aide
install -d -m 700 -o aide -g aide /home/aide/.ssh
sudo -u aide ssh-keygen -t ed25519 -N "" -f /home/aide/.ssh/aide_deploy -C "aide-server-deploy"
cat /home/aide/.ssh/aide_deploy.pub
```
- 출력된 공개키(`.pub`)를 GitHub 저장소 → Settings → Deploy keys → Add (**Allow write access 는 끈다**, 읽기 전용).
```bash
sudo -u aide bash -c 'printf "Host github.com\n  IdentityFile ~/.ssh/aide_deploy\n  IdentitiesOnly yes\n" > ~/.ssh/config && chmod 600 ~/.ssh/config'
sudo -u aide bash -c 'ssh-keyscan github.com >> ~/.ssh/known_hosts'
sudo -u aide git clone -b feat/notice-exclude git@github.com:boundaryx01-uniquelife/aide.git /home/aide/aide
sudo -u aide mkdir -p /home/aide/aide/data
```

## 3. 비밀 파일 복사 (PC → 서버, PowerShell)
서버 `config.json` 은 PC 것을 복사한 뒤 `watch_repos` 만 `[]` 로 고친다. 파일 내용은 채팅·커밋에 올리지 않는다.
```powershell
cd C:\dev\aide
scp .env config.json root@서버IP:/home/aide/aide/
scp data\google_client.json data\google_token.json root@서버IP:/home/aide/aide/data/
```
```bash
# 서버 (root)
chown aide:aide /home/aide/aide/.env /home/aide/aide/config.json /home/aide/aide/data/google_*.json
chmod 600 /home/aide/aide/.env /home/aide/aide/config.json /home/aide/aide/data/google_*.json
sudo -u aide sed -i 's/^\(\s*"watch_repos":\).*/\1 [],/' /home/aide/aide/config.json   # 또는 직접 편집
```
- 서버에는 브라우저가 없어 `google-login` 을 못 하므로 PC 의 토큰 파일을 복사한다. 토큰 만료 시 PC 에서 다시 로그인한 뒤 같은 방식으로 복사한다.

## 4. 시험 (서버, 아직 전송 없음)
```bash
cd /home/aide/aide
sudo -u aide env TZ=Asia/Seoul PYTHONPATH=src python3 -m aide selfcheck
sudo -u aide env TZ=Asia/Seoul PYTHONPATH=src python3 -m aide notices
sudo -u aide env TZ=Asia/Seoul PYTHONPATH=src python3 -m aide morning
sudo -u aide env TZ=Asia/Seoul PYTHONPATH=src python3 -m aide evening --force
```
- `--send` 를 붙이지 않았으므로 텔레그램으로 나가지 않는다. 시각이 한국 시간으로 나오는지, 로그인·메일·캘린더가 "됨"인지 본다.

## 5. 전환 (PC 를 먼저 끄고, 상태 파일을 옮긴 뒤, 서버를 켠다)
PC (PowerShell)
```powershell
Disable-ScheduledTask -TaskName aide-heartbeat, aide-poll-watch
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*aide*poll*--watch*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
scp C:\dev\aide\data\state.json root@서버IP:/home/aide/aide/data/
```
서버 (root)
```bash
chown aide:aide /home/aide/aide/data/state.json && chmod 600 /home/aide/aide/data/state.json
cp /home/aide/aide/deploy/aide-heartbeat.service /home/aide/aide/deploy/aide-heartbeat.timer /home/aide/aide/deploy/aide-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aide-watch.service aide-heartbeat.timer
systemctl list-timers aide-heartbeat.timer --no-pager
```
- `state.json` 을 옮기는 것은 이미 보낸 알림이 서버에서 다시 오지 않게 하기 위해서다.

## 6. 확인과 되돌리기
```bash
systemctl status aide-watch --no-pager | head -5
tail -n 20 /home/aide/aide/data/aide.log
```
- 되돌리기: 서버 `systemctl disable --now aide-watch.service aide-heartbeat.timer`, PC `Enable-ScheduledTask -TaskName aide-heartbeat, aide-poll-watch`. 둘이 동시에 켜져 있으면 안 된다.
- 며칠 안정적이면 PC 쪽 작업을 영구 삭제하거나 비활성으로 둔다.

## 메모
- 코드 갱신: 서버에서 `sudo -u aide git -C /home/aide/aide pull`, 서비스는 `systemctl restart aide-watch`.
- 서버에서는 "어제 작업" 항목이 비게 된다. 해당 항목을 `watch_repos` 가 비어 있으면 생략하도록 바꾸거나, GitHub 에서 커밋을 읽도록 확장하는 것은 별도 작업(로드맵 F 이전에 결정).
- 보안: 서버 침해 시 구글 토큰·텔레그램 토큰이 노출될 수 있다. 전용 계정(700), 읽기 전용 배포 키, 열린 포트 없음으로 줄인다. 위험 수용 내용은 SECURITY.md 에 기록.
