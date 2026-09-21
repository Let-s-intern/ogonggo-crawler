# job-crawler-automation

채용공고 크롤링 자동화 백엔드. 최종 사용자용 채용공고 사이트가 아니라, 그 사이트에 넘길 데이터를
만드는 파이프라인이다.

```
채용 사이트들 ──fetch──> [이 서비스] ──REST──> 채용공고 사이트(별도)
                            │
                      운영자 웹 화면
```

운영자는 한 명이라고 가정한다. 인증·권한은 이 서비스의 범위 밖이다.

## 파이프라인

URL 을 등록하면 LLM 이 CSS 셀렉터를 만들고, 테스트 실행이 실제 페이지에서 그 셀렉터를 검증하고,
통과하면 워크플로우로 승격돼 주기적으로 돈다. 수집된 원문은 정규화를 거쳐 소비 측 REST API 로
나간다.

```
URL 입력 → 셀렉터 생성(LLM) → 테스트 실행 → 워크플로우 등록 → 주기 실행
                                                                  ↓
                                        raw_jobs → 정규화 → normalized_jobs → 제공 API
```

`raw_jobs` 는 append-only 다. 정규화 규칙이 잘못돼도 원본은 그대로 남아 있어 규칙만 고쳐
다시 돌리면 된다. 자세한 구조는 [`.claude/docs/architecture.md`](.claude/docs/architecture.md)에
있다.

부트캠프는 이 흐름을 타지 않는다. 새싹(SeSAC) 오프라인 과정 하나만 모으므로 고정 파서로 읽고,
교육개요 이미지를 AI 로 정리해 오공고 `/api/v1/internal/bootcamps` 로 등록·교체한다. 코드는
`app/bootcamp/`, 화면은 위 메뉴 `부트캠프`, 표는 `migrations/0044_bootcamps.sql` 이다.

## 스택

FastAPI 프로세스 하나, SQLite 파일 하나, APScheduler 인프로세스 스케줄러, Jinja2 + HTMX 서버
렌더링 화면. 빌드 단계가 없다. Celery·Redis·별도 web 컨테이너는 두지 않는다 — 이유는
[`.claude/docs/tech-stack.md`](.claude/docs/tech-stack.md)에 있다.

회사 로고 파일은 MinIO(S3 호환) 컨테이너 하나에 둔다. SQLite 한 파일로는 이미지를 감당할 수
없어서 둔 유일한 예외다.

## 로컬에서 띄우기

```bash
cp .env.example .env   # 필요한 값만 채운다. 비워 둬도 뜬다
docker compose up -d --build
```

| 주소 | 화면 |
|---|---|
| `http://localhost:8000/` | 대시보드 |
| `http://localhost:8000/crawlers` | 크롤러 등록·셀렉터 생성 |
| `http://localhost:8000/workflows` | 워크플로우 목록 |
| `http://localhost:8000/review` | 수집 데이터 검수 |
| `http://localhost:8000/docs` | FastAPI 자동 문서 |

Docker 없이 직접 띄우려면:

```bash
pip install -e ".[dev]"
python -m app.cli migrate up
uvicorn app.main:app --reload
```

## 개발

```bash
ruff format .        # 포맷
ruff check .          # 린트
mypy app              # 타입체크
pytest -q -m "not live"   # 테스트. 실사이트를 때리는 live 테스트는 기본에서 뺀다
```

커밋마다 이 네 가지를 CI(`.github/workflows/ci.yml`)가 그대로 돈다.

## 문서

- [`.claude/docs/architecture.md`](.claude/docs/architecture.md) — 전체 구조, 파이프라인 단계
- [`.claude/docs/data-model.md`](.claude/docs/data-model.md) — 테이블 정의, 상태 전이
- [`.claude/docs/api-contract.md`](.claude/docs/api-contract.md) — 소비 측이 받는 제공 API
- [`.claude/docs/tech-stack.md`](.claude/docs/tech-stack.md) — 기술 선택과 그 이유
- [`.claude/rules/`](.claude/rules) — 이 저장소를 건드릴 때 지키는 제약
