-- 0042 주소 하나로 공고를 직접 넣는 워크플로우를 가른다 (2026-09-21 결정)
--
-- 목록을 주기적으로 도는 사이트 말고, 운영자가 공고 주소 하나를 넣어 한 번만 가져와 분류하는 길을
-- 더한다 (`app/crawler/manual.py`). 동원 하반기 신입 공채처럼 공고 한 장짜리 캠페인 페이지는
-- 목록이 없어 사이트로 등록할 수 없었다.
--
-- `raw_jobs.workflow_id` 가 필수라 그렇게 넣은 공고도 워크플로우 하나에 붙는다. 그 워크플로우는
-- 주기가 없고, 사이트 목록에 보이지 않고, 스케줄러가 돌리지 않는다 — 그것을 이 칸이 가른다.
-- 공고 화면에서는 사이트 이름 자리에 그 워크플로우 이름(`직접 추가`)이 보인다.
--
-- 되돌리기: 칸을 지운다. 직접 넣은 워크플로우와 그 공고는 남고, 이후로는 멈춘 사이트로 보인다.

-- migrate:up

ALTER TABLE workflows ADD COLUMN kind TEXT NOT NULL DEFAULT 'crawl'
    CHECK (kind IN ('crawl', 'manual'));

-- migrate:down

ALTER TABLE workflows DROP COLUMN kind;
