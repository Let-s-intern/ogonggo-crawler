-- 0040 분류가 회사 이름·모집 시작·모집 마감을 원문에서 짚어 둔다 (2026-09-17 결정, LC-3344)
--
-- 세 칸은 수집이 사이트에서 셀렉터로 읽는다. 셀렉터가 없거나 못 찾은 사이트(APR 등)는 세 칸이 늘
-- 비었고, 마감이 비어 기간 채용 공고가 상시 채용으로 나갔다. 분류는 이미 줄 번호로 본문 칸을 짚고
-- 있으니 같은 방법으로 이 세 칸도 짚어 둔다. 정규화는 사이트에서 읽은 값이 없을 때만 이 값을 쓴다
-- (`app/normalize/engine.py`).
--
-- 되돌리기: 세 컬럼을 지운다. 짚어 둔 값은 다시 분류하면 다시 생긴다.

-- migrate:up

ALTER TABLE job_classifications ADD COLUMN company_name TEXT;
ALTER TABLE job_classifications ADD COLUMN recruitment_start_at TEXT;
ALTER TABLE job_classifications ADD COLUMN recruitment_end_at TEXT;

-- migrate:down

ALTER TABLE job_classifications DROP COLUMN recruitment_end_at;
ALTER TABLE job_classifications DROP COLUMN recruitment_start_at;
ALTER TABLE job_classifications DROP COLUMN company_name;
