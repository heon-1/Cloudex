# Cloudex

Telegram에서 로컬 Codex 작업을 실행하고 관리하는 개인용 봇과 미니앱입니다.
Python 서버와 HTML/CSS/JavaScript 화면으로 구성되며, Python 외부 패키지나 프런트엔드 빌드가 필요하지 않습니다.

## 기능

- 세션별 작업 폴더와 대기열, 여러 세션의 동시 실행
- 대화 검색, 세션 전환·이름 변경·보관·복원·삭제 (삭제 시 작업 파일은 유지)
- 메시지를 대기열에 추가하거나 실행 중인 작업에 바로 전달
- 작업 진행 상황과 대화 내역 표시, 작업 중단
- 모델·추론 수준·승인 정책 설정
- 현재 Codex 계정 확인·저장 및 기기 코드를 이용한 다시 로그인
- 실행 승인과 추가 질문 응답
- 작업 파일 업로드·다운로드
- Telegram 미니앱 인증과 소유자 페어링

## 실행 준비

- Linux 및 Python 3.12
- `codex app-server`를 지원하며 인증이 완료된 Codex CLI (`codex`가 실행 사용자의 `PATH`에 있어야 합니다.)
- Telegram BotFather에서 발급받은 봇 토큰
- 미니앱용 공개 HTTPS 주소 또는 Cloudflare Quick Tunnel

```bash
git clone git@github.com:heon-1/Cloudex.git
cd Cloudex
python3 setup.py
python3 bot.py
```

`setup.py`에서 토큰을 입력하면 비공개 `config.json`을 생성하고 페어링 명령을 출력합니다.
봇이 실행 중일 때 Telegram의 해당 봇과 개인 대화에서 출력된 `/pair …` 명령을 보내세요.
페어링 후 `/help`로 사용법을 확인할 수 있습니다.

동일한 봇 토큰으로 여러 인스턴스를 동시에 실행하지 마세요.

## 미니앱 HTTPS 연결

기본 웹 서버 주소는 `127.0.0.1:8787`입니다. Telegram에서 미니앱을 열려면 HTTPS 연결이 필요합니다.

### 임시 주소로 시작

Ubuntu/Debian에서 다음 스크립트는 `cloudflared`를 설치하고 systemd 서비스를 시작하거나 재시작합니다.
앞에서 직접 실행한 `bot.py`는 먼저 종료하세요.

```bash
sudo bash install-quick-tunnel.sh
```

임시 주소는 터널 재시작 시 바뀔 수 있습니다. 서버가 HTTPS 상태를 확인하면 Telegram의 미니앱 메뉴를 갱신합니다.
페어링한 봇에 `/app`을 보내 미니앱을 여세요.

### 도메인 사용

도메인의 DNS를 서버로 연결하고 공개 포트 80·443을 열어둡니다.
다음 스크립트는 systemd 서비스를 설치한 뒤 Caddy로 HTTPS 프록시를 구성합니다.

```bash
sudo bash install-service.sh
sudo bash publish-miniapp.sh cloudex.example.com
```

이미 HTTPS 프록시가 있다면 해당 주소를 `127.0.0.1:8787`로 연결하고 설정을 적용하세요.

```bash
python3 configure-web.py https://cloudex.example.com
sudo systemctl restart cloudex.service
```

## 검증

테스트는 임시 폴더와 모의 Telegram/Codex 클라이언트를 사용합니다.

```bash
python3 -m unittest discover -v
```

## 파일 구성

| 경로 | 용도 |
| --- | --- |
| `bot.py` | Telegram 봇, Codex 연결, 세션·작업 관리 |
| `accounts.py` | 계정 정보 저장과 Codex 기기 코드 로그인 |
| `web.py` | 미니앱 HTTP 서버와 인증된 API |
| `tunnel.py` | 임시 HTTPS 터널과 메뉴 갱신 |
| `static/` | 미니앱 화면과 클라이언트 코드 |
| `setup.py`, `configure-web.py` | 토큰 및 HTTPS 주소 설정 |
| `install-*.sh`, `publish-miniapp.sh` | Linux 서비스 및 HTTPS 설치 |
| `test_*.py` | 봇·웹 API·터널 회귀 테스트 |

`config.json`, `state.json`, 세션 작업 폴더 및 비밀키는 Git에서 제외합니다.
기존 실행 환경의 토큰과 대화 상태는 저장소에 포함하지 않습니다.

설정의 **계정 저장**은 현재 계정 정보를 확인·갱신하고 프로필을 서버에 저장합니다.
로그인 자격 증명은 Codex의 기존 자격 증명 저장소에서 관리하며, 미니앱이나 Git으로 전송하지 않습니다.
**다시 로그인**을 누르면 ChatGPT 로그인 페이지와 일회용 코드가 표시됩니다.
로그인을 마치거나 취소할 때까지 새 작업 접수를 멈추며, 실행 중이거나 대기 중인 작업이 있으면 로그인 시작을 제한합니다.
인증 흐름은 [공식 Codex App Server 문서](https://learn.chatgpt.com/docs/app-server#authentication)를 따릅니다.
