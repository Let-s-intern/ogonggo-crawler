-- 0066 정규화 공고 회사·마감 인덱스 (LC-3504, 2026-10-28)
--
-- 오공고 전송이 보낼 공고마다 이미 보낸 공고 중 회사·사이트 제목·마감 일시·나눈 직무 이름이 같은
-- 공고를 찾는다 (`app/deliver/spring.py` 의 `DUPLICATE_SQL`). 사이트 제목은 수집 원본 JSON 안에 있어
-- 인덱스를 달 수 없으니 회사와 마감으로 먼저 좁힌다. 인덱스가 없으면 보낼 공고 하나마다 정규화 표를
-- 처음부터 끝까지 읽는다.
--
-- 되돌리기: 인덱스를 지운다.

-- migrate:up

CREATE INDEX idx_normalized_jobs_company_end ON normalized_jobs (company_name, recruitment_end_at);

-- migrate:down

DROP INDEX idx_normalized_jobs_company_end;
