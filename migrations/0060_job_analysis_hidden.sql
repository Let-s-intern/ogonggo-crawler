-- 0060 분석을 끝내 못 한 공고를 오공고에서 숨긴 시각 (2026-10-21 결정)
--
-- 공고 분석이 같은 본문으로 세 번 실패했거나 본문이 비어 분석할 수 없는 공고는 사용자에게 보여 줄
-- 분석이 없다. 그런 공고를 오공고 내부 API(`POST /api/v1/internal/jobs/{jobId}/hide`)로 숨긴다
-- (`app/job_analysis/runner.py`). 숨긴 시각을 적어 같은 공고를 실행마다 다시 숨기지 않는다. 본문이
-- 바뀌면 비운다 — 운영자가 본문을 고쳐 다시 노출한 공고는 다시 분석해 보고, 또 못 하면 다시 숨긴다.
--
-- 되돌리기: 칸을 지운다.

-- migrate:up

ALTER TABLE job_analysis_records ADD COLUMN hidden_at TEXT;

-- migrate:down

ALTER TABLE job_analysis_records DROP COLUMN hidden_at;
