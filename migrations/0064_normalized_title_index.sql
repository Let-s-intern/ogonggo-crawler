-- 0064 정규화 공고 제목 인덱스 (LC-3504, 2026-10-28)
--
-- 오공고 전송이 보낼 공고마다 이미 보낸 공고 중 회사·제목·마감 일시·나눈 직무 이름이 같은 공고를
-- 찾는다 (`app/deliver/spring.py` 의 `DUPLICATE_SQL`). 제목이 가장 잘 갈라 먼저 견주는데, 인덱스가
-- 없으면 보낼 공고 하나마다 정규화 표를 처음부터 끝까지 읽는다.
--
-- 되돌리기: 인덱스를 지운다.

-- migrate:up

CREATE INDEX idx_normalized_jobs_title ON normalized_jobs (title);

-- migrate:down

DROP INDEX idx_normalized_jobs_title;
