# 기술 선택

무엇을 쓰는지보다 무엇을 안 쓰기로 했는지가 더 중요하다. 아래 "쓰지 않는 것" 을 다시
논의하려면 근거(측정값)를 가져와야 한다.

## 쓰는 것

| 영역 | 선택 | 이유 |
|---|---|---|
| 웹 | FastAPI | API 와 화면을 한 프로세스에서 낸다 |
| 화면 | Jinja2 + HTMX | 빌드 단계 없음. 부분 갱신만 필요하다 |
| 스케줄 | APScheduler | 인프로세스. 브로커가 필요 없다 |
| 정적 크롤링 | httpx + BeautifulSoup | 대부분의 채용 페이지에 충분하다 |
| JS 렌더링 | Playwright (Python) | 사이트별 개별 승격. 기본값 아님 |
| LLM | 제공자 다섯 중 기능마다 선택 | [llm-providers.md](llm-providers.md) |
| DB | SQLite | 파일 하나. 볼륨 마운트로 영속화 |
| 파일 저장 | MinIO (S3 호환) | 로고 이미지. SQLite 가 담을 수 없다 |
| 알림 | ntfy | 새 공고 알림 하나. 계정도 앱도 필요 없다 |
| 품질 | ruff, mypy, pytest | |
| 배포 | Docker Compose | api 1개 + MinIO 1개 |

## 두 번째 컨테이너는 하나만 예외다

2026-08-28 에 MinIO 를 들였다. 거절하지 않은 이유는 둘이다. SQLite 는 파일 하나라 업로드된
이미지를 담을 수 없고, MinIO 는 S3 API 를 말하므로 나중에 실제 S3 로 바꾸는 것이 엔드포인트
설정 하나에 코드 변경이 없다.

이것이 유일한 예외다. 단일 프로세스가 이미 할 수 있는 일을 위한 두 번째 컨테이너는 여전히
거절한다 (`.claude/rules/core.md`).

## 쓰지 않는 것

| 안 쓰는 것 | 대신 | 다시 볼 조건 |
|---|---|---|
| Celery + Redis | APScheduler | 워크플로우가 인프로세스로 감당 안 되는 수치가 나왔을 때 |
| PostgreSQL | SQLite | 동시 쓰기 락이 실제로 문제가 됐을 때 |
| React 등 SPA | HTMX | 운영자 화면에 클라이언트 상태가 실제로 필요해졌을 때 |
| Node 크롤러 | Python 통일 | Python 으로 안 되는 사이트가 실제로 나왔을 때 |
| 별도 web 컨테이너 | api 하나 | 없음 |
| 인증·권한 | 비밀번호 하나 | 운영자가 2명 이상이 됐을 때 |
| 로그 파일·수집기 | 메모리 100줄 + `docker compose logs` | 지난 실행의 로그를 봐야 할 때 |

Node 를 따로 두면 프로세스와 배포가 두 스택으로 갈라진다. 1차는 Python 단일 스택이고, 정말
필요한 사이트가 나오면 그때 별도 워커로 검토한다. "나올 것 같아서" 미리 만들지 않는다.

`app/log_ring.py` 는 이 프로세스가 방금 낸 로그 100줄을 메모리에 들고 대시보드가 읽는다.
재시작하면 비는 것이 맞고, `docker compose logs` 를 대체하려는 것이 아니다. `app` 로거
하나에만 붙는다 — uvicorn 아래 라이브러리 로그까지 담으면 100줄이 접속·헬스체크 잡음으로
금방 밀려난다.

## 환경변수

`.env.example` 이 이름을 문서화한다. 값은 절대 커밋하지 않는다.

빈 문자열은 값으로 치지 않는다. 비워 두면 기본값으로 되돌아간다.

### 모델 제공자

| 이름 | 기본값 |
|---|---|
| GEMINI_API_KEY / GEMINI_MODEL | 없음 / `gemini-3.5-flash` |
| CLAUDE_API_KEY / CLAUDE_MODEL | 없음 / `claude-haiku-4-5-20251001` |
| GPT_API_KEY / GPT_MODEL | 없음 / `gpt-5.6-luna` |
| QWEN_API_KEY / QWEN_MODEL / QWEN_BASE_URL | 없음 / `qwen3.8-flash` / 국제 엔드포인트 |
| OLLAMA_API_KEY / OLLAMA_MODEL / OLLAMA_BASE_URL | 없음 / `gpt-oss:120b` / `https://ollama.com/v1` |
| SELECTOR_GENERATE_PROVIDER | `gemini` |
| SELECTOR_REPAIR_PROVIDER | `gemini` |
| CLASSIFY_PROVIDER | `gemini` |

키가 없으면 서버는 뜨고 그 제공자를 쓰는 기능만 실패한다. 서버 문제로 오진하기 쉬운 지점이다.

모델 ID 는 바뀐다. 여기 적힌 기본값은 그때의 값이고, 현재 값은 `app/config.py` 가 진실이다.

### 크롤링

| 이름 | 기본값 | 용도 |
|---|---|---|
| CRAWL_USER_AGENT | `job-crawler-automation (contact: unset)` | 이름과 연락처. 브라우저 위장 금지 |
| CRAWL_DELAY_SECONDS | 3.0 | 같은 호스트 요청 간 최소 간격 |
| CRAWL_TIMEOUT_SECONDS | 20.0 | 요청 1건 타임아웃 |
| CRAWL_MAX_RETRIES | 3 | transport 실패 재시도 횟수 |
| RENDER_TIMEOUT_SECONDS | 60.0 | 렌더 1회 상한 |

기본값은 보수적으로(느리게) 둔다. 늦게 끝나는 워크플로우는 괜찮고, IP 가 막히면 그 사이트를
영영 잃는다.

### 실행과 저장

| 이름 | 기본값 | 용도 |
|---|---|---|
| DATABASE_PATH | `./data/jobs.db` | SQLite 파일 경로 |
| MAX_CONCURRENT_RUNS | 3 | 전역 동시 실행 상한의 초기값 |
| FIRST_RUN_LIMIT | 20 | 워크플로우 첫 실행 항목 수 상한의 초기값. 0 은 상한 없음 |
| RUN_TIMEOUT_SECONDS | 600 | 워크플로우 실행 1회 상한 |

뒤의 셋 중 앞의 둘은 `app_settings` 에 값이 없을 때만 쓰인다. 한 번 들어간 뒤로는 DB 가
이기고, 환경변수를 나중에 고쳐도 저장된 값을 덮지 않는다 ([scheduling.md](scheduling.md)).

### 화면

| 이름 | 기본값 | 용도 |
|---|---|---|
| DISPLAY_TIMEZONE | `Asia/Seoul` | 화면에 시각을 그릴 때만 쓴다. 저장과 제공 API 는 UTC |
| ADMIN_PASSWORD | `1234` | 화면과 API 를 여는 비밀번호 하나 |
| BUILD_SHA | `unknown` | 빌드가 심는 커밋 SHA. `/health` 가 돌려준다 |

`ADMIN_PASSWORD` 가 기본값이면 공개 주소에서 잠기지 않은 것과 같다. 화면과 기동 로그가
그렇다고 알린다.

로고 저장소와 알림 설정에는 환경변수 짝이 없다. 운영자가 화면에서 정하는 값이다
([integrations.md](integrations.md)).
