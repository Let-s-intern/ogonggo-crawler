# 아키텍처

전체 구조와 모듈 배치. 흐름별 상세는 각 문서로 나눠 두었고 여기서는 되풀이하지 않는다.

## 이 서비스의 위치

최종 사용자용 채용공고 사이트가 아니다. 그 사이트에 넘길 데이터를 만드는 백엔드
파이프라인이다.

```
채용 사이트들 ──fetch──> [이 서비스] ──REST──> 오공고 백엔드(별도)
                            │
                      운영자 웹 화면
```

운영자는 한 명이라고 가정한다. 계정도 권한도 없고, 화면과 API 를 여는 비밀번호 하나가 전부다
(`ADMIN_PASSWORD`, `app/api/auth.py`).

## 단계가 일곱이다

각 단계는 다음 단계에 넘기는 것 외에는 서로를 모른다.

| 단계 | 입력 | 출력 | 문서 |
|---|---|---|---|
| 등록 | 목록 URL | 셀렉터, 수집 모드, API 설정 | [registration.md](registration.md) |
| 수집 | 셀렉터와 경로 | `raw_jobs` 행 | [fetching.md](fetching.md), [crawl-run.md](crawl-run.md) |
| 스케줄링 | 워크플로우 주기 | 주기적 실행 | [scheduling.md](scheduling.md) |
| 정규화 | `raw_jobs` | `normalized_jobs` | [normalization.md](normalization.md) |
| 분류 | `raw_jobs` 의 본문 | `job_classifications` | [classification.md](classification.md) |
| 제공 | `normalized_jobs` | REST 응답 | [api-contract.md](api-contract.md) |
| 전달 | `normalized_jobs` | 오공고 백엔드 | [spring-delivery.md](spring-delivery.md) |

마지막 단계는 아직 구현 전이다.

정규화가 raw 를 건드리지 않는 것이 이 구조의 핵심이다. 잘못된 정규화 규칙 하나가 수집 데이터를
영구 손상시키지 않는다 (`.claude/rules/data-safety.md`).

## 프로세스 구성

컨테이너 하나, 프로세스 하나다. 두 번째 컨테이너는 로고 파일을 담는 MinIO 하나뿐이고 그것이
예외의 전부다 (`.claude/rules/core.md`, [tech-stack.md](tech-stack.md)).

```
api (FastAPI)
├── REST API          운영 화면용 + 외부 제공용
├── Jinja2 + HTMX     서버 렌더링 화면. 빌드 단계 없음
├── APScheduler       워크플로우 주기 실행. 인프로세스
└── 크롤링 워커        같은 프로세스의 async 태스크
    └── SQLite (볼륨 마운트된 파일 1개)
```

기동할 때 하는 일은 `app/main.py` 의 `lifespan` 에 있다. 기본 비밀번호 경고, 지난 프로세스가
남긴 미완 실행 정리, 스케줄러 등록 셋이다. 스키마 적용은 여기가 아니라 컨테이너가 uvicorn
앞에서 따로 돌린다.

## 폴더 배치

```
app/
├── main.py             FastAPI 앱, 라우터 등록, 기동 시 뒷정리
├── config.py           환경변수
├── db.py               SQLite 연결, 마이그레이션 러너
├── settings.py         DB 에 저장되는 운영 설정 (동시 실행 상한, 첫 실행 상한)
├── scheduler.py        APScheduler 등록·갱신·동시성 상한
├── companies.py        companies 표 하나를 가진 저장소. 로고가 붙는 곳
├── taxonomy.py         job_taxonomy 표. 분류가 고르는 직무 목록
├── cli.py              마이그레이션 명령
├── log_ring.py         최근 로그 100줄. 대시보드가 읽는다
│
├── crawler/            수집. 밖으로 나가는 요청은 fetcher.py 하나만 만든다
│   ├── fetcher.py      공용 fetch 클라이언트. robots·딜레이·재시도
│   ├── collect.py      이 실행이 목록·상세를 무엇으로 가져올지 고른다
│   ├── api_source.py   JSON·HTML 조각 API 목록과 상세
│   ├── playwright.py   브라우저 렌더
│   ├── parser.py       셀렉터 JSON 적용
│   ├── shell.py        정적 HTML 이 껍데기인지 판정
│   ├── deadline.py     마감 지난 공고를 건너뛸지 판정
│   ├── hashing.py      중복 감지 content hash
│   ├── failures.py     실패를 error_class 로 옮긴다
│   ├── click_probe.py  등록할 때 항목을 눌러 상세 도달을 판정
│   └── runner.py       실행 1회 = crawl_runs 행 하나
│
├── selector/           등록. HTML 정제, 생성, 검증, 수리
│   ├── cleaner.py      LLM 입력용 정제·샘플링
│   ├── generator.py    셀렉터 생성
│   ├── repair.py       실패한 필드만 다시 요청
│   ├── verify.py       생성 즉시 그 HTML 에 적용
│   ├── narrow.py       넓게 잡은 항목 셀렉터를 좁힌다
│   ├── link.py         항목에서 상세 URL 을 뽑는 판정 한 곳
│   ├── discovery.py    상세로 가는 길을 스스로 찾는다
│   ├── list_api.py     관찰한 요청에서 목록 API 를 찾는다
│   ├── detail_path.py  클릭 뒤 나간 요청을 상세 설정으로
│   ├── link_probe.py   공고마다 다른 주소 형식을 만든다
│   ├── schema.py       셀렉터 JSON 스키마와 검증
│   └── api_schema.py   api_config_json 스키마와 검증
│
├── classify/           본문을 아홉 칸으로 나눈다
│   ├── batch.py        분류 실행. 상한 안에서 돈다
│   ├── classifier.py   공고 하나를 나눈다
│   ├── schema.py       응답 스키마와 검증
│   ├── grounding.py    받은 값에 원문 근거가 있는지 본다
│   └── store.py        job_classifications / job_field_suggestions
│
├── normalize/          raw_jobs 를 읽고 normalized_jobs 에만 쓴다
│   ├── engine.py       규칙 적용
│   ├── rules.py        규칙 타입과 설정 스키마
│   └── backfill.py     수동 재정규화
│
├── llm/                제공자 다섯. 분기하는 코드는 항목 안에만 있다
│   ├── base.py         제공자와 무관한 것만
│   ├── providers.py    이름 하나를 항목 하나로
│   ├── gemini.py / claude.py / openai_compat.py
│   ├── settings.py     키와 모델 설정
│   └── log.py          llm_calls 기록
│
├── side/               크롤링과 따로 도는 작업의 설정과 실행 기록
│   ├── store.py        side_workflows
│   ├── runs.py         side_runs
│   └── runner.py       부가 실행 1회
│
├── notify/             ntfy 알림
├── storage/            S3 호환 로고 저장소
├── deliver/            오공고 전달 설정. 전송 코드는 아직 없다
│
├── api/                라우터. ui_ 로 시작하는 것이 화면이다
└── templates/          Jinja2
tests/
├── fixtures/           저장된 HTML. 파서 테스트는 여기만 본다
└── ...
migrations/             스키마는 이 파일들로만 바뀐다
seeds/                  씨앗 데이터와 사이트 실측
```

## 라우터가 두 종류다

`app/api/` 안에서 `ui_` 로 시작하는 것이 화면(HTML 조각)이고 나머지가 JSON API 다.
등록 순서가 `app/main.py` 에 있고, API 라우터를 먼저 붙인다 — `/api/...` 가 먼저 잡혀야 한다.

조각 요청의 실패는 200 과 오류 조각으로 나간다. HTMX 가 4xx·5xx 를 갈아 끼우지 않아 그대로
두면 화면이 조용해진다. `/api/...` 의 상태 코드는 건드리지 않는다.

잠금은 라우트 등록이 끝난 뒤에 건다. 열어 두는 것은 `/health` 와 로그인 자리뿐이고 나머지는
전부 잠긴다 — 새 라우트가 생겨도 기본이 잠김이다.

## 설정이 두 곳에 있다

| 어디 | 무엇 | 누가 정하나 |
|---|---|---|
| 환경변수 (`app/config.py`) | 배포가 정하는 값 | 배포 |
| `app_settings` 표 | 돌아가는 중에 바꾸는 값 | 운영자, 화면에서 |

`app_settings` 를 읽는 저장소가 다섯으로 갈려 있다 — `app/settings.py`(정수 둘),
`app/llm/settings.py`, `app/notify/settings.py`, `app/storage/settings.py`,
`app/deliver/settings.py`. 표는 하나고 저장소만 나눈 것이다. `/api/settings` 가
`dict[str, int]` 를 내보내는 API 에 묶여 있어서, 문자열 값을 그 위에 얹으면 이미 있는 화면이
흔들린다 ([integrations.md](integrations.md)).

`MAX_CONCURRENT_RUNS` 와 `FIRST_RUN_LIMIT` 만 환경변수 짝이 있다. 값이 아직 없을 때 한 번
채워 넣고, 그 뒤로는 DB 가 이긴다. 나머지 설정에는 짝을 두지 않는다 — 같은 설정이 두 곳에
생기면 어느 쪽이 진실인지 매번 확인해야 한다.

## 스키마는 마이그레이션으로만 바뀐다

`migrations/` 의 파일이 유일한 경로다. DB 파일을 지우고 새로 만드는 길은 두지 않는다.
SQLite 파일은 Docker 볼륨에 있는 실제 수집 데이터이고, 그 수집은 다시 재생할 수 없다
(`.claude/rules/data-safety.md`).

```
python -m app.cli migrate up
python -m app.cli migrate down --steps 1
python -m app.cli migrate status
```

지금까지 26개다. 어느 마이그레이션이 무엇을 왜 바꿨는지는 각 파일의 머리 주석과
[data-model.md](data-model.md) 에 있다.

## 배포에 무엇이 떠 있는지 아는 길

`/health` 가 `{"status": "ok", "build": <커밋 SHA>}` 를 돌려준다. 이미지 태그를 커밋 SHA 로
고정하지 않으므로 이 값이 유일한 길이다. 빌드가 심고(`Dockerfile` 의 `BUILD_SHA`) 사람이
손대지 않는다.

## 미결정 사항

- 소비 측에 자격증명을 어떻게 줄지 ([api-contract.md](api-contract.md))
- 오공고 전달의 다섯 항목 ([spring-delivery.md](spring-delivery.md))
