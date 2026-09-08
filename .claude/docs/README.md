# 프로젝트 문서

이 서비스가 무엇을 어떻게 하는지에 대한 사람용 문서다. 제약(무엇을 하면 안 되는지)은 여기가 아니라
`.claude/rules/` 에 있다.

| 문서 | 내용 |
|---|---|
| [architecture.md](architecture.md) | 전체 구조, 파이프라인 단계, 폴더 배치 |
| [data-model.md](data-model.md) | 테이블 정의, 중복 감지 hash, 상태 전이 |
| [api-contract.md](api-contract.md) | 채용공고 사이트가 소비하는 제공 API |
| [spring-delivery.md](spring-delivery.md) | 오공고 백엔드로 보내는 전달 설계. 아직 구현 전이다 |
| [tech-stack.md](tech-stack.md) | 기술 선택과 그 이유, 쓰지 않기로 한 것 |
| [ocr-benchmark.md](ocr-benchmark.md) | 수집 방식 네 가지의 시간·토큰 실측 (2026-08-24) |

원본 PRD 는 `.claude/tasks/done/job-crawler/prd-job-crawler.md` 에 있다.
사이트별 특성은 `.claude/site-recipes/` 에 사이트당 한 파일로 있다.
