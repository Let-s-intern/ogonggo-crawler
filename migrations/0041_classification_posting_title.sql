-- 0041 분류가 공고마다 제목을 짓는다 (2026-09-18 결정)
--
-- 사이트 제목은 `2026년 하반기 신입사원 모집` 처럼 직무를 말하지 않는 것이 많다. 분류가 사이트 제목과
-- 본문을 보고 그 공고의 직무 이름이 반드시 들어간 제목을 지어 여기 둔다. 정규화는 이 값이 있으면
-- 사이트 제목 대신 쓰고, 없으면 사이트 제목을 그대로 쓴다 (`app/normalize/engine.py`). 사람이 고친
-- 제목은 그 위에 덮인다.
--
-- 되돌리기: 컬럼을 지운다. 지은 제목은 다시 분류하면 다시 생긴다.

-- migrate:up

ALTER TABLE job_classifications ADD COLUMN posting_title TEXT;

-- migrate:down

ALTER TABLE job_classifications DROP COLUMN posting_title;
