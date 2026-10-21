-- 0057 품질 검사로 다시 분류한 공고 (2026-10-21 결정)
--
-- 확인 필요 공고가 너무 많아 운영자가 손으로 다시 채우던 것을 자동으로 돌린다. 분류가 끝난 공고가
-- 품질 검사(`app/classify/gate.py`)의 네 조건 중 하나에 걸리면 AI 로 한 번 더 분류한다.
--
-- 공고마다 **한 번만** 다시 분류한다. 원문에 마감일이 없는 상시 채용처럼 다시 돌려도 그대로인 공고가
-- 있어, 적어 두지 않으면 분류할 때마다 돈이 다시 나간다. 행이 있는 공고는 검사가 다시 집지 않는다.
--
-- `reasons` 는 다시 분류하기 전에 걸린 조건, `reasons_after` 는 다시 분류한 뒤에도 걸린 조건이다.
-- 둘 다 쉼표로 이은 조건 이름이다. 다시 분류가 끝나기 전이거나 실패했으면 `finished_at` 이 비어 있다.
--
-- 되돌리기: 표를 지운다.

-- migrate:up

CREATE TABLE quality_gate_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id    INTEGER NOT NULL UNIQUE REFERENCES raw_jobs (id) ON DELETE CASCADE,
    reasons       TEXT    NOT NULL,
    reasons_after TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

-- migrate:down

DROP TABLE quality_gate_runs;
