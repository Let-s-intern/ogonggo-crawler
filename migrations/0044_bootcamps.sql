-- 0044 새싹(SeSAC) 부트캠프를 수집해 오공고로 보낸다 (2026-09-22 결정, LC-3364)
--
-- 공고 파이프라인(raw_jobs → normalized_jobs → 분류 → 전송)은 오공고 Job 칸에 맞춰져 있어 부트캠프를
-- 싣지 않는다. 부트캠프 사이트는 새싹 하나라 전용 수집기(`app/bootcamp/`)를 따로 두고, 과정 한 건을
-- 행 하나에 담는다. 파서가 읽은 값, AI 가 채운 글 칸, 오공고 전송 상태가 한 행에 있다.
--
-- 한 번 모아 AI 정리까지 끝난 과정은 다시 읽지 않는다 (2026-09-22 결정). 상세를 다시 받지도, AI 를
-- 다시 부르지도, 오공고로 다시 보내지도 않는다. 정리에 실패한 과정만 다음 수집에서 다시 받는다.
-- page_hash 는 파서가 읽은 값의 해시로, 정리(filled_hash)·전송(sent_hash)이 어느 판에 대한 것인지 가른다.
-- 모집 상태만은 매번 목록 카드에서 읽어 status_label 에 적는다. 보낸 상태(sent_status_label)와 다르면
-- 오공고에 다시 보낸다 — 모집중이던 과정이 운영중이 되면 오공고에서도 모집 마감이 돼야 한다.
--
-- 되돌리기: 두 표를 지운다.

-- migrate:up

CREATE TABLE bootcamps (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url             TEXT NOT NULL UNIQUE,
    site                   TEXT NOT NULL DEFAULT 'sesac',
    external_id            TEXT NOT NULL,
    title                  TEXT NOT NULL,
    campus                 TEXT NOT NULL DEFAULT '',
    category               TEXT NOT NULL DEFAULT '',
    status_label           TEXT NOT NULL DEFAULT '',
    recruitment_start_date TEXT,
    recruitment_end_date   TEXT,
    program_start_date     TEXT NOT NULL,
    program_end_date       TEXT NOT NULL,
    hours                  INTEGER,
    thumbnail_url          TEXT NOT NULL DEFAULT '',
    overview_text          TEXT NOT NULL DEFAULT '',
    overview_images_json   TEXT NOT NULL DEFAULT '[]',
    curriculum_json        TEXT NOT NULL DEFAULT '[]',
    page_hash              TEXT NOT NULL,
    short_description      TEXT,
    content                TEXT,
    eligibility            TEXT,
    capacity               INTEGER,
    manager_email          TEXT,
    filled_hash            TEXT,
    fill_error             TEXT NOT NULL DEFAULT '',
    spring_bootcamp_id     INTEGER,
    sent_hash              TEXT,
    sent_status_label      TEXT,
    send_status            TEXT CHECK (send_status IN ('sent', 'failed')),
    send_attempts          INTEGER NOT NULL DEFAULT 0,
    send_error             TEXT NOT NULL DEFAULT '',
    sent_at                TEXT,
    first_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE bootcamp_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL DEFAULT 'sesac',
    trigger       TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    status        TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    listed_count  INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    filled_count  INTEGER NOT NULL DEFAULT 0,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

-- migrate:down

DROP TABLE bootcamp_runs;
DROP TABLE bootcamps;
