# 프로젝트 문서

이 서비스가 무엇을 어떻게 하는지에 대한 사람용 문서다. 제약(무엇을 하면 안 되는지)은 여기가
아니라 `.claude/rules/` 에 있다.

## 전체 그림

| 문서 | 내용 |
|---|---|
| [architecture.md](architecture.md) | 전체 구조, 프로세스 구성, 모듈 배치, 설정이 어디 있는가 |
| [tech-stack.md](tech-stack.md) | 기술 선택과 그 이유, 쓰지 않기로 한 것, 환경변수 |
| [data-model.md](data-model.md) | 테이블 정의, 중복 감지 hash, 상태 전이 |

## 흐름별

| 문서 | 내용 |
|---|---|
| [registration.md](registration.md) | 목록 URL 하나로 크롤러를 등록하기까지 |
| [fetching.md](fetching.md) | 공용 fetch 클라이언트, 수집 모드 셋, 마감 거르기 |
| [crawl-run.md](crawl-run.md) | 실행 1회에 무슨 일이 일어나는가, 실패 분류, 자동 중지 |
| [scheduling.md](scheduling.md) | APScheduler, 워크플로우 두 종류, 동시 실행 상한 |
| [normalization.md](normalization.md) | 규칙 엔진, 회사명 두 칸, 재정규화 |
| [classification.md](classification.md) | 본문을 아홉 칸으로 나누기, 근거 검증, 직무 분류 |
| [llm-providers.md](llm-providers.md) | 제공자 다섯, 기능별 선택, 호출 기록 |
| [integrations.md](integrations.md) | 알림, 로고 저장소, 전달 설정 |

## 경계면

| 문서 | 내용 |
|---|---|
| [api-contract.md](api-contract.md) | 소비 측이 가져가는 제공 API |
| [spring-delivery.md](spring-delivery.md) | 오공고 백엔드로 보내는 전달 설계. 아직 구현 전이다 |

## 측정 기록

| 문서 | 내용 |
|---|---|
| [ocr-benchmark.md](ocr-benchmark.md) | 수집 방식 네 가지의 시간·토큰 실측 (2026-08-24) |

원본 PRD 는 `.claude/tasks/done/job-crawler/prd-job-crawler.md` 에 있다.
사이트별 특성은 `.claude/site-recipes/` 에 사이트당 한 파일로 있다.
