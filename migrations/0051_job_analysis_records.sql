-- 0051 공고 분석 기록과 실행 기록 (2026-10-08 결정, LC-3446)
--
-- 크롤러는 매일 오공고 서버에서 분석할 공고(분석이 없거나 본문이 바뀐 공고)를 받아 AI 로 분석하고
-- 결과를 서버로 보낸다. 크롤러가 모은 공고만이 아니라 고용24·기업 직접 등록 공고도 대상이라 공고
-- 한 건을 오공고 공고 id 로 가리킨다. '공고 분석' 화면이 이 표로 무엇을 분석했고 어디까지 보냈는지
-- 보여 준다 (`app/job_analysis/runner.py`).
--
-- 분석에 쓴 공고 글(`posting_json`)을 함께 남긴다. 화면에서 다시 분석하거나 원문을 볼 때 서버에 다시
-- 묻지 않는다. `content_hash` 는 서버가 준 본문 해시로, 결과를 보낼 때 그대로 돌려줘 그사이 본문이
-- 바뀌었으면 서버가 받지 않게 한다(409 → stale).
--
-- 상태: sent 보냄 · unsent 분석했지만 못 보냄 · failed 분석 실패 · stale 그사이 본문이 바뀜
--
-- 되돌리기: 두 표를 지운다.

-- migrate:up

CREATE TABLE job_analysis_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ogonggo_job_id INTEGER NOT NULL UNIQUE,
    source         TEXT NOT NULL DEFAULT '',
    company        TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    source_url     TEXT NOT NULL DEFAULT '',
    posting_json   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    analysis_json  TEXT,
    guide_version  INTEGER,
    model          TEXT NOT NULL DEFAULT '',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('sent', 'unsent', 'failed', 'stale')),
    error          TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    run_id         INTEGER,
    analyzed_at    TEXT,
    sent_at        TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_job_analysis_records_status ON job_analysis_records (status, updated_at);

CREATE TABLE job_analysis_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger        TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    status         TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    target_count   INTEGER NOT NULL DEFAULT 0,
    analyzed_count INTEGER NOT NULL DEFAULT 0,
    sent_count     INTEGER NOT NULL DEFAULT 0,
    failed_count   INTEGER NOT NULL DEFAULT 0,
    left_count     INTEGER NOT NULL DEFAULT 0,
    notes          TEXT NOT NULL DEFAULT '',
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT
);

-- migrate:down

DROP TABLE job_analysis_runs;
DROP INDEX idx_job_analysis_records_status;
DROP TABLE job_analysis_records;
